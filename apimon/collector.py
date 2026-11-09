"""Collector API (Flask + sqlite3): project/endpoint registry, validated ingestion, analytics, dashboard."""
from __future__ import annotations

import base64
import hmac
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, render_template, request
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import alerts as AL
from . import analytics as A
from . import charts, db
from . import uptime as UP
from .ratelimit import RateLimiter
from .report import build_report

log = logging.getLogger("apimon.collector")
_RID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class Event(BaseModel):
    ts: float = Field(gt=0)
    method: str = Field(pattern=r"^[A-Za-z]{3,10}$")
    route: str = Field(min_length=1, max_length=200)
    status: int = Field(ge=100, le=599)
    latency_ms: float = Field(ge=0, le=600_000)
    request_bytes: int = Field(0, ge=0)
    response_bytes: int = Field(0, ge=0)
    error: str | None = Field(None, max_length=200)
    request_id: str | None = Field(None, max_length=64)
    sample_rate: float = Field(1.0, gt=0, le=1)

    @field_validator("method")
    @classmethod
    def _up(cls, v):
        return v.upper()


class IngestBody(BaseModel):
    events: list[Event] = Field(min_length=1, max_length=1000)


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._ -]+$")
    slo_target: float = Field(0.99, gt=0, lt=1)
    apdex_t_ms: float = Field(300.0, gt=0)


class EndpointBody(BaseModel):
    method: str = Field(pattern=r"^[A-Za-z]{3,10}$")
    route: str = Field(min_length=1, max_length=200)


def _err(code: str, message: str, status: int, **extra):
    resp = jsonify({"error": {"code": code, "message": message, **extra}, "request_id": g.get("request_id")})
    resp.status_code = status
    return resp


def create_app(db_path: str, admin_token: str | None = None, rate_capacity: float = 60.0,
               rate_refill: float = 30.0, clock=time.time, alert_config: AL.AlertConfig | None = None,
               checks: list[UP.CheckConfig] | None = None, policies=None) -> Flask:
    app = Flask(__name__)
    app.config.update(DB_PATH=db_path, ADMIN_TOKEN=admin_token)
    limiter = RateLimiter(rate_capacity, rate_refill)
    boot = db.connect(db_path)
    db.init_db(boot)
    boot.close()

    def conn():
        if "db" not in g:
            g.db = db.connect(db_path)
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    @app.before_request
    def _rid():
        rid = request.headers.get("X-Request-ID", "")
        g.request_id = rid if _RID.match(rid) else uuid.uuid4().hex

    @app.after_request
    def _rid_out(resp):
        resp.headers["X-Request-ID"] = g.get("request_id", "")
        return resp

    def now_ts() -> float:
        at = request.args.get("at", type=float)
        return at if at is not None else clock()

    def window_args():
        w = request.args.get("window", 3600, type=float)
        b = request.args.get("bucket", None, type=float)
        if not 1 <= w <= 30 * 86400 or (b is not None and not 0.1 <= b <= w):
            raise ValueError("window must be 1s..30d and bucket within 0.1s..window")
        return w, b

    def api_key_from_request() -> str:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return request.headers.get("X-API-Key") or request.args.get("key", "")

    def require_key(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            key = api_key_from_request()
            project = db.project_by_key(conn(), key) if key else None
            if project is None:
                return _err("unauthorized", "missing or invalid API key", 401)
            g.project = project
            ok, retry = limiter.allow(f"{project['id']}:{key[:12]}")
            if not ok:
                resp = _err("rate_limited", "too many requests", 429, retry_after_s=round(retry, 3))
                resp.headers["Retry-After"] = str(max(1, int(retry + 0.999)))
                return resp
            return fn(*a, **kw)
        return wrapper

    @app.errorhandler(404)
    def _nf(_e):
        return _err("not_found", "no such route", 404)

    @app.errorhandler(405)
    def _na(_e):
        return _err("method_not_allowed", "method not allowed", 405)

    @app.errorhandler(Exception)
    def _boom(e):
        if hasattr(e, "code") and isinstance(getattr(e, "code"), int):
            return _err("http_error", getattr(e, "description", str(e)), e.code)
        log.exception("unhandled error (request_id=%s)", g.get("request_id"))
        return _err("internal_error", "internal server error", 500)

    def body_model(model):
        data = request.get_json(silent=True)
        if data is None:
            raise ValueError("body must be valid JSON")
        return model.model_validate(data)

    @app.errorhandler(ValidationError)
    def _val(e):
        return _err("validation_error", "invalid request body", 422,
                    details=[{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()][:10])

    @app.errorhandler(ValueError)
    def _bad(e):
        return _err("bad_request", str(e), 400)

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.post("/api/projects")
    def create_project():
        token = app.config["ADMIN_TOKEN"]
        sent = request.headers.get("X-Admin-Token", "")
        if not token or not hmac.compare_digest(sent.encode(), token.encode()):
            return _err("forbidden", "admin token required", 403)
        b = body_model(ProjectBody)
        try:
            pid, key = db.create_project(conn(), b.name, b.slo_target, b.apdex_t_ms)
        except Exception as e:  # unique violation
            if "UNIQUE" in str(e):
                return _err("conflict", "project name already exists", 409)
            raise
        conn().commit()
        return jsonify({"project": {"id": pid, "name": b.name, "slo_target": b.slo_target,
                                    "apdex_t_ms": b.apdex_t_ms},
                        "api_key": key, "note": "store this key now; only its hash is kept"}), 201

    @app.get("/api/endpoints")
    @require_key
    def list_endpoints():
        rows = conn().execute(
            "SELECT e.method, e.route, e.first_seen, COUNT(r.id) AS recorded_events FROM endpoints e"
            " LEFT JOIN requests r ON r.endpoint_id = e.id WHERE e.project_id=? GROUP BY e.id"
            " ORDER BY e.route, e.method", (g.project["id"],)).fetchall()
        return jsonify({"endpoints": [dict(r) for r in rows]})

    @app.post("/api/endpoints")
    @require_key
    def add_endpoint():
        b = body_model(EndpointBody)
        eid = db.register_endpoint(conn(), g.project["id"], b.method, b.route, clock())
        conn().commit()
        return jsonify({"id": eid, "method": b.method.upper(), "route": b.route}), 201

    @app.post("/api/ingest")
    @require_key
    def ingest():
        b = body_model(IngestBody)
        horizon = clock() + 86400
        if any(e.ts > horizon for e in b.events):
            raise ValueError("event timestamp is more than 24h in the future")
        n = db.insert_events(conn(), g.project["id"], [e.model_dump() for e in b.events])
        return jsonify({"accepted": n}), 202

    @app.get("/api/stats")
    @require_key
    def stats():
        w, _ = window_args()
        now = now_ts()
        df = db.load_requests(conn(), g.project["id"], now - w, now)
        return jsonify({"window_s": w, **A.summary(df, w, g.project["apdex_t_ms"])})

    @app.get("/api/endpoints/stats")
    @require_key
    def endpoint_stats():
        w, _ = window_args()
        now = now_ts()
        T = g.project["apdex_t_ms"]
        df = db.load_requests(conn(), g.project["id"], now - w, now)
        rank = request.args.get("rank_by", "p95_ms")
        try:
            rows = A.per_endpoint(df, T, request.args.get("slow_ms", 2 * T, type=float), rank_by=rank)
        except ValueError as e:
            return _err("bad_request", str(e), 400)
        return jsonify({"window_s": w, "rank_by": rank, "endpoints": rows})

    @app.get("/api/slo")
    @require_key
    def slo():
        w, _ = window_args()
        now = now_ts()
        horizon = max([w] + [p["long_s"] for p in (policies or A.DEFAULT_POLICIES)])
        df = db.load_requests(conn(), g.project["id"], now - horizon, now)
        return jsonify(A.slo_report(df, now, g.project["slo_target"], w, policies))

    @app.get("/api/anomalies")
    @require_key
    def anomalies():
        w, b = window_args()
        now = now_ts()
        df = db.load_requests(conn(), g.project["id"], now - w, now)
        series = A.timeseries(df, now - w, now, b or max(1.0, w / 60))
        return jsonify({"window_s": w, "method": "robust z-score (MAD), upward, threshold 3.5",
                        "anomalies": A.detect_anomalies(series)})

    @app.get("/api/report")
    @require_key
    def report():
        w, b = window_args()
        return jsonify(build_report(conn(), g.project, now_ts(), w, b, policies))

    @app.get("/api/alerts")
    @require_key
    def alerts_state():
        return jsonify({"rules": AL.current_states(conn(), g.project["id"]),
                        "events": AL.recent_events(conn(), g.project["id"])})

    @app.post("/api/alerts/evaluate")
    @require_key
    def alerts_eval():
        if alert_config is None:
            return _err("not_configured", "server started without an alert config", 409)
        engine = AL.AlertEngine(conn(), g.project, alert_config)
        return jsonify({"transitions": engine.evaluate(now_ts())})

    @app.get("/api/uptime")
    @require_key
    def uptime_latest():
        return jsonify({"checks": db.latest_checks(conn(), g.project["id"])})

    @app.post("/api/uptime/run")
    @require_key
    def uptime_run():
        if not checks:
            return _err("not_configured", "server started without uptime checks", 409)
        results = UP.run_checks(checks)  # URLs come from server config only, never from the request
        db.save_check_results(conn(), g.project["id"], results)
        return jsonify({"results": results})

    @app.get("/dashboard")
    @require_key
    def dashboard():
        w, b = window_args()
        rep = build_report(conn(), g.project, now_ts(), w, b, policies)
        return render_dashboard(rep, "synthetic or user-supplied events stored in the local SQLite database")

    return app


def render_dashboard(rep: dict, source_note: str) -> str:
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")  # noqa: E731
    for a in rep["anomalies"]:
        a["t_iso"] = iso(a["t"])
    img = {k: base64.b64encode(v).decode() for k, v in charts.all_charts(rep).items()}
    from flask import has_app_context
    if has_app_context():
        return render_template("dashboard.html", r=rep, img=img, now_iso=iso(rep["now"]), source_note=source_note)
    app = Flask(__name__)
    with app.app_context():
        return render_template("dashboard.html", r=rep, img=img, now_iso=iso(rep["now"]), source_note=source_note)
