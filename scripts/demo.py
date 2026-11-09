"""End-to-end demo: load generator -> middleware -> collector ingestion -> analytics -> alerts -> uptime -> dashboard.

All traffic is SYNTHETIC. Request latencies are measured from real handler execution; only event timestamps follow
a virtual clock so 12 minutes of traffic replay in seconds. Writes PNGs and a sample HTML dashboard into docs/.
"""
import json
import logging
import os
import secrets
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402

from apimon import alerts as AL  # noqa: E402
from apimon import analytics as A  # noqa: E402
from apimon import charts, db  # noqa: E402
from apimon import uptime as UP  # noqa: E402
from apimon.collector import create_app  # noqa: E402
from apimon.loadgen import flask_transport, run_load  # noqa: E402
from apimon.middleware import MonitorMiddleware, Recorder  # noqa: E402
from apimon.probe_app import ProbeApp, serve_in_thread  # noqa: E402
from apimon.report import build_report  # noqa: E402

BUILD, DOCS = os.path.join(ROOT, "build"), os.path.join(ROOT, "docs")
POLICIES = [  # demo-scale windows (real deployments would use e.g. 1h/5m and 6h/30m)
    {"name": "fast", "long_s": 120, "short_s": 30, "factor": 6.0, "severity": "page"},
    {"name": "slow", "long_s": 360, "short_s": 60, "factor": 3.0, "severity": "ticket"},
]


def overhead_us(n=1500):
    """Measured per-request cost of the middleware on a trivial Flask route (test client, same process)."""
    def run(wrap):
        app = Flask("o")

        @app.get("/x")
        def x():
            return "ok"

        if wrap:
            class Null:
                def write(self, e):
                    pass
            rec = Recorder(Null(), batch_size=500, flush_interval=0, autostart=False)
            app.wsgi_app = MonitorMiddleware(app.wsgi_app, rec)
        c = app.test_client()
        for _ in range(100):
            c.get("/x")
        t = time.perf_counter()
        for _ in range(n):
            c.get("/x").get_data()
        return (time.perf_counter() - t) / n * 1e6
    base = min(run(False) for _ in range(3))
    wrapped = min(run(True) for _ in range(3))
    return base, wrapped


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # silence per-request logs of the local probe
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)
    os.makedirs(DOCS, exist_ok=True)
    srv, base = serve_in_thread(ProbeApp())
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    checks = UP.load_checks([
        {"name": "ok", "url": base + "/ok", "max_latency_ms": 500},
        {"name": "flaky-retry", "url": base + "/flaky", "retries": 2, "backoff_s": 0.05},
        {"name": "slow-vs-100ms", "url": base + "/slow?ms=150", "max_latency_ms": 100, "retries": 0},
        {"name": "wrong-status", "url": base + "/status/503", "retries": 1, "backoff_s": 0.05},
        {"name": "refused", "url": f"http://127.0.0.1:{dead_port}/", "retries": 1, "backoff_s": 0.05},
    ])
    admin = secrets.token_hex(8)
    dbp = os.path.join(BUILD, "demo.db")
    app = create_app(dbp, admin_token=admin, rate_capacity=100, rate_refill=100, policies=POLICIES, checks=checks)
    c = app.test_client()
    key = c.post("/api/projects", json={"name": "demo-shop", "slo_target": 0.99, "apdex_t_ms": 25},
                 headers={"X-Admin-Token": admin}).get_json()["api_key"]

    print("== 1. load generator (synthetic traffic, real measured latencies) ==")
    out = run_load(flask_transport(c), "http://collector.local", key, n=2400, duration_s=720)
    print(json.dumps({k: out[k] for k in ("requests_sent", "wall_seconds", "achieved_rps", "status_counts",
                                          "recorder")}, indent=2))
    end = out["virtual_end"]
    conn = db.connect(dbp)
    project = db.project_by_name(conn, "demo-shop")

    print("\n== 2. uptime checks against a local probe app on 127.0.0.1 ==")
    for r in c.post("/api/uptime/run", headers={"X-API-Key": key}).get_json()["results"]:
        print(f"  {r['name']:<14} {'UP  ' if r['ok'] else 'DOWN'} status={r['status']} latency={r['latency_ms']}ms "
              f"attempts={r['attempts']} {r['error'] or ''}")
    srv.shutdown()

    print("\n== 3. alert rules replayed over the virtual timeline (evaluated every 15s) ==")
    cfg = AL.load_config(os.path.join(ROOT, "examples", "alerts.json"))
    cfg.sinks["file"]["path"] = os.path.join(BUILD, "alerts.jsonl")
    engine = AL.AlertEngine(conn, project, cfg)
    t0 = out["virtual_start"]
    t = t0 + 60
    while t <= end:
        for e in engine.evaluate(t):
            if not e["notified"] and e["to_state"] in ("firing", "resolved"):
                print(f"  (t+{t - t0:5.0f}s) {e['message']}")
        t += 15
    webhook = engine.sinks["hook"]
    print(f"  webhook-mock payloads captured (no network): {len(webhook.sent)}")
    print(f"  alert transitions stored: {len(AL.recent_events(conn, project['id'], 500))}; "
          f"file sink lines: {sum(1 for _ in open(os.path.join(BUILD, 'alerts.jsonl')))}")

    print("\n== 4. analytics over the full 720s window ==")
    rep = build_report(conn, project, end, 720, 12, POLICIES)
    s = rep["summary"]
    print(f"  requests={s['requests']} errors={s['errors']} error_rate={s['error_rate']:.4f} rps={s['throughput_rps']} "
          f"p50/p95/p99={s['p50_ms']}/{s['p95_ms']}/{s['p99_ms']} ms apdex(T=25ms)={s['apdex']}")
    print("  endpoints ranked by p95:")
    for e in rep["endpoints"]:
        print(f"    {e['method']:<4} {e['route']:<26} n={e['requests']:<4} p50={e['p50_ms']:<6} p95={e['p95_ms']:<7} "
              f"err={e['error_rate']:.3f} slow={e['slow']}")
    df = db.load_requests(conn, project["id"], t0 - 1, end)
    sp = df[(df.route == "/api/search")]
    in_spike = sp[(sp.ts >= t0 + 0.45 * 720) & (sp.ts < t0 + 0.55 * 720)]
    out_spike = sp.drop(in_spike.index)
    od = df[df.route == "/api/orders"]
    in_burst = od[(od.ts >= t0 + 0.70 * 720) & (od.ts < t0 + 0.78 * 720)]
    out_burst = od.drop(in_burst.index)
    print(f"  /api/search p95 outside spike {out_spike.latency_ms.quantile(.95):.1f} ms vs inside {in_spike.latency_ms.quantile(.95):.1f} ms")
    print(f"  /api/orders error rate outside burst {A.error_mask(out_burst).mean():.3f} vs inside {A.error_mask(in_burst).mean():.3f}")
    print("  anomalous buckets (robust z > 3.5), seconds from start:",
          [round(a["t"] - t0) for a in rep["anomalies"]], "(spike injected 324s-396s, orders burst 504s-562s)")
    for p in rep["slo"]["policies"]:
        print(f"  burn policy {p['name']}: long={p['burn_long']} short={p['burn_short']} (factor {p['factor']}) firing={p['firing']} at end")
    peak = max(v for v in rep["burn_series"]["short"] if v is not None)
    print(f"  peak 30s burn rate over timeline: {peak}; budget remaining over window: {rep['slo']['budget_remaining']}")

    print("\n== 5. middleware overhead (trivial route, in-process, best of 3) ==")
    b, w = overhead_us()
    print(f"  without: {b:.0f} us/request   with middleware: {w:.0f} us/request   delta: {w - b:.0f} us")

    print("\n== 6. artifacts ==")
    rep = build_report(conn, project, end, 720, 12, POLICIES)  # re-read so alert states are included
    names = {"latency": "latency_percentiles.png", "errors": "error_rate.png", "endpoints": "endpoint_ranking.png",
             "burn": "slo_burn_rate.png"}
    for k, png in charts.all_charts(rep).items():
        with open(os.path.join(DOCS, names[k]), "wb") as fh:
            fh.write(png)
        print("  wrote docs/" + names[k], f"({len(png) // 1024} KiB)")
    page = c.get(f"/dashboard?window=720&bucket=12&at={end}", headers={"X-API-Key": key})
    assert page.status_code == 200
    html = page.get_data(as_text=True).replace("synthetic or user-supplied events stored in the local SQLite database",
                                               "SYNTHETIC demo traffic (scripts/demo.py)")
    with open(os.path.join(DOCS, "sample_dashboard.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  wrote docs/sample_dashboard.html ({len(html) // 1024} KiB)")
    shutil.rmtree(BUILD, ignore_errors=True)  # scratch db / alert file


if __name__ == "__main__":
    main()
