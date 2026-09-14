"""Observability endpoints: Prometheus metrics, the event journal, tick history, summary."""
from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import Response, StreamingResponse

from app.obs import sse
from app.obs.metrics import METRICS
from app.state import get_db, get_event_journal, get_monitor, get_operations
from app.store import events as events_store
from app.store import summary
from app.store import ticks as tick_store

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, db=Depends(get_db)):
    """Prometheus scrape target. Exempt from Cloudflare Access (see auth.py); set
    METRICS_TOKEN to require ``Authorization: Bearer <token>`` instead."""
    token = os.environ.get("METRICS_TOKEN")
    if token:
        provided = request.headers.get("authorization") or ""
        expected = f"Bearer {token}"
        if not hmac.compare_digest(provided.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Invalid metrics token")
    await summary.refresh_gauges(db)
    return Response(METRICS.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/events")
async def list_events(
    kind: str | None = None,
    level: str | None = None,
    tvdb: int | None = None,
    tick: int | None = None,
    source: str | None = None,
    before: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    db=Depends(get_db),
):
    """Newest-first journal events. ``kind`` is a prefix (``poll.``), ``level`` a
    minimum; page with ``before=<nextBefore>``."""
    try:
        items = await events_store.query(
            db, kind=kind, level=level, tvdb_id=tvdb, tick_id=tick, source=source,
            before=before, limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"items": items, "nextBefore": items[-1]["id"] if len(items) == limit else None}


@router.get("/api/events/stream")
async def events_stream(
    request: Request,
    since: int | None = None,
    db=Depends(get_db),
    journal=Depends(get_event_journal),
):
    """Live journal as SSE. Resumes from ``Last-Event-ID`` (sent automatically by
    EventSource on reconnect) or ``?since=``; otherwise live events only."""
    header = request.headers.get("last-event-id", "")
    last = int(header) if header.isdigit() else since
    return StreamingResponse(
        sse.event_stream(journal, db, last_event_id=last),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/api/ticks")
async def list_ticks(limit: int = Query(96, ge=1, le=500), db=Depends(get_db)):
    return await tick_store.recent(db, limit=limit)


@router.get("/api/ticks/{tick_id}")
async def tick_detail(tick_id: int, db=Depends(get_db)):
    tick = await tick_store.get(db, tick_id)
    if tick is None:
        raise HTTPException(status_code=404, detail="Tick not found")
    return {**tick, "events": await events_store.for_tick(db, tick_id)}


@router.get("/api/operations/{op_id}")
async def operation_detail(op_id: int, ops=Depends(get_operations)):
    op = await ops.get(op_id)
    if op is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return op


@router.get("/api/summary")
async def overview_summary(db=Depends(get_db), monitor=Depends(get_monitor)):
    return {
        "placementCounts": await summary.placement_counts(db),
        "seriesStatusCounts": await summary.series_status_counts(db),
        "intents": await summary.intent_counts(db),
        "lastTick": await tick_store.last_completed(db),
        "instances": monitor.snapshot() if monitor is not None else [],
    }
