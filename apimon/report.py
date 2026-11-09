"""Assemble the full monitoring report used by the JSON API, the dashboard, the CLI and the demo."""
from __future__ import annotations

from . import alerts as AL
from . import analytics as A
from . import db


def build_report(conn, project, now: float, window_s: float = 3600, bucket_s: float | None = None,
                 policies=None, slow_ms: float | None = None, rank_by: str = "p95_ms") -> dict:
    policies = policies or A.DEFAULT_POLICIES
    pid, T = project["id"], project["apdex_t_ms"]
    start = now - window_s
    bucket_s = bucket_s or max(1.0, window_s / 60)
    horizon = max([window_s] + [p["long_s"] for p in policies])
    df_all = db.load_requests(conn, pid, now - horizon, now)
    df = df_all[df_all["ts"] > start]
    slow_ms = slow_ms or 2 * T
    series = A.timeseries(df, start, now, bucket_s)
    p0 = policies[0]
    times = [p["t"] for p in series]
    return {
        "project": project["name"], "now": now, "window_s": window_s, "bucket_s": bucket_s,
        "apdex_t_ms": T, "slow_ms": slow_ms,
        "summary": A.summary(df, window_s, T),
        "series": series,
        "anomalies": A.detect_anomalies(series),
        "endpoints": A.per_endpoint(df, T, slow_ms, rank_by=rank_by),
        "slo": A.slo_report(df_all, now, project["slo_target"], window_s, policies),
        "burn_series": {"long_s": p0["long_s"], "short_s": p0["short_s"], "factor": p0["factor"],
                        "long": A.burn_series(df_all, times, p0["long_s"], project["slo_target"]),
                        "short": A.burn_series(df_all, times, p0["short_s"], project["slo_target"])},
        "alerts": AL.current_states(conn, pid),
        "alert_events": AL.recent_events(conn, pid, 10),
        "uptime": db.latest_checks(conn, pid),
    }
