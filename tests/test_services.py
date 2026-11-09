import io
import json

import pytest

from apimon import alerts as AL
from apimon import db
from apimon import uptime as UP
from apimon.cli import main as cli_main
from apimon.collector import render_dashboard
from apimon.loadgen import flask_transport, run_load
from apimon.probe_app import ProbeApp, serve_in_thread
from apimon.report import build_report

from conftest import NOW, ev


def mk(app, name="p1"):
    c = app.test_client()
    r = c.post("/api/projects", json={"name": name, "apdex_t_ms": 100}, headers={"X-Admin-Token": "adm"})
    return c, r.get_json()["api_key"], r


# ---------- collector ----------
def test_project_creation_needs_admin_and_key_is_hashed(app, dbpath):
    c = app.test_client()
    assert c.post("/api/projects", json={"name": "x"}).status_code == 403
    assert c.post("/api/projects", json={"name": "x"}, headers={"X-Admin-Token": "bad"}).status_code == 403
    _, key, r = mk(app)
    assert r.status_code == 201 and key.startswith("apm_")
    raw = open(dbpath, "rb").read()
    assert key.encode() not in raw  # plaintext never stored
    assert db.hash_key(key).encode() in raw
    assert c.post("/api/projects", json={"name": "p1"}, headers={"X-Admin-Token": "adm"}).status_code == 409


def test_auth_required_and_bearer_accepted(app):
    c, key, _ = mk(app)
    assert c.get("/api/endpoints").status_code == 401
    assert c.get("/api/endpoints", headers={"X-API-Key": "apm_wrong"}).status_code == 401
    assert c.get("/api/endpoints", headers={"Authorization": f"Bearer {key}"}).status_code == 200


def test_ingest_validates_and_registers_endpoints(app):
    c, key, _ = mk(app)
    h = {"X-API-Key": key}
    good = {"events": [ev(NOW - 5, "/users/<int:id>", 200, 12.5), ev(NOW - 4, "/users/<int:id>", 500, 40)]}
    assert c.post("/api/ingest", json=good, headers=h).get_json() == {"accepted": 2}
    bad = c.post("/api/ingest", json={"events": [dict(ev(NOW), status=999)]}, headers=h)
    assert bad.status_code == 422 and bad.get_json()["error"]["code"] == "validation_error"
    assert c.post("/api/ingest", json={"events": []}, headers=h).status_code == 422
    assert c.post("/api/ingest", data="not json", headers=h).status_code == 400
    future = c.post("/api/ingest", json={"events": [ev(NOW + 10 * 86400)]}, headers=h)
    assert future.status_code == 400
    eps = c.get("/api/endpoints", headers=h).get_json()["endpoints"]
    assert eps[0]["route"] == "/users/<int:id>" and eps[0]["recorded_events"] == 2


def test_ingest_rate_limited_with_retry_after(dbpath):  # capacity 5, refill 1/s (real clock)
    from apimon.collector import create_app
    c, key, _ = mk(create_app(dbpath, admin_token="adm", rate_capacity=5, rate_refill=1))
    codes = [c.get("/api/stats", headers={"X-API-Key": key}).status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and 429 in codes
    r = c.get("/api/stats", headers={"X-API-Key": key})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert r.get_json()["error"]["code"] == "rate_limited"


def test_request_id_propagation_in_collector(app):
    c, key, _ = mk(app)
    r = c.get("/healthz", headers={"X-Request-ID": "trace-42"})
    assert r.headers["X-Request-ID"] == "trace-42"
    r = c.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert r.headers["X-Request-ID"] != "bad id with spaces" and len(r.headers["X-Request-ID"]) == 32
    e = c.get("/api/stats").get_json()
    assert e["request_id"] and e["error"]["code"] == "unauthorized"


def test_analytics_endpoints_and_dashboard(dbpath):
    from apimon.collector import create_app
    app = create_app(dbpath, admin_token="adm", clock=lambda: NOW, rate_capacity=1000, rate_refill=1000)
    c, key, _ = mk(app)
    h = {"X-API-Key": key}
    events = [ev(NOW - 599 + i * 2, "/a", 500 if i % 10 == 0 else 200, 20 + (i % 7)) for i in range(250)]
    events += [ev(NOW - 590 + i * 3, "/b", 200, 400) for i in range(60)]
    c.post("/api/ingest", json={"events": events}, headers=h)
    s = c.get("/api/stats?window=600", headers=h).get_json()
    assert s["requests"] == 310 and s["errors"] == 25
    eps = c.get("/api/endpoints/stats?window=600&slow_ms=100", headers=h).get_json()["endpoints"]
    assert eps[0]["route"] == "/b" and eps[0]["slow"] is True
    assert c.get("/api/endpoints/stats?rank_by=bogus", headers=h).status_code == 400
    assert c.get("/api/stats?window=-5", headers=h).status_code == 400
    assert "policies" in c.get("/api/slo?window=600", headers=h).get_json()
    rep = c.get("/api/report?window=600", headers=h).get_json()
    assert rep["summary"]["requests"] == 310 and len(rep["series"]) == 60
    page = c.get(f"/dashboard?window=600&key={key}")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "data:image/png;base64," in html and "/b" in html and "error budget" in html
    assert "<script" not in html


def test_alert_and_uptime_endpoints_configured(dbpath):
    from apimon.collector import create_app
    cfg = AL.load_config({"rules": [{"name": "lat", "metric": "p95_ms", "threshold": 1}]})
    srv, base = serve_in_thread()
    try:
        app = create_app(dbpath, admin_token="adm", clock=lambda: NOW, alert_config=cfg,
                         checks=[UP.CheckConfig(name="ok", url=base + "/ok")])
        c, key, _ = mk(app)
        h = {"X-API-Key": key}
        c.post("/api/ingest", json={"events": [ev(NOW - 5 + i * 0.1, lat=50) for i in range(10)]}, headers=h)
        tr = c.post("/api/alerts/evaluate", headers=h).get_json()["transitions"]
        assert tr and tr[0]["to_state"] == "firing"
        assert c.get("/api/alerts", headers=h).get_json()["rules"][0]["state"] == "firing"
        res = c.post("/api/uptime/run", headers=h).get_json()["results"]
        assert res[0]["ok"] and c.get("/api/uptime", headers=h).get_json()["checks"][0]["check_name"] == "ok"
    finally:
        srv.shutdown()


# ---------- uptime (real sockets on 127.0.0.1 only) ----------
@pytest.fixture(scope="module")
def probe():
    srv, base = serve_in_thread(ProbeApp())
    yield base
    srv.shutdown()


def test_uptime_ok_and_status_assertion(probe):
    assert UP.run_check(UP.CheckConfig(name="a", url=probe + "/ok"))["ok"]
    r = UP.run_check(UP.CheckConfig(name="b", url=probe + "/status/503", retries=0))
    assert not r["ok"] and r["status"] == 503 and "expected status 200" in r["error"] and r["attempts"] == 1
    assert UP.run_check(UP.CheckConfig(name="c", url=probe + "/status/204", expect_status=204))["ok"]
    assert UP.run_check(UP.CheckConfig(name="d", url=probe + "/ok", expect_body_contains="ok"))["ok"]
    assert not UP.run_check(UP.CheckConfig(name="e", url=probe + "/ok", expect_body_contains="zzz", retries=0))["ok"]


def test_uptime_latency_assertion_timeout_and_retry(probe):
    slow = UP.run_check(UP.CheckConfig(name="s", url=probe + "/slow?ms=120", max_latency_ms=50, retries=0))
    assert not slow["ok"] and "latency" in slow["error"] and slow["status"] == 200
    to = UP.run_check(UP.CheckConfig(name="t", url=probe + "/slow?ms=600", timeout_s=0.1, retries=1,
                                     backoff_s=0), sleep=lambda s: None)
    assert not to["ok"] and to["attempts"] == 2 and to["status"] is None
    app = ProbeApp()
    srv, base = serve_in_thread(app)  # fresh app: first /flaky call fails, second succeeds
    try:
        r = UP.run_check(UP.CheckConfig(name="f", url=base + "/flaky", retries=2, backoff_s=0))
        assert r["ok"] and r["attempts"] == 2
    finally:
        srv.shutdown()


def test_uptime_connection_refused_and_scheme_validation():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = UP.run_check(UP.CheckConfig(name="x", url=f"http://127.0.0.1:{port}/", retries=0))
    assert not r["ok"] and "ConnectionRefusedError" in r["error"] or "URLError" in r["error"]
    with pytest.raises(ValueError):
        UP.CheckConfig(name="bad", url="file:///etc/passwd")


# ---------- alerts ----------
def test_state_machine_transitions():
    f = AL.next_state
    assert f("ok", 0, False, 2) == ("ok", 0)
    assert f("ok", 0, True, 2) == ("pending", 1)
    assert f("pending", 1, True, 2) == ("firing", 2)
    assert f("pending", 1, False, 2) == ("ok", 0)
    assert f("firing", 2, True, 2) == ("firing", 3)
    assert f("firing", 3, False, 2) == ("resolved", 0)
    assert f("resolved", 0, False, 2) == ("ok", 0)
    assert f("resolved", 0, True, 1) == ("firing", 1)


def _engine(conn, project, rule_extra=None, sinks=None):
    rule = {"name": "r", "metric": "error_rate", "threshold": 0.2, "window_s": 100, "for_evaluations": 2,
            "cooldown_s": 500, "sinks": ["hook"]}
    rule.update(rule_extra or {})
    cfg = AL.load_config({"sinks": {"hook": {"type": "webhook-mock"}}, "rules": [rule]})
    sinks = AL.build_sinks(cfg)
    return AL.AlertEngine(conn, project["row"], cfg, sinks), sinks["hook"]


def test_alert_lifecycle_with_cooldown(conn, project):
    eng, hook = _engine(conn, project)
    pid = project["id"]
    states = lambda: [r["state"] for r in AL.current_states(conn, pid)]  # noqa: E731
    db.insert_events(conn, pid, [ev(1000 + i, status=500 if i % 2 == 0 else 200) for i in range(50)])  # 50% errors
    assert eng.evaluate(1050) and states() == ["pending"] and not hook.sent
    ev2 = eng.evaluate(1051)
    assert states() == ["firing"] and ev2[0]["notified"] and len(hook.sent) == 1
    assert eng.evaluate(1052) == [] and len(hook.sent) == 1  # still firing: no repeat notification
    out = eng.evaluate(1400)  # window now empty -> no data -> condition false
    assert states() == ["resolved"] and out[0]["notified"] and len(hook.sent) == 2
    eng.evaluate(1401)
    assert states() == ["ok"]
    # relapse inside cooldown (500s since first notification at 1051): pending then firing, but suppressed
    db.insert_events(conn, pid, [ev(1450 + i, status=500) for i in range(20)])
    eng.evaluate(1480)
    suppressed = eng.evaluate(1481)
    assert states() == ["firing"] and suppressed[0]["notified"] is False and "suppressed" in suppressed[0]["message"]
    assert len(hook.sent) == 2
    assert len(AL.recent_events(conn, pid, 50)) == 6


def test_alert_route_scope_min_requests_and_uptime_metric(conn, project):
    pid = project["id"]
    db.insert_events(conn, pid, [ev(100 + i, "/a", 500) for i in range(5)] + [ev(100 + i, "/b", 200) for i in range(5)])
    eng, _ = _engine(conn, project, {"route": "/b", "for_evaluations": 1})
    assert eng.evaluate(110) == []  # /b has no errors
    eng, _ = _engine(conn, project, {"name": "few", "min_requests": 100, "for_evaluations": 1})
    assert eng.evaluate(110) == []  # not enough data -> no alert
    db.save_check_results(conn, pid, [{"name": "c", "ts": 105, "ok": False, "attempts": 1, "status": None,
                                       "latency_ms": 1.0, "error": "down"}])
    eng, _ = _engine(conn, project, {"name": "up", "metric": "uptime_failure_rate", "threshold": 0.5,
                                     "for_evaluations": 1})
    assert eng.evaluate(110)[0]["to_state"] == "firing"


def test_sinks_console_file_webhook_mock(tmp_path):
    cfg = AL.load_config({"sinks": {"c": {"type": "console"}, "f": {"type": "file", "path": str(tmp_path / "a.jsonl")},
                                    "w": {"type": "webhook-mock"}},
                          "rules": [{"name": "x", "metric": "p99_ms", "threshold": 1, "sinks": ["c", "f", "w"]}]})
    buf = io.StringIO()
    sinks = AL.build_sinks(cfg, stream=buf)
    e = {"rule": "x", "to_state": "firing", "value": 2, "message": "hello"}
    for s in sinks.values():
        s.send(e)
    assert "ALERT hello" in buf.getvalue()
    assert json.loads((tmp_path / "a.jsonl").read_text())["rule"] == "x"
    assert sinks["w"].sent[0]["body"]["state"] == "firing" and sinks["w"].sent[0]["url"].endswith(".invalid/hook")


def test_alert_config_validation_rejects_bad_rules():
    with pytest.raises(ValueError):
        AL.load_config({"rules": [{"name": "x", "metric": "nonsense", "threshold": 1}]})
    with pytest.raises(ValueError):
        AL.load_config({"rules": [{"name": "x", "metric": "p95_ms", "threshold": 1, "sinks": ["missing"]}]})


# ---------- end to end ----------
def test_loadgen_through_ingest_and_report(dbpath):
    from apimon.collector import create_app
    app = create_app(dbpath, admin_token="adm", clock=lambda: NOW, rate_capacity=1000, rate_refill=1000)
    c, key, _ = mk(app, "lg")
    out = run_load(flask_transport(c), "http://x", key, n=300, duration_s=300, end_ts=NOW, seed=3, batch_size=100)
    assert out["recorder"]["written"] == 300 and out["recorder"]["flush_errors"] == 0
    conn = db.connect(dbpath)
    rep = build_report(conn, db.project_by_name(conn, "lg"), NOW, 300)
    assert rep["summary"]["requests"] == 300 and rep["endpoints"]
    assert any(e["route"] == "/api/users/<int:user_id>" for e in rep["endpoints"])
    assert "<html" in render_dashboard(rep, "test")


def test_cli_create_project_and_report(tmp_path, capsys):
    dbp = str(tmp_path / "c.db")
    assert cli_main(["create-project", "cli", "--db", dbp]) == 0
    assert "apm_" in capsys.readouterr().out
    conn = db.connect(dbp)
    db.insert_events(conn, db.project_by_name(conn, "cli")["id"], [ev(NOW - 10, lat=33.0)])
    assert cli_main(["report", "--project", "cli", "--db", dbp, "--at", str(NOW), "--window", "60"]) == 0
    assert '"p50_ms": 33.0' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli_main(["report", "--project", "nope", "--db", dbp])
