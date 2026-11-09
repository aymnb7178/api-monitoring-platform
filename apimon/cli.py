"""argparse CLI: init-db, create-project, serve, check, report, evaluate, loadgen."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from . import alerts as AL
from . import db
from . import uptime as UP
from .report import build_report


def _project(conn, name):
    p = db.project_by_name(conn, name)
    if p is None:
        raise SystemExit(f"error: no project named {name!r} (run create-project first)")
    return p


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="apimon", description="API monitoring toolkit")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = argparse.ArgumentParser(add_help=False)
    d.add_argument("--db", default="build/apimon.db")

    sub.add_parser("init-db", parents=[d])
    c = sub.add_parser("create-project", parents=[d])
    c.add_argument("name")
    c.add_argument("--slo", type=float, default=0.99)
    c.add_argument("--apdex-t", type=float, default=300.0, help="Apdex threshold T in ms")

    s = sub.add_parser("serve", parents=[d])
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--alerts", help="alert rules JSON")
    s.add_argument("--checks", help="uptime checks JSON")

    k = sub.add_parser("check", help="run uptime checks from a JSON file")
    k.add_argument("--config", required=True)
    k.add_argument("--db")
    k.add_argument("--project")

    r = sub.add_parser("report", parents=[d])
    r.add_argument("--project", required=True)
    r.add_argument("--window", type=float, default=3600)
    r.add_argument("--at", type=float, help="evaluate at this epoch (default: now)")

    e = sub.add_parser("evaluate", parents=[d])
    e.add_argument("--project", required=True)
    e.add_argument("--alerts", required=True)
    e.add_argument("--at", type=float)

    lg = sub.add_parser("loadgen", parents=[d])
    lg.add_argument("--project", required=True)
    lg.add_argument("--requests", type=int, default=2400)
    lg.add_argument("--duration", type=float, default=720.0, help="virtual seconds the traffic spans")
    lg.add_argument("--seed", type=int, default=7)
    lg.add_argument("--sample-rate", type=float, default=1.0)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if a.cmd == "check":
        results = UP.run_checks(UP.load_checks(json.load(open(a.config, encoding="utf-8"))))
        if a.db and a.project:
            conn = db.connect(a.db)
            db.init_db(conn)
            db.save_check_results(conn, _project(conn, a.project)["id"], results)
        print(json.dumps(results, indent=2))
        return 0 if all(r["ok"] for r in results) else 1
    if hasattr(a, "db"):
        os.makedirs(os.path.dirname(os.path.abspath(a.db)), exist_ok=True)
        conn = db.connect(a.db)
        db.init_db(conn)
    if a.cmd == "init-db":
        print(f"initialised {a.db}")
    elif a.cmd == "create-project":
        try:
            _, key = db.create_project(conn, a.name, a.slo, a.apdex_t)
        except Exception as ex:
            raise SystemExit(f"error: {ex}")
        conn.commit()
        print(f"project {a.name!r} created. API key (shown once, only its hash is stored):\n{key}")
    elif a.cmd == "serve":
        from .collector import create_app
        cfg = AL.load_config(a.alerts) if a.alerts else None
        checks = UP.load_checks(json.load(open(a.checks, encoding="utf-8"))) if a.checks else None
        app = create_app(a.db, admin_token=os.environ.get("APIMON_ADMIN_TOKEN"), alert_config=cfg, checks=checks)
        app.run(host=a.host, port=a.port, threaded=True)
    elif a.cmd == "report":
        rep = build_report(conn, _project(conn, a.project), a.at or time.time(), a.window)
        for k in ("series", "burn_series"):
            rep.pop(k)
        print(json.dumps(rep, indent=2))
    elif a.cmd == "evaluate":
        p = _project(conn, a.project)
        for ev in AL.AlertEngine(conn, p, AL.load_config(a.alerts)).evaluate(a.at):
            print(json.dumps(ev))
    elif a.cmd == "loadgen":
        from .collector import create_app
        from .loadgen import flask_transport, run_load
        p = _project(conn, a.project)
        key = db.new_api_key(conn, p["id"])
        client = create_app(a.db, rate_capacity=1000, rate_refill=1000).test_client()
        print(json.dumps(run_load(flask_transport(client), "http://collector.local", key, a.requests, a.duration,
                                  seed=a.seed, sample_rate=a.sample_rate), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
