import pytest

from apimon import db
from apimon.collector import create_app

NOW = 1_800_000_000.0


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def dbpath(tmp_path):
    return str(tmp_path / "t.db")


@pytest.fixture
def conn(dbpath):
    c = db.connect(dbpath)
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def project(conn):
    pid, key = db.create_project(conn, "demo", slo_target=0.99, apdex_t_ms=100)
    return {"id": pid, "key": key, "row": db.project_by_name(conn, "demo")}


@pytest.fixture
def app(dbpath):
    return create_app(dbpath, admin_token="adm", clock=lambda: NOW, rate_capacity=1000, rate_refill=1000)


def ev(ts, route="/a", status=200, lat=10.0, method="GET", error=None, rate=1.0):
    return {"ts": ts, "method": method, "route": route, "status": status, "latency_ms": lat,
            "request_bytes": 0, "response_bytes": 10, "error": error, "request_id": None, "sample_rate": rate}
