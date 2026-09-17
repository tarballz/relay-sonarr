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

from app.services.health import failing_indexers, indexers_degraded  # noqa: E402

SOME = {"source": "IndexerStatusCheck", "type": "warning",
        "message": "Indexers unavailable due to failures: The Pirate Bay (Prowlarr)"}
LONG = {"source": "IndexerLongTermStatusCheck", "type": "warning",
        "message": "Indexers unavailable due to failures for more than 6 hours: "
                   "1337x (Prowlarr), Internet Archive (Prowlarr)"}
NONE_LEFT = {"source": "IndexerSearchCheck", "type": "error",
             "message": "No indexers available with Interactive Search enabled, "
                        "Sonarr will not provide any interactive search results"}


def test_failing_indexers_parses_names_from_the_message():
    assert failing_indexers([SOME]) == {"The Pirate Bay (Prowlarr)"}


def test_failing_indexers_unions_short_and_long_term_without_double_counting():
    assert failing_indexers([SOME, LONG]) == {
        "The Pirate Bay (Prowlarr)", "1337x (Prowlarr)", "Internet Archive (Prowlarr)"}


def test_failing_indexers_ignores_unrelated_checks():
    assert failing_indexers([{"source": "UpdateCheck", "type": "warning",
                              "message": "New update is available: v4.0"}]) == set()


def test_a_minority_of_failing_indexers_is_not_degraded():
    """The bug this replaces: any single failure tripped the gate, and with eight
    public-tracker indexers at least one is always failing — so it tripped
    permanently, discarded ~9000 searches a day and froze the cache."""
    assert indexers_degraded([SOME, LONG], interactive_count=8) is False


def test_every_indexer_failing_is_degraded():
    assert indexers_degraded([SOME, LONG], interactive_count=3) is True


def test_no_indexers_available_at_all_is_degraded():
    assert indexers_degraded([NONE_LEFT], interactive_count=8) is True


def test_no_health_issues_is_not_degraded():
    assert indexers_degraded([], interactive_count=8) is False


def test_unknown_indexer_count_does_not_gate():
    """If we can't prove capacity is gone, trust the result — over-triggering was
    strictly worse than under-triggering, because it blocks its own repair path."""
    assert indexers_degraded([SOME, LONG], interactive_count=None) is False


def test_zero_configured_indexers_is_degraded():
    assert indexers_degraded([], interactive_count=0) is True


def test_missing_fields_are_tolerated():
    assert indexers_degraded([{}, {"source": None}], interactive_count=8) is False
