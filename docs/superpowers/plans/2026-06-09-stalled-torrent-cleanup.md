# Stalled-torrent cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Autonomously remove torrents stuck at 0% past a threshold (default 3 days) by having Sonarr remove + blocklist them (which triggers a replacement search), as a step in the reconciler tick.

**Architecture:** A new `sweep_stalled()` in `services/poller.py` is called from `Reconciler.tick()` after `poll_all`. It fetches each instance's queue, finds torrent records at 0% older than `stalledDays`, and calls a new `SonarrClient.delete_queue_item()` (`DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true`). Each removal is logged to the Operation store. Gated by a `STALLED_CLEANUP_ENABLED` env flag and a per-tick cap.

**Tech Stack:** FastAPI, stdlib `sqlite3`, httpx (`SonarrClient`), pytest + respx. Spec: `docs/superpowers/specs/2026-06-09-stalled-torrent-cleanup-design.md`.

Run tests from `backend/` with `uv run pytest`.

---

### Task 1: `SonarrClient.delete_queue_item`

**Files:**
- Modify: `backend/app/sonarr/client.py` (add method after `delete_series`)
- Test: `backend/tests/test_client.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_client.py`:

```python
@respx.mock
async def test_delete_queue_item_removes_and_blocklists(client):
    route = respx.delete(f"{BASE}/api/v3/queue/42").mock(return_value=httpx.Response(200))
    await client.delete_queue_item(42)
    params = route.calls.last.request.url.params
    assert params["removeFromClient"] == "true"
    assert params["blocklist"] == "true"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_client.py::test_delete_queue_item_removes_and_blocklists -v`
Expected: FAIL — `AttributeError: 'SonarrClient' object has no attribute 'delete_queue_item'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/sonarr/client.py`, add directly after the `delete_series` method:

```python
    async def delete_queue_item(self, queue_id: int, *, remove_from_client: bool = True,
                                blocklist: bool = True) -> None:
        """Remove a queue item (DELETE /queue/{id}).

        With blocklist=true Sonarr blocklists the release so it isn't re-grabbed and,
        leaving skipRedownload at its default, searches for a replacement."""
        await self._delete(
            f"/queue/{queue_id}",
            {
                "removeFromClient": str(remove_from_client).lower(),
                "blocklist": str(blocklist).lower(),
            },
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_client.py::test_delete_queue_item_removes_and_blocklists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/sonarr/client.py backend/tests/test_client.py
git commit -m "feat: SonarrClient.delete_queue_item (remove + blocklist)"
```

---

### Task 2: `sweep_stalled` detection + action

**Files:**
- Modify: `backend/app/services/poller.py` (add `import logging`, logger, `_is_stalled`, `_stalled_title`, `sweep_stalled`)
- Test: `backend/tests/test_stalled.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_stalled.py`:

```python
"""Autonomous stalled-torrent sweep: remove torrents stuck at 0% past the threshold."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import respx

from app.db import Database
from app.services.poller import sweep_stalled
from app.store.operations import OperationStore
from tests.test_chain import A, B, make_registry

NOW = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)


def _rec(**over):
    base = {"id": 1, "protocol": "torrent", "status": "downloading",
            "size": 1000, "sizeleft": 1000, "added": (NOW - timedelta(days=10)).isoformat(),
            "seriesId": 5, "title": "Show.S01E01", "downloadId": "HASH"}
    base.update(over)
    return base


def _queue(records):
    return httpx.Response(200, json={"page": 1, "pageSize": 200,
                                     "totalRecords": len(records), "records": records})


@respx.mock
async def test_sweep_removes_only_old_zero_percent_torrents(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    # 1080p (A) queue: one stalled-old, one stalled-recent, one healthy-partial, one usenet-0%.
    a_records = [
        _rec(id=11, added=(NOW - timedelta(days=10)).isoformat()),          # REMOVE
        _rec(id=12, added=(NOW - timedelta(hours=2)).isoformat()),          # too new
        _rec(id=13, sizeleft=400, added=(NOW - timedelta(days=10)).isoformat()),  # partial, not 0%
        _rec(id=14, protocol="usenet", added=(NOW - timedelta(days=10)).isoformat()),  # not a torrent
    ]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(a_records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    del_route = respx.delete(f"{A}/api/v3/queue/11").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=3, cap=25, now=NOW)

    assert removed == 1
    assert del_route.called
    params = del_route.calls.last.request.url.params
    assert params["removeFromClient"] == "true" and params["blocklist"] == "true"
    # An operation was logged for the removal.
    op = (await ops.recent())[0]
    assert op["kind"] == "stalled-cleanup" and op["source"] == "reconciler"


@respx.mock
async def test_sweep_respects_per_tick_cap(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    ops = OperationStore(db)
    reg = make_registry()
    records = [_rec(id=100 + i, added=(NOW - timedelta(days=10)).isoformat()) for i in range(5)]
    respx.get(f"{A}/api/v3/queue").mock(return_value=_queue(records))
    respx.get(f"{B}/api/v3/queue").mock(return_value=_queue([]))
    for i in range(5):
        respx.delete(f"{A}/api/v3/queue/{100 + i}").mock(return_value=httpx.Response(200))

    removed = await sweep_stalled(reg, db, ops, stalled_days=3, cap=2, now=NOW)
    assert removed == 2  # cap honored; the other 3 wait for the next tick
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_stalled.py -v`
Expected: FAIL — `ImportError: cannot import name 'sweep_stalled' from 'app.services.poller'`

- [ ] **Step 3: Write minimal implementation**

At the top of `backend/app/services/poller.py`, add after the existing imports:

```python
import logging

logger = logging.getLogger(__name__)
```

Add these functions to `backend/app/services/poller.py` (end of file):

```python
def _is_stalled(record: dict, *, now: datetime, stalled_days: float) -> bool:
    """A torrent downloading at literal 0% (no bytes) since longer than the threshold."""
    if record.get("protocol") != "torrent":
        return False
    if (record.get("status") or "").lower() != "downloading":
        return False
    size = record.get("size") or 0
    if size <= 0 or record.get("sizeleft") != size:  # sizeleft==size means 0% downloaded
        return False
    added = record.get("added")
    if not added:
        return False
    try:
        added_dt = datetime.fromisoformat(str(added).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - added_dt).total_seconds() > stalled_days * 86400.0


def _stalled_title(record: dict) -> str:
    base = (record.get("series") or {}).get("title") or record.get("title") or "unknown"
    ep = record.get("episode") or {}
    if ep.get("seasonNumber") is not None and ep.get("episodeNumber") is not None:
        return f"{base} S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"
    return base


async def sweep_stalled(registry, db, ops, *, stalled_days: float, cap: int,
                        now: datetime) -> int:
    """Remove torrents stuck at 0% past the threshold. Sonarr removes them from the
    client, blocklists the release, and re-searches. Returns the number removed."""
    removed = 0
    skipped = 0
    for inst, res in await gather_instances(registry, lambda i: i.client.queue()):
        if isinstance(res, Exception):
            continue
        for r in res.get("records", []):
            if not _is_stalled(r, now=now, stalled_days=stalled_days):
                continue
            if removed >= cap:
                skipped += 1
                continue
            added_dt = datetime.fromisoformat(str(r["added"]).replace("Z", "+00:00"))
            age_days = int((now - added_dt).total_seconds() // 86400)
            title = _stalled_title(r)
            op_id = await ops.start(
                kind="stalled-cleanup", title=title, tvdb_id=r.get("seriesId") or 0,
                started_at=now.isoformat(), source="reconciler",
            )
            try:
                await inst.client.delete_queue_item(r["id"])
                await ops.add_step(op_id, {
                    "phase": "remove", "status": "done",
                    "message": f"Removed stalled torrent — {title}, 0% for {age_days}d "
                               f"(blocklisted; Sonarr re-searching)",
                })
                await ops.finish(op_id, result={"removed": True, "downloadId": r.get("downloadId")},
                                 finished_at=now.isoformat())
                removed += 1
            except Exception as exc:  # noqa: BLE001 - one failure shouldn't stop the sweep
                await ops.finish(op_id, error=str(exc), finished_at=now.isoformat())
                logger.warning("failed to remove stalled torrent %s: %s", r.get("id"), exc)
    if skipped:
        logger.warning("stalled sweep hit per-tick cap (%d); %d deferred to next tick", cap, skipped)
    if removed:
        logger.info("stalled sweep removed %d torrent(s)", removed)
    return removed
```

Verify `datetime` and `gather_instances` are already imported at the top of `poller.py` (they are — `from datetime import ...` and `from app.services.fanout import gather_instances`). If `datetime` is not imported, add `from datetime import datetime`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_stalled.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/poller.py backend/tests/test_stalled.py
git commit -m "feat: sweep_stalled — detect + remove torrents stuck at 0%"
```

---

### Task 3: Wire the sweep into the reconciler tick

**Files:**
- Modify: `backend/app/reconciler.py` (`__init__` params + `tick()` step)
- Test: `backend/tests/test_reconciler.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_reconciler.py` (near the other unit tests; `_make` and `NOW` already exist in that file):

```python
async def test_tick_runs_stalled_sweep_when_enabled(tmp_path, monkeypatch):
    reg, db, ops, rec = _make(tmp_path)
    rec.stalled_cleanup = True
    called = {}

    async def fake_poll_all(*a, **k):
        return {"polled": []}

    async def fake_sweep(registry, db_, ops_, *, stalled_days, cap, now):
        called["days"] = stalled_days
        called["cap"] = cap
        return 0

    monkeypatch.setattr("app.reconciler.poller.poll_all", fake_poll_all)
    monkeypatch.setattr("app.reconciler.poller.sweep_stalled", fake_sweep)
    monkeypatch.setattr(rec, "_ensure_intents", lambda: _anoop())

    await rec.tick()
    assert called.get("cap") == rec._stalled_cap  # sweep ran with the configured cap


async def test_tick_skips_stalled_sweep_when_disabled(tmp_path, monkeypatch):
    reg, db, ops, rec = _make(tmp_path)
    rec.stalled_cleanup = False
    called = {"ran": False}

    async def fake_poll_all(*a, **k):
        return {"polled": []}

    async def fake_sweep(*a, **k):
        called["ran"] = True
        return 0

    monkeypatch.setattr("app.reconciler.poller.poll_all", fake_poll_all)
    monkeypatch.setattr("app.reconciler.poller.sweep_stalled", fake_sweep)
    monkeypatch.setattr(rec, "_ensure_intents", lambda: _anoop())

    await rec.tick()
    assert called["ran"] is False


async def _anoop():
    return None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reconciler.py::test_tick_runs_stalled_sweep_when_enabled -v`
Expected: FAIL — `AttributeError: 'Reconciler' object has no attribute 'stalled_cleanup'` (or `_stalled_cap`)

- [ ] **Step 3: Write minimal implementation**

In `backend/app/reconciler.py`, change the `__init__` signature line:

```python
                 wait_delay: float = 1.5, enabled: bool = True,
                 stalled_cleanup: bool = True, stalled_cap: int = 25):
```

And in the `__init__` body, after `self.enabled = enabled`, add:

```python
        self.stalled_cleanup = stalled_cleanup
        self._stalled_cap = stalled_cap
```

In `tick()`, replace the first line (`await poller.poll_all(self.registry, self.db, now=self.now())`) with:

```python
        await poller.poll_all(self.registry, self.db, now=self.now())
        if self.stalled_cleanup:
            defaults = await settings_store.get_defaults(self.db)
            try:
                await poller.sweep_stalled(
                    self.registry, self.db, self.ops,
                    stalled_days=defaults.get("stalledDays", 3),
                    cap=self._stalled_cap, now=self.now(),
                )
            except Exception:  # noqa: BLE001 - sweep failure must not stop the tick
                logger.exception("stalled sweep failed")
```

(`settings_store` and `logger` are already imported/defined in `reconciler.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reconciler.py -v`
Expected: PASS (all reconciler tests, including the two new ones)

- [ ] **Step 5: Commit**

```bash
git add backend/app/reconciler.py backend/tests/test_reconciler.py
git commit -m "feat: run stalled sweep each reconciler tick (gated + capped)"
```

---

### Task 4: `STALLED_CLEANUP_ENABLED` env flag

**Files:**
- Modify: `backend/app/main.py`

- [ ] **Step 1: Add the flag helper**

In `backend/app/main.py`, after the `_reconciler_enabled()` function, add:

```python
def _stalled_cleanup_enabled() -> bool:
    return os.environ.get("STALLED_CLEANUP_ENABLED", "true").lower() != "false"
```

- [ ] **Step 2: Pass it into the Reconciler**

In the lifespan, change the `Reconciler(...)` construction to:

```python
    app.state.reconciler = Reconciler(
        app.state.registry, app.state.db, app.state.operations,
        enabled=enabled, stalled_cleanup=_stalled_cleanup_enabled(),
    )
```

- [ ] **Step 3: Run the full backend suite**

Run: `uv run pytest -q`
Expected: PASS (all tests green)

- [ ] **Step 4: Commit**

```bash
git add backend/app/main.py
git commit -m "feat: STALLED_CLEANUP_ENABLED env flag (default on)"
```

---

### Task 5: `stalledDays` knob in the Settings defaults editor

**Files:**
- Modify: `frontend/src/pages/Settings.jsx` (the `DefaultsEditor` component)

No frontend test runner exists; verify by build + manual check.

- [ ] **Step 1: Seed `stalledDays` into the form state**

In `DefaultsEditor`, update the `useEffect` that seeds `form` to include `stalledDays`:

```jsx
  useEffect(() => {
    if (data) setForm({
      allowSplit: data.allowSplit ?? true,
      escalateAfterDays: data.escalateAfterDays ?? 0,
      stalledDays: data.stalledDays ?? 3,
    });
  }, [JSON.stringify(data)]);
```

- [ ] **Step 2: Add the input**

In `DefaultsEditor`'s returned JSX, after the `escalateAfterDays` field's `</label>`, add:

```jsx
        <label className="field" style={{ maxWidth: 260, marginTop: 14 }}>
          Remove torrents stuck at 0% after (days)
          <input
            className="input"
            type="number"
            min="1"
            value={form.stalledDays}
            onChange={(e) => setForm((f) => ({ ...f, stalledDays: Number(e.target.value) }))}
          />
        </label>
```

(`save()` already PUTs `{ ...data, ...form }`, so `stalledDays` persists with no further change.)

- [ ] **Step 3: Build the SPA to verify it compiles**

Run (from repo root):
```bash
docker run --rm -v "$PWD/frontend":/fe -w /fe node:20-alpine sh -c "npm install --no-audit --no-fund --loglevel=error >/dev/null 2>&1 && npm run build" 2>&1 | tail -3; rm -f frontend/package-lock.json
```
Expected: `✓ built` with no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/Settings.jsx
git commit -m "feat: stalledDays knob in Settings defaults editor"
```

---

### Task 6: Deploy and verify end-to-end

- [ ] **Step 1: Full backend suite green**

Run: `cd backend && uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Build + deploy**

Run (repo root): `docker compose up -d --build`
Wait for healthy: `until curl -sf http://localhost:8088/healthz >/dev/null; do sleep 2; done`

- [ ] **Step 3: Trigger a tick and confirm removals are logged**

Run: `curl -s -X POST http://localhost:8088/api/reconcile/tick >/dev/null`
Then check the Operations feed for `stalled-cleanup` entries:
```bash
curl -s http://localhost:8088/api/operations | python3 -c "import sys,json; ops=json.load(sys.stdin); print([o['title'] for o in ops if o.get('kind')=='stalled-cleanup'][:10])"
```
Expected: titles of removed stalled series (your current backlog includes ~14 at 0%, one ~6 weeks old). Re-checking `/api/queue` should show those 0% torrents gone.

- [ ] **Step 4: Final commit (if any deploy-related tweaks)**

```bash
git add -A && git commit -m "chore: deploy stalled-torrent cleanup" || echo "nothing to commit"
```

---

## Notes for the implementer

- **TDD throughout the backend** — every backend task writes the test first, watches it fail, then implements. The frontend (Task 5) has no runner; verify by build + manual.
- The sweep fetches each instance's queue once (separate from `poll_all`'s internal fetch). Acceptable: the tick runs every ~30 min.
- Strict 0% (`sizeleft == size`) means removal can never discard real progress.
- `STALLED_CLEANUP_ENABLED=false` is the kill-switch; `stalledDays` is tunable from the Settings page without a restart.
