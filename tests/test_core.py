import threading

import pytest
from flask import Flask, jsonify

from apimon import analytics as A
from apimon import db
from apimon.middleware import MonitorMiddleware, Recorder, SQLiteSink, instrument_flask, normalize_path
from apimon.ratelimit import RateLimiter, TokenBucket

from conftest import NOW, FakeClock, ev


class ListSink:
    def __init__(self):
        self.batches = []

    def write(self, events):
        self.batches.append(list(events))

    @property
    def events(self):
        return [e for b in self.batches for e in b]


def make_app(rec, **kw):
    app = Flask("t")

    @app.get("/items/<int:i>")
    def item(i):
        return jsonify({"id": i})

    @app.post("/echo")
    def echo():
        return "x" * 50

    @app.get("/boom")
    def boom():
        raise RuntimeError("no")

    instrument_flask(app, rec, **kw)
    return app


def test_middleware_records_route_template_status_sizes():
    sink = ListSink()
    rec = Recorder(sink, batch_size=1, flush_interval=0, autostart=False)
    c = make_app(rec).test_client()
    r = c.post("/echo", data=b"12345")
    r.get_data()
    r.close()
    c.get("/items/42").get_data()
    c.get("/nope").get_data()
    rec.close()
    by = {(e["method"], e["route"]): e for e in sink.events}
    assert by[("POST", "/echo")]["request_bytes"] == 5
    assert by[("POST", "/echo")]["response_bytes"] == 50
    assert ("GET", "/items/<int:i>") in by  # template, not /items/42
    assert by[("GET", "/items/<int:i>")]["status"] == 200
    assert by[("GET", "<unmatched>")]["status"] == 404
    assert all(e["latency_ms"] >= 0 for e in sink.events)


def test_middleware_records_exceptions_as_errors():
    sink = ListSink()
    rec = Recorder(sink, batch_size=1, flush_interval=0, autostart=False)
    r = make_app(rec).test_client().get("/boom")
    r.get_data()
    rec.close()
    e = [x for x in sink.events if x["route"] == "/boom"][0]
    assert e["status"] == 500 and e["error"] == "RuntimeError"


def test_raw_wsgi_exception_is_recorded_and_reraised():
    sink = ListSink()
    rec = Recorder(sink, batch_size=1, flush_interval=0, autostart=False)

    def bad(environ, sr):
        raise KeyError("x")

    mw = MonitorMiddleware(bad, rec)
    with pytest.raises(KeyError):
        mw({"REQUEST_METHOD": "GET", "PATH_INFO": "/users/17"}, lambda *a: None)
    assert sink.events[0]["error"] == "KeyError"
    assert sink.events[0]["route"] == "/users/{id}"  # fallback normalisation


def test_normalize_path():
    assert normalize_path("/a/123/b/550e8400-e29b-41d4-a716-446655440000") == "/a/{id}/b/{id}"


def test_request_id_propagated_to_app_and_response():
    rec = Recorder(ListSink(), flush_interval=0, autostart=False)
    seen = {}

    def inner(environ, sr):
        seen["rid"] = environ["HTTP_X_REQUEST_ID"]
        sr("200 OK", [("Content-Length", "0")])
        return [b""]

    hdrs = {}
    mw = MonitorMiddleware(inner, rec)
    list(mw({"REQUEST_METHOD": "GET", "PATH_INFO": "/", "HTTP_X_REQUEST_ID": "abc-1"},
            lambda s, h, e=None: hdrs.update(dict(h))))
    assert seen["rid"] == "abc-1" and hdrs["X-Request-ID"] == "abc-1"
    list(mw({"REQUEST_METHOD": "GET", "PATH_INFO": "/"}, lambda s, h, e=None: hdrs.update(dict(h))))
    assert len(hdrs["X-Request-ID"]) == 32  # generated


def test_sampling_is_deterministic_and_recorded():
    sink = ListSink()
    seq = iter([0.1, 0.9, 0.2, 0.8])
    rec = Recorder(sink, batch_size=100, flush_interval=0, sample_rate=0.5, rng=lambda: next(seq), autostart=False)
    for i in range(4):
        rec.record(ev(i))
    rec.close()
    assert len(sink.events) == 2 and rec.stats["sampled_out"] == 2
    assert all(e["sample_rate"] == 0.5 for e in sink.events)
    with pytest.raises(ValueError):
        Recorder(sink, sample_rate=1.5)


def test_recorder_thread_safe_batched_writes_to_sqlite(dbpath, conn, project):
    rec = Recorder(SQLiteSink(dbpath, project["id"]), batch_size=25, flush_interval=0.05)

    def worker(k):
        for i in range(100):
            rec.record(ev(NOW + k * 1000 + i, route=f"/r{k % 3}"))

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rec.close()
    n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert n == 800 and rec.stats["written"] == 800 and rec.stats["dropped"] == 0
    assert conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 3


def test_recorder_requeues_when_sink_fails():
    class Flaky(ListSink):
        fail = True

        def write(self, events):
            if self.fail:
                raise OSError("down")
            super().write(events)

    s = Flaky()
    rec = Recorder(s, batch_size=100, flush_interval=0, autostart=False)
    rec.record(ev(1))
    assert rec.flush() == 0 and rec.stats["flush_errors"] == 1
    s.fail = False
    assert rec.flush() == 1


def test_token_bucket_refill_and_deny():
    clk = FakeClock(100)
    b = TokenBucket(3, 1.0, clk)
    assert [b.allow()[0] for _ in range(3)] == [True] * 3
    ok, retry = b.allow()
    assert not ok and retry == pytest.approx(1.0)
    clk.t += 2.5
    assert b.allow()[0] and b.allow()[0] and not b.allow()[0]
    clk.t += 1000
    assert b.tokens <= 3  # never above capacity
    with pytest.raises(ValueError):
        TokenBucket(0, 1)


def test_rate_limiter_keys_are_independent():
    clk = FakeClock()
    rl = RateLimiter(1, 0.1, clk)
    assert rl.allow("a")[0] and not rl.allow("a")[0] and rl.allow("b")[0]


def test_percentiles_error_rate_and_apdex():
    import pandas as pd
    rows = [ev(i, lat=float(i + 1)) for i in range(100)]  # latencies 1..100
    rows[0].update(status=500)
    rows[1].update(error="Boom")
    df = pd.DataFrame(rows)
    s = A.summary(df, 100, apdex_t_ms=50)
    assert s["p50_ms"] == pytest.approx(50.5) and s["p95_ms"] == pytest.approx(95.05)
    assert s["errors"] == 2 and s["error_rate"] == 0.02 and s["throughput_rps"] == 1.0
    # satisfied: lat<=50 and not error -> 48 ; tolerating: 51..100 (<=200) -> 50 ; apdex=(48+25)/100
    assert s["apdex"] == pytest.approx(0.73)


def test_sampling_weights_keep_rates_unbiased():
    import pandas as pd
    df = pd.DataFrame([ev(i, status=500 if i < 5 else 200, rate=0.1) for i in range(50)])
    s = A.summary(df, 100, 100)
    assert s["requests"] == 500 and s["errors"] == 50 and s["error_rate"] == 0.1


def test_per_endpoint_ranking_and_slow_detection():
    import pandas as pd
    rows = [ev(i, route="/fast", lat=5) for i in range(30)] + [ev(i, route="/slow", lat=120) for i in range(30)]
    rows += [ev(i, route="/tiny", lat=999) for i in range(3)]
    out = A.per_endpoint(pd.DataFrame(rows), 100, slow_ms=50)
    assert [r["route"] for r in out] == ["/tiny", "/slow", "/fast"]
    flags = {r["route"]: r["slow"] for r in out}
    assert flags == {"/tiny": False, "/slow": True, "/fast": False}  # /tiny below min_requests
    assert A.per_endpoint(pd.DataFrame(rows), 100, 50, rank_by="requests")[-1]["route"] == "/tiny"


def test_slo_budget_and_multiwindow_burn():
    import pandas as pd
    # 1000 requests over the last 1000s; the last 300s have 20% errors, before that none
    rows = [ev(NOW - 1000 + i + 1, status=500 if i >= 700 and i % 5 == 0 else 200) for i in range(1000)]
    df = pd.DataFrame(rows)
    pol = [{"name": "fast", "long_s": 400, "short_s": 100, "factor": 14.4, "severity": "page"}]
    r = A.slo_report(df, NOW, 0.99, 1000, pol)
    assert r["bad"] == 60 and r["budget_spent"] == 6.0 and r["budget_remaining"] == -5.0
    p = r["policies"][0]
    assert p["burn_short"] == pytest.approx(20.0) and p["firing"] is True  # 20% / 1% = 20x
    healthy = A.slo_report(df[df["ts"] < NOW - 400], NOW - 400, 0.99, 600, pol)
    assert healthy["policies"][0]["firing"] is False and healthy["budget_remaining"] == 1.0


def test_robust_z_detects_spike_and_handles_zero_mad():
    z = A.robust_z([10, 11, 9, 10, 10, 11, 9, 200])
    assert z[-1] > 3.5 and max(abs(z[:-1])) < 3.5
    z0 = A.robust_z([5, 5, 5, 5, 5, 5, 50])  # MAD == 0 fallback
    assert z0[-1] > 3.5 and A.robust_z([3, 3, 3]).tolist() == [0, 0, 0]


def test_detect_anomalies_on_bucket_series():
    series = [{"t": i, "count": 20, "p95_ms": 10 + (i % 3), "p50_ms": 5, "p99_ms": 20, "rps": 1, "error_rate": 0}
              for i in range(20)]
    series[12]["p95_ms"] = 400
    assert [a["t"] for a in A.detect_anomalies(series)] == [12]
    assert A.detect_anomalies(series[:5]) == []  # too few points
