"""SQLite schema and repository helpers (stdlib sqlite3 + pandas for reads)."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    slo_target REAL NOT NULL DEFAULT 0.99,
    apdex_t_ms REAL NOT NULL DEFAULT 300,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS endpoints (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    route TEXT NOT NULL,
    first_seen REAL NOT NULL,
    UNIQUE (project_id, method, route)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    status INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    request_bytes INTEGER NOT NULL DEFAULT 0,
    response_bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    request_id TEXT,
    sample_rate REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_requests_project_ts ON requests (project_id, ts);
CREATE TABLE IF NOT EXISTS check_results (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    check_name TEXT NOT NULL,
    ts REAL NOT NULL,
    ok INTEGER NOT NULL,
    status INTEGER,
    latency_ms REAL,
    attempts INTEGER NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_project_ts ON check_results (project_id, ts);
CREATE TABLE IF NOT EXISTS alert_state (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    rule TEXT NOT NULL,
    state TEXT NOT NULL,
    consecutive INTEGER NOT NULL DEFAULT 0,
    since REAL NOT NULL,
    last_notified REAL,
    firing_notified INTEGER NOT NULL DEFAULT 0,
    last_value REAL,
    PRIMARY KEY (project_id, rule)
);
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    rule TEXT NOT NULL,
    ts REAL NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    value REAL,
    notified INTEGER NOT NULL,
    message TEXT NOT NULL
);
"""

REQUEST_COLUMNS = ["ts", "method", "route", "status", "latency_ms", "request_bytes",
                   "response_bytes", "error", "request_id", "sample_rate"]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def hash_key(key: str) -> str:
    """SHA-256 of the key. Keys are 192-bit random tokens, so a fast hash is adequate
    (unlike passwords, they cannot be brute-forced from a dictionary)."""
    return hashlib.sha256(key.encode()).hexdigest()


def create_project(conn, name: str, slo_target: float = 0.99, apdex_t_ms: float = 300.0):
    """Create a project and its first API key. Returns (project_id, plaintext_key); the
    plaintext is never stored."""
    if not 0 < slo_target < 1:
        raise ValueError("slo_target must be between 0 and 1 (exclusive)")
    if apdex_t_ms <= 0:
        raise ValueError("apdex_t_ms must be positive")
    cur = conn.execute("INSERT INTO projects (name, slo_target, apdex_t_ms, created_at) VALUES (?,?,?,?)",
                       (name, slo_target, apdex_t_ms, time.time()))
    pid = cur.lastrowid
    key = new_api_key(conn, pid)
    return pid, key


def new_api_key(conn, project_id: int) -> str:
    key = "apm_" + secrets.token_urlsafe(24)
    conn.execute("INSERT INTO api_keys (project_id, key_hash, key_prefix, created_at) VALUES (?,?,?,?)",
                 (project_id, hash_key(key), key[:8], time.time()))
    conn.commit()
    return key


def project_by_key(conn, key: str):
    return conn.execute(
        "SELECT p.* FROM projects p JOIN api_keys k ON k.project_id = p.id WHERE k.key_hash = ?",
        (hash_key(key),)).fetchone()


def project_by_name(conn, name: str):
    return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def register_endpoint(conn, project_id: int, method: str, route: str, now: float | None = None) -> int:
    conn.execute("INSERT OR IGNORE INTO endpoints (project_id, method, route, first_seen) VALUES (?,?,?,?)",
                 (project_id, method.upper(), route, now if now is not None else time.time()))
    row = conn.execute("SELECT id FROM endpoints WHERE project_id=? AND method=? AND route=?",
                       (project_id, method.upper(), route)).fetchone()
    return row["id"]


def insert_events(conn, project_id: int, events: list[dict]) -> int:
    """Insert a batch in a single transaction, auto-registering endpoints."""
    cache: dict[tuple[str, str], int] = {}
    rows = []
    for e in events:
        k = (e["method"].upper(), e["route"])
        if k not in cache:
            cache[k] = register_endpoint(conn, project_id, k[0], k[1], e["ts"])
        rows.append((project_id, cache[k], e["ts"], e["status"], e["latency_ms"],
                     e.get("request_bytes", 0), e.get("response_bytes", 0), e.get("error"),
                     e.get("request_id"), e.get("sample_rate", 1.0)))
    conn.executemany(
        "INSERT INTO requests (project_id, endpoint_id, ts, status, latency_ms, request_bytes,"
        " response_bytes, error, request_id, sample_rate) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def load_requests(conn, project_id: int, since: float, until: float) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT r.ts, e.method, e.route, r.status, r.latency_ms, r.request_bytes, r.response_bytes,"
        " r.error, r.request_id, r.sample_rate FROM requests r JOIN endpoints e ON e.id = r.endpoint_id"
        " WHERE r.project_id = ? AND r.ts > ? AND r.ts <= ? ORDER BY r.ts",
        conn, params=(project_id, since, until))
    return df


def save_check_results(conn, project_id: int, results: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO check_results (project_id, check_name, ts, ok, status, latency_ms, attempts, error)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(project_id, r["name"], r["ts"], int(r["ok"]), r.get("status"), r.get("latency_ms"),
          r["attempts"], r.get("error")) for r in results])
    conn.commit()


def load_check_results(conn, project_id: int, since: float, until: float) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT check_name, ts, ok, status, latency_ms, attempts, error FROM check_results"
        " WHERE project_id=? AND ts > ? AND ts <= ? ORDER BY ts", conn, params=(project_id, since, until))


def latest_checks(conn, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT c.check_name, c.ts, c.ok, c.status, c.latency_ms, c.attempts, c.error FROM check_results c"
        " WHERE c.project_id=? AND c.id = (SELECT MAX(id) FROM check_results WHERE project_id=c.project_id"
        " AND check_name=c.check_name) ORDER BY c.check_name", (project_id,)).fetchall()
    return [dict(r) for r in rows]
