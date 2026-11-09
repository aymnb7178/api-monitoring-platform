"""matplotlib (Agg) charts rendered to PNG bytes from a report dict."""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BLUE, ORANGE, RED, GREY, GREEN = "#2563eb", "#d97706", "#dc2626", "#6b7280", "#15803d"


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _xs(report):
    return [(p["t"] - report["now"]) / 60.0 for p in report["series"]]


def _style(ax, ylabel):
    ax.set_xlabel("minutes relative to end of window")
    ax.set_ylabel(ylabel)
    ax.grid(alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def latency_chart(report) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 3.4))
    xs = _xs(report)
    for key, col, lab in (("p50_ms", GREEN, "p50"), ("p95_ms", BLUE, "p95"), ("p99_ms", ORANGE, "p99")):
        pts = [(x, p[key]) for x, p in zip(xs, report["series"]) if p[key] is not None]
        ax.plot([a for a, _ in pts], [b for _, b in pts], color=col, label=lab, lw=1.8)
    an = report["anomalies"]
    if an:
        ax.scatter([(a["t"] - report["now"]) / 60 for a in an], [a["value"] for a in an], color=RED, zorder=5,
                   label="anomaly (robust z > 3.5)")
    ax.axhline(report["apdex_t_ms"], color=GREY, ls=":", lw=1, label=f"Apdex T = {report['apdex_t_ms']:g} ms")
    ax.set_title("Latency percentiles per bucket")
    _style(ax, "latency (ms)")
    ax.legend(frameon=False, fontsize=8, ncol=5, loc="upper center", bbox_to_anchor=(0.5, -0.22))
    return _png(fig)


def error_chart(report) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 3.0))
    xs = _xs(report)
    ys = [(p["error_rate"] or 0) * 100 for p in report["series"]]
    ax.bar(xs, ys, width=(xs[1] - xs[0]) * 0.85 if len(xs) > 1 else 1, color=RED, alpha=0.85)
    ax.axhline((1 - report["slo"]["target"]) * 100, color=GREY, ls="--", lw=1,
               label=f"allowed by SLO ({(1 - report['slo']['target']) * 100:g}%)")
    ax.set_title("Error rate per bucket (HTTP 5xx or exception)")
    _style(ax, "error rate (%)")
    ax.legend(frameon=False, fontsize=8)
    return _png(fig)


def endpoint_chart(report) -> bytes:
    rows = report["endpoints"][:8][::-1]
    fig, ax = plt.subplots(figsize=(8, 0.5 * len(rows) + 1.6))
    labels = [f"{r['method']} {r['route']}" for r in rows]
    ys = range(len(rows))
    ax.barh([y + 0.2 for y in ys], [r["p50_ms"] for r in rows], height=0.38, color=GREEN, label="p50")
    ax.barh([y - 0.2 for y in ys], [r["p95_ms"] for r in rows], height=0.38,
            color=[RED if r["slow"] else BLUE for r in rows], label="p95 (red = flagged slow)")
    ax.set_yticks(list(ys), labels, fontsize=8)
    ax.axvline(report["slow_ms"], color=GREY, ls="--", lw=1)
    ax.set_title(f"Endpoints ranked by p95 (slow threshold {report['slow_ms']:g} ms)")
    ax.set_xlabel("latency (ms)")
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    return _png(fig)


def burn_chart(report) -> bytes:
    b = report["burn_series"]
    fig, ax = plt.subplots(figsize=(8, 3.0))
    xs = _xs(report)
    for key, col, lab in (("long", BLUE, f"long window ({b['long_s']:g}s)"),
                          ("short", ORANGE, f"short window ({b['short_s']:g}s)")):
        pts = [(x, v) for x, v in zip(xs, b[key]) if v is not None]
        ax.plot([a for a, _ in pts], [v for _, v in pts], color=col, label=lab, lw=1.8)
    ax.axhline(b["factor"], color=RED, ls="--", lw=1, label=f"policy factor {b['factor']:g}")
    ax.axhline(1, color=GREY, ls=":", lw=1, label="burn = 1 (on budget)")
    ax.set_title(f"Error-budget burn rate (SLO {report['slo']['target'] * 100:g}%)")
    _style(ax, "burn rate (x budget)")
    ax.legend(frameon=False, fontsize=8, ncol=2)
    return _png(fig)


def all_charts(report) -> dict[str, bytes]:
    return {"latency": latency_chart(report), "errors": error_chart(report),
            "endpoints": endpoint_chart(report) if report["endpoints"] else b"",
            "burn": burn_chart(report)}
