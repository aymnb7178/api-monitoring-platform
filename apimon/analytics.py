"""Pure analytics over request DataFrames: percentiles, Apdex, SLO burn rate, anomalies."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

DEFAULT_POLICIES = [
    {"name": "fast", "long_s": 3600, "short_s": 300, "factor": 14.4, "severity": "page"},
    {"name": "slow", "long_s": 21600, "short_s": 1800, "factor": 6.0, "severity": "ticket"},
]


def _f(x, nd=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


def error_mask(df: pd.DataFrame) -> pd.Series:
    """A request counts as failed on HTTP 5xx or a recorded exception. 4xx are client errors."""
    return (df["status"] >= 500) | df["error"].notna()


def summary(df: pd.DataFrame, window_s: float, apdex_t_ms: float) -> dict:
    """Headline stats. Counts are weighted by 1/sample_rate so sampling does not skew rates."""
    empty = {"requests": 0, "recorded_events": 0, "errors": 0, "error_rate": None, "throughput_rps": 0.0,
             "p50_ms": None, "p95_ms": None, "p99_ms": None, "apdex": None,
             "avg_request_bytes": None, "avg_response_bytes": None}
    if df.empty:
        return empty
    w = 1.0 / df["sample_rate"].to_numpy(float)
    err = error_mask(df).to_numpy()
    lat = df["latency_ms"].to_numpy(float)
    n, e = w.sum(), (w * err).sum()
    sat = w[(lat <= apdex_t_ms) & ~err].sum()
    tol = w[(lat > apdex_t_ms) & (lat <= 4 * apdex_t_ms) & ~err].sum()
    p50, p95, p99 = np.percentile(lat, [50, 95, 99])
    return {"requests": int(round(n)), "recorded_events": int(len(df)), "errors": int(round(e)),
            "error_rate": _f(e / n, 5), "throughput_rps": _f(n / window_s, 3),
            "p50_ms": _f(p50), "p95_ms": _f(p95), "p99_ms": _f(p99),
            "apdex": _f((sat + tol / 2) / n, 4),
            "avg_request_bytes": _f(df["request_bytes"].mean(), 1),
            "avg_response_bytes": _f(df["response_bytes"].mean(), 1)}


def per_endpoint(df: pd.DataFrame, apdex_t_ms: float, slow_ms: float, min_requests: int = 20,
                 rank_by: str = "p95_ms") -> list[dict]:
    """Per (method, route) stats, ranked descending by `rank_by`. `slow` = p95 above slow_ms
    with at least min_requests samples."""
    if df.empty:
        return []
    total_time = df["latency_ms"].sum()
    rows = []
    for (method, route), g in df.groupby(["method", "route"]):
        s = summary(g, 1.0, apdex_t_ms)
        rows.append({"method": method, "route": route, "requests": s["requests"], "errors": s["errors"],
                     "error_rate": s["error_rate"], "p50_ms": s["p50_ms"], "p95_ms": s["p95_ms"],
                     "p99_ms": s["p99_ms"], "apdex": s["apdex"],
                     "time_share": _f(g["latency_ms"].sum() / total_time, 4),
                     "slow": bool(len(g) >= min_requests and s["p95_ms"] > slow_ms)})
    if rank_by not in rows[0]:
        raise ValueError(f"cannot rank by {rank_by!r}")
    return sorted(rows, key=lambda r: (r[rank_by] is None, -(r[rank_by] or 0)))


def timeseries(df: pd.DataFrame, start: float, end: float, bucket_s: float) -> list[dict]:
    """Fixed-width buckets over (start, end]; empty buckets carry None so gaps stay visible."""
    nb = max(1, int(math.ceil((end - start) / bucket_s)))
    out = []
    groups = {}
    if not df.empty:
        idx = np.minimum(((df["ts"].to_numpy() - start) // bucket_s).astype(int), nb - 1)
        for i, g in df.assign(_b=idx).groupby("_b"):
            groups[int(i)] = g
    for i in range(nb):
        t = start + (i + 1) * bucket_s
        g = groups.get(i)
        if g is None or g.empty:
            out.append({"t": t, "count": 0, "rps": 0.0, "p50_ms": None, "p95_ms": None, "p99_ms": None,
                        "error_rate": None})
            continue
        s = summary(g, bucket_s, 1.0)
        out.append({"t": t, "count": s["requests"], "rps": s["throughput_rps"], "p50_ms": s["p50_ms"],
                    "p95_ms": s["p95_ms"], "p99_ms": s["p99_ms"], "error_rate": s["error_rate"]})
    return out


def robust_z(values) -> np.ndarray:
    """Modified z-score 0.6745*(x-median)/MAD. If MAD is 0 (more than half the values identical) fall back
    to the mean absolute deviation (Iglewicz & Hoaglin); all-identical input yields zeros."""
    x = np.asarray(values, float)
    med = np.median(x)
    mad = np.median(np.abs(x - med))
    if mad > 0:
        return 0.6745 * (x - med) / mad
    meanad = np.mean(np.abs(x - med))
    if meanad == 0:
        return np.zeros_like(x)
    return (x - med) / (1.253314 * meanad)


def detect_anomalies(series: list[dict], key: str = "p95_ms", threshold: float = 3.5, min_points: int = 8,
                     min_count: int = 5) -> list[dict]:
    """Upward latency anomalies (robust z > threshold) among buckets with enough traffic."""
    pts = [(i, p) for i, p in enumerate(series) if p[key] is not None and p["count"] >= min_count]
    if len(pts) < min_points:
        return []
    z = robust_z([p[key] for _, p in pts])
    return [{"t": p["t"], "value": p[key], "z": _f(zi), "count": p["count"]}
            for (_, p), zi in zip(pts, z) if zi > threshold]


def _window_rate(df, t_from, t_to):
    d = df[(df["ts"] > t_from) & (df["ts"] <= t_to)]
    if d.empty:
        return None
    w = 1.0 / d["sample_rate"].to_numpy(float)
    return float((w * error_mask(d).to_numpy()).sum() / w.sum())


def burn_rate(df, now: float, window_s: float, target: float):
    """Error-budget burn rate: observed error rate / allowed error rate (1 - target). 1.0 = exactly on budget."""
    r = _window_rate(df, now - window_s, now)
    return None if r is None else r / (1 - target)


def burn_series(df, times: list[float], window_s: float, target: float) -> list[float | None]:
    return [_f(burn_rate(df, t, window_s, target), 3) for t in times]


def slo_report(df, now: float, target: float, period_s: float, policies=None) -> dict:
    """Availability SLO status: budget consumed over `period_s`, and multi-window burn-rate policies
    (a policy fires only when both its long and short window exceed the factor)."""
    policies = policies or DEFAULT_POLICIES
    p = df[(df["ts"] > now - period_s) & (df["ts"] <= now)]
    if p.empty:
        spent = remaining = None
        n = bad = 0
    else:
        w = 1.0 / p["sample_rate"].to_numpy(float)
        n, bad = float(w.sum()), float((w * error_mask(p).to_numpy()).sum())
        allowed = (1 - target) * n
        spent = bad / allowed
        remaining = 1 - spent
    pol = []
    for q in policies:
        bl, bs = burn_rate(df, now, q["long_s"], target), burn_rate(df, now, q["short_s"], target)
        pol.append({**q, "burn_long": _f(bl, 3), "burn_short": _f(bs, 3),
                    "firing": bool(bl is not None and bs is not None and bl >= q["factor"] and bs >= q["factor"])})
    return {"target": target, "period_s": period_s, "requests": int(round(n)), "bad": int(round(bad)),
            "budget_spent": _f(spent, 4), "budget_remaining": _f(remaining, 4), "policies": pol}
