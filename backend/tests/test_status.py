"""Library status summary (per-series resolution state) from the DB."""
import httpx
import respx
from starlette.testclient import TestClient

from app.db import Database
from app.main import app
from app.services.status import derive_status, library_status
from app.state import get_db, get_registry
from app.store import intents as intent_store
from app.store import placements as place_store
from tests.test_chain import make_registry


def test_derive_status_precedence():
    assert derive_status({"searching": 1, "wanted": 3}, set()) == "reconciling"
    assert derive_status({"failed": 1}, set()) == "retrying"
    assert derive_status({"unavailable": 2}, set()) == "stuck"
    assert derive_status({"wanted": 2}, set()) == "waiting"
    assert derive_status({"imported": 5}, {"4k"}) == "on-target"
    assert derive_status({"imported": 5}, {"4k", "1080p"}) == "split"


async def test_library_status_groups_by_series(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    await place_store.upsert(db, tvdb_id=99, season=1, episode=1, desired_tier="4k",
                             obtained_tier="4k", state="imported", reason=None, updated_at="t")
    await place_store.upsert(db, tvdb_id=99, season=1, episode=2, desired_tier="4k",
                             obtained_tier=None, state="wanted", reason=None, updated_at="t")
    await intent_store.ensure(db, tvdb_id=99, title="Mad Men", chain_key="4k", now="t")
    await intent_store.set_paused(db, 99, True, now="t")

    out = {s["tvdbId"]: s for s in await library_status(db)}
    assert out[99]["status"] == "waiting"
    assert out[99]["counts"] == {"imported": 1, "wanted": 1}
    assert out[99]["paused"] is True


@respx.mock
def test_library_status_endpoint(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    app.dependency_overrides[get_registry] = make_registry
    app.dependency_overrides[get_db] = lambda: db
    try:
        c = TestClient(app)
        assert c.get("/api/library/status").json() == []
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_registry, None)
