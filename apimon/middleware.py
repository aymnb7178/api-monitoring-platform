"""Embeddable WSGI middleware: records one event per request into a batching Recorder."""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import urllib.request
import uuid

from . import db

log = logging.getLogger("apimon.middleware")
_RID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ID_SEG = re.compile(r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|[0-9a-fA-F]{24,})$")


def normalize_path(path: str) -> str:
    """Fallback route template when the framework does not provide one: numeric/uuid/hex segments -> {id}."""
    segs = [("{id}" if _ID_SEG.match(s) else s) for s in path.split("/")]
    return "/".join(segs) or "/"


class SQLiteSink:
    """Writes batches straight into the collector database (same-process use)."""

    def __init__(self, db_path: str, project_id: int):
        self.conn = db.connect(db_path)
        db.init_db(self.conn)
        self.project_id = project_id

    def write(self, events: list[dict]) -> None:
        db.insert_events(self.conn, self.project_id, events)


class HTTPSink:
    """POSTs batches to a collector's /api/ingest. `transport(url, headers, body)` returns the HTTP
    status and can be replaced (tests and the demo use the Flask test client, so no sockets are needed)."""

    def __init__(self, base_url: str, api_key: str, transport=None, timeout: float = 3.0):
        self.url = base_url.rstrip("/") + "/api/ingest"
        self.headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        self.transport = transport or self._urllib
        self.timeout = timeout

    def _urllib(self, url, headers, body):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 (user-configured URL)
            return r.status

    def write(self, events: list[dict]) -> None:
        for i in range(0, len(events), 1000):  # ingest accepts at most 1000 events per call
            body = json.dumps({"events": events[i:i + 1000]}).encode()
            status = self.transport(self.url, self.headers, body)
            if status >= 400:
                raise RuntimeError(f"collector rejected batch: HTTP {status}")


class Recorder:
    """Thread-safe buffer with sampling and batched flushes (by size and by a background interval)."""

    def __init__(self, sink, batch_size=100, flush_interval=2.0, sample_rate=1.0, max_buffer=10000,
                 rng=random.random, autostart=True):
        if not 0 <= sample_rate <= 1:
            raise ValueError("sample_rate must be within [0, 1]")
        self.sink, self.batch_size, self.sample_rate = sink, batch_size, sample_rate
        self.max_buffer, self.rng, self.flush_interval = max_buffer, rng, flush_interval
        self._buf: list[dict] = []
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.stats = {"seen": 0, "sampled_out": 0, "written": 0, "dropped": 0, "flush_errors": 0}
        if autostart and flush_interval:
            self._thread = threading.Thread(target=self._loop, name="apimon-flush", daemon=True)
            self._thread.start()

    def record(self, event: dict) -> None:
        with self._lock:
            self.stats["seen"] += 1
            if self.sample_rate < 1 and self.rng() >= self.sample_rate:
                self.stats["sampled_out"] += 1
                return
            event["sample_rate"] = self.sample_rate
            self._buf.append(event)
            if len(self._buf) > self.max_buffer:
                del self._buf[0]
                self.stats["dropped"] += 1
            full = len(self._buf) >= self.batch_size
        if full:
            self.flush()

    def flush(self) -> int:
        with self._flush_lock:
            with self._lock:
                batch, self._buf = self._buf, []
            if not batch:
                return 0
            try:
                self.sink.write(batch)
            except Exception:  # keep monitoring failures away from the host app
                log.exception("flush failed; re-queueing %d events", len(batch))
                with self._lock:
                    self._buf = (batch + self._buf)[-self.max_buffer:]
                    self.stats["flush_errors"] += 1
                return 0
            with self._lock:
                self.stats["written"] += len(batch)
            return len(batch)

    def _loop(self):
        while not self._stop.wait(self.flush_interval):
            self.flush()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.flush()


class _Body:
    """Wraps the response iterable to count bytes and finish timing once it is exhausted/closed."""

    def __init__(self, result, finish):
        self.result, self.finish, self.nbytes, self._done = result, finish, 0, False

    def __iter__(self):
        try:
            for chunk in self.result:
                self.nbytes += len(chunk)
                yield chunk
        finally:
            self._finish()

    def _finish(self):
        if not self._done:
            self._done = True
            self.finish(self.nbytes)

    def close(self):
        try:
            if hasattr(self.result, "close"):
                self.result.close()
        finally:
            self._finish()


class MonitorMiddleware:
    def __init__(self, app, recorder: Recorder, clock=time.time, exclude_prefixes=(), route_fn=None):
        self.app, self.recorder, self.clock = app, recorder, clock
        self.exclude, self.route_fn = tuple(exclude_prefixes), route_fn

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.exclude and path.startswith(self.exclude):
            return self.app(environ, start_response)
        rid = environ.get("HTTP_X_REQUEST_ID", "")
        if not _RID.match(rid):
            rid = uuid.uuid4().hex
        environ["HTTP_X_REQUEST_ID"] = rid  # propagate to the wrapped app
        t0, ts = time.perf_counter(), self.clock()
        state = {"status": 500}

        def sr(status, headers, exc_info=None):
            state["status"] = int(status.split(" ", 1)[0])
            if not any(k.lower() == "x-request-id" for k, _ in headers):
                headers = list(headers) + [("X-Request-ID", rid)]
            return start_response(status, headers, exc_info)

        def finish(nbytes, error=None):
            try:
                try:
                    req_bytes = max(0, int(environ.get("CONTENT_LENGTH") or 0))
                except ValueError:
                    req_bytes = 0
                route = (self.route_fn(environ) if self.route_fn else None) or environ.get("apimon.route") \
                    or normalize_path(path)
                self.recorder.record({
                    "ts": ts, "method": environ.get("REQUEST_METHOD", "GET").upper(), "route": route,
                    "status": state["status"], "latency_ms": (time.perf_counter() - t0) * 1000.0,
                    "request_bytes": req_bytes, "response_bytes": nbytes,
                    "error": error or environ.get("apimon.error"), "request_id": rid})
            except Exception:
                log.exception("failed to record request")

        try:
            result = self.app(environ, sr)
        except Exception as exc:
            finish(0, error=type(exc).__name__)
            raise
        return _Body(result, finish)


def instrument_flask(app, recorder: Recorder, **kw) -> Recorder:
    """Wrap a Flask app: records the matched URL rule as the route template and exception types."""
    from flask import got_request_exception, request

    @app.after_request
    def _route(resp):
        rule = request.url_rule.rule if request.url_rule is not None else None
        request.environ["apimon.route"] = rule or "<unmatched>"
        return resp

    def _err(sender, exception, **extra):
        request.environ["apimon.error"] = type(exception).__name__

    got_request_exception.connect(_err, app, weak=False)
    app.wsgi_app = MonitorMiddleware(app.wsgi_app, recorder, **kw)
    return recorder
