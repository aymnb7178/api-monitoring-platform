"""Load generator: drives the instrumented demo app with a weighted request mix, an injected latency spike
and an injected error burst. Latencies are measured from real handler execution through the real middleware.
Only the event *timestamps* follow a virtual clock so minutes of traffic can be replayed in seconds."""
from __future__ import annotations

import json
import random
import time
from collections import Counter

from .demoapp import create_demo_app
from .middleware import HTTPSink, Recorder, instrument_flask

MIX = [  # (weight, method, path template, json body)
    (20, "GET", "/api/users", None),
    (35, "GET", "/api/users/{uid}", None),
    (15, "POST", "/api/orders", {"sku": "demo", "qty": 1}),
    (15, "GET", "/api/search?q={word}", None),
    (8, "GET", "/api/reports/{name}", None),
    (7, "GET", "/health", None),
]
WORDS = ["alpha", "beta", "gamma", "delta", "router", "cache"]


class VirtualClock:
    def __init__(self, start: float):
        self.t = start

    def __call__(self):
        return self.t


def run_load(transport, base_url: str, api_key: str, n: int = 2400, duration_s: float = 720.0,
             end_ts: float | None = None, seed: int = 7, spike=(0.45, 0.55), burst=(0.70, 0.78),
             spike_extra_ms: float = 60.0, burst_error_p: float = 0.6, sample_rate: float = 1.0,
             batch_size: int = 200) -> dict:
    """Send n requests. Returns measured stats (wall time, status mix, recorder counters)."""
    rng = random.Random(seed)
    end_ts = end_ts if end_ts is not None else time.time()
    clock = VirtualClock(end_ts - duration_s)
    app = create_demo_app(seed)
    sink = HTTPSink(base_url, api_key, transport=transport)
    rec = Recorder(sink, batch_size=batch_size, flush_interval=0, sample_rate=sample_rate,
                   rng=random.Random(seed + 1).random, autostart=False)
    instrument_flask(app, rec, clock=clock)
    client = app.test_client()
    weights = [m[0] for m in MIX]
    statuses: Counter = Counter()
    faults = app.config["FAULTS"]
    t0 = time.perf_counter()
    for i in range(n):
        f = i / n
        faults.clear()
        if spike[0] <= f < spike[1]:
            faults["search"] = {"extra_ms": spike_extra_ms}
        if burst[0] <= f < burst[1]:
            faults["orders_create"] = {"raise_p": burst_error_p * 0.5, "status_p": burst_error_p * 0.5}
        clock.t = end_ts - duration_s + (i + rng.random()) * (duration_s / n)
        _, method, tpl, body = rng.choices(MIX, weights)[0]
        path = tpl.format(uid=rng.randint(1, 940), word=rng.choice(WORDS), name=rng.choice(["daily", "weekly"]))
        resp = client.open(path, method=method, json=body, headers={"X-Request-ID": f"lg-{seed}-{i}"})
        resp.get_data()
        resp.close()
        statuses[resp.status_code] += 1
    rec.close()
    wall = time.perf_counter() - t0
    return {"requests_sent": n, "wall_seconds": round(wall, 2), "achieved_rps": round(n / wall, 1),
            "virtual_start": end_ts - duration_s, "virtual_end": end_ts,
            "status_counts": dict(sorted(statuses.items())), "recorder": dict(rec.stats),
            "spike": {"route": "/api/search", "extra_ms": spike_extra_ms, "fraction": list(spike)},
            "burst": {"route": "/api/orders", "error_p": burst_error_p, "fraction": list(burst)}}


def flask_transport(client):
    """Adapter so HTTPSink posts into a Flask test client instead of a socket."""
    def send(url, headers, body):
        path = "/api/ingest"
        return client.post(path, data=body, headers=headers).status_code
    return send


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps({"hint": "use `python -m apimon loadgen`"}))
