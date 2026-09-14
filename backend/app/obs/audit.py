"""Audit trail: user-initiated changes, journaled with before/after and the actor."""
from __future__ import annotations

from app.obs.journal import get_journal


def record(kind: str, message: str, *, actor: str | None = None, level: str = "info",
           tvdb_id: int | None = None, instance_id: str | None = None, **data) -> None:
    """Journal a user action. ``actor`` is the Cloudflare Access email when Access
    is enabled (``verify_access``), and is omitted otherwise."""
    if actor:
        data["actor"] = actor
    get_journal().emit(kind, message, level=level, source="user",
                       tvdb_id=tvdb_id, instance_id=instance_id, data=data)
