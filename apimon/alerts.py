"""JSON-configured alert rules with an ok/pending/firing/resolved state machine and mock sinks."""
from __future__ import annotations

import json
import logging
import operator
import sys
import time
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import analytics as A
from . import db

log = logging.getLogger("apimon.alerts")
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
METRICS = ("p50_ms", "p95_ms", "p99_ms", "error_rate", "apdex", "throughput_rps", "burn_rate",
           "uptime_failure_rate")


class AlertRule(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    metric: Literal[METRICS]
    op: Literal[">", ">=", "<", "<="] = ">"
    threshold: float
    window_s: float = Field(300, gt=0)
    for_evaluations: int = Field(1, ge=1)
    cooldown_s: float = Field(0, ge=0)
    min_requests: int = Field(1, ge=1)
    route: str | None = None
    severity: str = "warning"
    sinks: list[str] = ["console"]


class AlertConfig(BaseModel):
    sinks: dict[str, dict] = {"console": {"type": "console"}}
    rules: list[AlertRule]

    @model_validator(mode="after")
    def _sinks_exist(self):
        for r in self.rules:
            for s in r.sinks:
                if s not in self.sinks:
                    raise ValueError(f"rule {r.name!r} references unknown sink {s!r}")
        return self


def load_config(path_or_dict) -> AlertConfig:
    if isinstance(path_or_dict, dict):
        return AlertConfig(**path_or_dict)
    with open(path_or_dict, encoding="utf-8") as fh:
        return AlertConfig(**json.load(fh))


class ConsoleSink:
    def __init__(self, stream=None):
        self.stream = stream

    def send(self, event: dict) -> None:
        print(f"ALERT {event['message']}", file=self.stream or sys.stdout)


class FileSink:
    def __init__(self, path: str):
        self.path = path

    def send(self, event: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")


class WebhookMockSink:
    """Builds the JSON body a webhook would receive but never opens a socket."""

    def __init__(self, url: str = "https://example.invalid/hook"):
        self.url, self.sent = url, []

    def send(self, event: dict) -> None:
        self.sent.append({"url": self.url, "body": {"text": event["message"], "rule": event["rule"],
                                                     "state": event["to_state"], "value": event["value"]}})


def build_sinks(cfg: AlertConfig, stream=None) -> dict:
    out = {}
    for name, c in cfg.sinks.items():
        t = c.get("type")
        if t == "console":
            out[name] = ConsoleSink(stream)
        elif t == "file":
            out[name] = FileSink(c["path"])
        elif t == "webhook-mock":
            out[name] = WebhookMockSink(c.get("url", "https://example.invalid/hook"))
        else:
            raise ValueError(f"unknown sink type {t!r}")
    return out


def next_state(state: str, consecutive: int, cond: bool, for_evaluations: int) -> tuple[str, int]:
    """Pure transition function.
    ok/resolved --cond--> pending (or firing once consecutive >= for_evaluations)
    pending --not cond--> ok;  pending --cond x N--> firing
    firing --not cond--> resolved;  resolved --not cond--> ok"""
    if state in ("ok", "resolved"):
        if cond:
            c = 1
            return ("firing" if c >= for_evaluations else "pending"), c
        return "ok", 0
    if state == "pending":
        if cond:
            c = consecutive + 1
            return ("firing" if c >= for_evaluations else "pending"), c
        return "ok", 0
    if state == "firing":
        return ("firing", consecutive + 1) if cond else ("resolved", 0)
    raise ValueError(state)


class AlertEngine:
    def __init__(self, conn, project, config: AlertConfig, sinks: dict | None = None, slo_target=None):
        self.conn, self.project, self.config = conn, project, config
        self.sinks = sinks if sinks is not None else build_sinks(config)
        self.target = slo_target or project["slo_target"]

    def metric_value(self, rule: AlertRule, now: float):
        since = now - rule.window_s
        if rule.metric == "uptime_failure_rate":
            c = db.load_check_results(self.conn, self.project["id"], since, now)
            return None if c.empty else float(1 - c["ok"].mean())
        df = db.load_requests(self.conn, self.project["id"], since, now)
        if rule.route:
            df = df[df["route"] == rule.route]
        if len(df) < rule.min_requests:
            return None
        if rule.metric == "burn_rate":
            return A.burn_rate(df, now, rule.window_s, self.target)
        return A.summary(df, rule.window_s, self.project["apdex_t_ms"])[rule.metric]

    def evaluate(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        pid, events = self.project["id"], []
        for rule in self.config.rules:
            value = self.metric_value(rule, now)
            cond = value is not None and OPS[rule.op](value, rule.threshold)
            row = self.conn.execute("SELECT * FROM alert_state WHERE project_id=? AND rule=?",
                                    (pid, rule.name)).fetchone()
            state, consec, since, last_notified, fired_ok = (
                (row["state"], row["consecutive"], row["since"], row["last_notified"], row["firing_notified"])
                if row else ("ok", 0, now, None, 0))
            new, consec = next_state(state, consec, cond, rule.for_evaluations)
            notify = False
            if new != state:
                since = now
                if new == "firing":
                    notify = last_notified is None or now - last_notified >= rule.cooldown_s
                    fired_ok = int(notify)
                    if notify:
                        last_notified = now
                elif new == "resolved":
                    notify = bool(fired_ok)
                events.append(self._event(rule, state, new, value, notify, now))
            self.conn.execute(
                "INSERT OR REPLACE INTO alert_state (project_id, rule, state, consecutive, since, last_notified,"
                " firing_notified, last_value) VALUES (?,?,?,?,?,?,?,?)",
                (pid, rule.name, new, consec, since, last_notified, fired_ok, value))
            if new != state and notify:
                for s in rule.sinks:
                    try:
                        self.sinks[s].send(events[-1])
                    except Exception:
                        log.exception("sink %s failed", s)
        for e in events:
            self.conn.execute(
                "INSERT INTO alert_events (project_id, rule, ts, from_state, to_state, value, notified, message)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (pid, e["rule"], e["ts"], e["from_state"], e["to_state"], e["value"], int(e["notified"]),
                 e["message"]))
        self.conn.commit()
        return events

    @staticmethod
    def _event(rule, old, new, value, notified, now):
        v = None if value is None else round(value, 4)
        return {"rule": rule.name, "severity": rule.severity, "ts": now, "from_state": old, "to_state": new,
                "value": v, "notified": notified,
                "message": f"[{rule.severity}] {rule.name} {old}->{new}: {rule.metric}={v} "
                           f"(rule: {rule.op} {rule.threshold}, window {rule.window_s:g}s)"
                           + ("" if notified or new in ("pending", "ok") else " [suppressed]")}


def current_states(conn, project_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT rule, state, consecutive, since, last_value FROM alert_state WHERE project_id=? ORDER BY rule",
        (project_id,))]


def recent_events(conn, project_id: int, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT rule, ts, from_state, to_state, value, notified, message FROM alert_events WHERE project_id=?"
        " ORDER BY id DESC LIMIT ?", (project_id, limit))]
