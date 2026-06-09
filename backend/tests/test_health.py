"""/healthz liveness + /api/reconciler/status endpoints."""
from starlette.testclient import TestClient

from app.main import app
from app.state import get_reconciler


class FakeReconciler:
    def __init__(self, healthy: bool):
        self._healthy = healthy

    def status(self) -> dict:
        return {
            "enabled": True,
            "healthy": self._healthy,
            "totalTicks": 3,
            "consecutiveFailures": 0 if self._healthy else 5,
            "lastError": None if self._healthy else "ValueError: boom",
            "lastTickActions": 2,
            "lastTickFinishedAt": "2026-06-09T00:00:00+00:00",
            "lastTickDurationS": 0.4,
            "secondsSinceLastTick": 12.0,
            "intervalS": 1800,
        }


def _client(healthy: bool) -> TestClient:
    app.dependency_overrides[get_reconciler] = lambda: FakeReconciler(healthy)
    return TestClient(app)


def _teardown():
    app.dependency_overrides.pop(get_reconciler, None)


def test_healthz_ok_when_reconciler_healthy():
    try:
        r = _client(True).get("/healthz")
        assert r.status_code == 200
        assert r.json()["healthy"] is True
    finally:
        _teardown()


def test_healthz_503_when_reconciler_stale():
    try:
        r = _client(False).get("/healthz")
        assert r.status_code == 503
        body = r.json()
        assert body["healthy"] is False
        assert body["consecutiveFailures"] == 5
    finally:
        _teardown()


def test_reconciler_status_endpoint_shape():
    try:
        r = _client(True).get("/api/reconciler/status")
        assert r.status_code == 200
        body = r.json()
        assert set(body) >= {"enabled", "healthy", "totalTicks", "lastTickActions"}
        assert body["totalTicks"] == 3
    finally:
        _teardown()
