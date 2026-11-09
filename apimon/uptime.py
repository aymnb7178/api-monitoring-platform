"""Active uptime checker: probe URLs with timeout, retry and status/latency/body assertions."""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class CheckConfig(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str
    method: str = "GET"
    expect_status: int = Field(200, ge=100, le=599)
    max_latency_ms: float = Field(1000, gt=0)
    timeout_s: float = Field(2.0, gt=0, le=60)
    retries: int = Field(1, ge=0, le=5)
    backoff_s: float = Field(0.1, ge=0, le=10)
    expect_body_contains: str | None = None

    @field_validator("url")
    @classmethod
    def _http_only(cls, v):
        if urlparse(v).scheme not in ("http", "https"):
            raise ValueError("url must be http(s)")
        return v

    @field_validator("method")
    @classmethod
    def _method(cls, v):
        v = v.upper()
        if v not in ("GET", "HEAD"):
            raise ValueError("only GET and HEAD are supported")
        return v


def _attempt(cfg: CheckConfig):
    req = urllib.request.Request(cfg.url, method=cfg.method, headers={"User-Agent": "apimon-checker/0.1"})
    t0 = time.perf_counter()
    status, body, err = None, b"", None
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_s) as r:  # noqa: S310 (operator-configured URL)
            status, body = r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read(65536)
    except Exception as e:  # connection refused, timeout, DNS, ...
        err = f"{type(e).__name__}: {e}"
    latency = (time.perf_counter() - t0) * 1000.0
    if err is None:
        if status != cfg.expect_status:
            err = f"expected status {cfg.expect_status}, got {status}"
        elif latency > cfg.max_latency_ms:
            err = f"latency {latency:.0f}ms exceeds {cfg.max_latency_ms:.0f}ms"
        elif cfg.expect_body_contains and cfg.expect_body_contains.encode() not in body:
            err = "body assertion failed"
    return err is None, status, latency, err


def run_check(cfg: CheckConfig, sleep=time.sleep) -> dict:
    """Run one check; failed attempts (any reason) are retried up to cfg.retries times."""
    ok = status = latency = err = None
    attempts = 0
    for i in range(cfg.retries + 1):
        attempts += 1
        ok, status, latency, err = _attempt(cfg)
        if ok:
            break
        if i < cfg.retries:
            sleep(cfg.backoff_s * (2 ** i))
    return {"name": cfg.name, "url": cfg.url, "ts": time.time(), "ok": bool(ok), "status": status,
            "latency_ms": round(latency, 2), "attempts": attempts, "error": err}


def run_checks(configs: list[CheckConfig]) -> list[dict]:
    return [run_check(c) for c in configs]


def load_checks(raw: list[dict]) -> list[CheckConfig]:
    return [CheckConfig(**c) for c in raw]
