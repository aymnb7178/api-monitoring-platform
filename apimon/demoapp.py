"""Instrumentable demo API. Handlers do small real work plus a sleep to stand in for I/O, and obey a mutable
`faults` table so the load generator can inject latency spikes and error bursts."""
from __future__ import annotations

import random
import time

from flask import Flask, abort, jsonify, request

BASE_MS = {"users_list": 3.0, "user_get": 2.0, "orders_create": 6.0, "search": 8.0, "report": 16.0, "health": 0.2}


def create_demo_app(seed: int = 1) -> Flask:
    app = Flask("demo_api")
    app.logger.disabled = True  # injected failures are expected; keep the demo output readable
    rng = random.Random(seed)
    faults: dict[str, dict] = {}
    app.config["FAULTS"] = faults

    def work(name: str):
        f = faults.get(name, {})
        ms = BASE_MS[name] * rng.lognormvariate(0, 0.25) + f.get("extra_ms", 0.0) * rng.uniform(0.8, 1.2)
        time.sleep(ms / 1000.0)
        if rng.random() < f.get("raise_p", 0.0):
            raise RuntimeError(f"injected failure in {name}")
        if rng.random() < f.get("status_p", 0.0):
            abort(503)

    @app.get("/api/users")
    def users_list():
        work("users_list")
        return jsonify([{"id": i, "name": f"user{i}"} for i in range(20)])

    @app.get("/api/users/<int:user_id>")
    def user_get(user_id):
        work("user_get")
        if user_id > 900:
            abort(404)
        return jsonify({"id": user_id, "name": f"user{user_id}"})

    @app.post("/api/orders")
    def orders_create():
        work("orders_create")
        return jsonify({"order_id": rng.randint(1000, 9999), "items": len(request.get_json(silent=True) or {})}), 201

    @app.get("/api/search")
    def search():
        work("search")
        return jsonify({"q": request.args.get("q", ""), "results": list(range(10))})

    @app.get("/api/reports/<name>")
    def report(name):
        work("report")
        return jsonify({"report": name, "rows": 250})

    @app.get("/health")
    def health():
        work("health")
        return jsonify({"status": "ok"})

    return app
