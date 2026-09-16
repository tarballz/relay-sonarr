"""/healthz liveness + /api/reconciler/status endpoints."""
from starlette.testclient import TestClient

from app.main import app
from app.state import get_reconciler


class FakeReconciler:
    def __init__(self, healthy: bool):
        self._healthy = healthy

    async def status(self) -> dict:
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


from app.reconciler import TickBusy


class ManualReconciler:
    def __init__(self, busy: bool):
        self.busy = busy

    async def run_manual(self):
        if self.busy:
            raise TickBusy()
        return {"tickId": 5, "status": "ok", "error": None, "reconciled": []}


def test_manual_tick_endpoint_returns_result_or_409():
    try:
        app.dependency_overrides[get_reconciler] = lambda: ManualReconciler(busy=False)
        r = TestClient(app).post("/api/reconcile/tick")
        assert r.status_code == 200 and r.json()["tickId"] == 5
        app.dependency_overrides[get_reconciler] = lambda: ManualReconciler(busy=True)
        assert TestClient(app).post("/api/reconcile/tick").status_code == 409
    finally:
        _teardown()


# --- indexer degradation ------------------------------------------------------

from app.services.health import indexers_degraded  # noqa: E402


def _h(source, type_="warning", message="..."):
    return {"source": source, "type": type_, "message": message}


def test_no_health_issues_is_not_degraded():
    assert indexers_degraded([]) is False


def test_indexer_status_check_is_degraded():
    assert indexers_degraded([_h("IndexerStatusCheck")]) is True


def test_indexer_long_term_status_check_is_degraded():
    assert indexers_degraded([_h("IndexerLongTermStatusCheck")]) is True


def test_unrelated_warnings_are_not_degraded():
    """A root-folder or update warning says nothing about search results."""
    assert indexers_degraded([_h("RootFolderCheck"), _h("UpdateCheck")]) is False


def test_an_indexer_error_counts_too():
    assert indexers_degraded([_h("IndexerSearchCheck", "error")]) is True


def test_an_informational_indexer_notice_does_not_count():
    """Sonarr uses 'ok'/'notice' for things that aren't failures."""
    assert indexers_degraded([_h("IndexerStatusCheck", "ok")]) is False


def test_missing_fields_are_tolerated():
    assert indexers_degraded([{}, {"source": None}, {"type": "warning"}]) is False
