"""Tiny local WSGI target for uptime checks (tests and demo bind it to 127.0.0.1 only)."""
from __future__ import annotations

import threading
import time

from werkzeug.serving import make_server


class ProbeApp:
    def __init__(self):
        self.flaky_failures_left = 1
        self.lock = threading.Lock()

    def __call__(self, environ, start_response):
        path = environ["PATH_INFO"]
        qs = environ.get("QUERY_STRING", "")
        status, body = "200 OK", b"ok"
        if path == "/slow":
            ms = float(dict(p.split("=") for p in qs.split("&") if "=" in p).get("ms", 150))
            time.sleep(ms / 1000)
        elif path == "/flaky":
            with self.lock:
                if self.flaky_failures_left > 0:
                    self.flaky_failures_left -= 1
                    status, body = "503 Service Unavailable", b"warming up"
        elif path.startswith("/status/"):
            code = int(path.rsplit("/", 1)[1])
            status, body = f"{code} Status", b"status"
        elif path != "/ok":
            status, body = "404 Not Found", b"nope"
        start_response(status, [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
        return [body]


def serve_in_thread(app=None):
    """Start the probe app on 127.0.0.1 with an OS-assigned port. Returns (server, base_url)."""
    server = make_server("127.0.0.1", 0, app or ProbeApp(), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
