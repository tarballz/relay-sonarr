"""English-option sweep: every imported file must offer English.

The rule (user-set, 2026-11-09): other languages are welcome alongside, but a
file must carry English **audio** — or, for anime only, English **subtitles**.

Two layers enforce it. Up front, a Sonarr custom format ("Foreign without
English", -10000 on every profile) rejects releases whose *title* is tagged
foreign with no English marker. Titles can prove a release is foreign but never
that it is English — fansub releases carry no language tag at all — so this
sweep is the backstop: it reads the tracks Sonarr's mediaInfo actually found in
each imported file.

A violating file is deleted, its episodes re-monitored, the grab marked failed
(which blocklists the release for the whole download) and the episodes
re-searched. Delete comes first: with the old file still on disk at cutoff,
Sonarr rejects every replacement as "not an upgrade".

Untagged tracks (``und``) may well be English, so they are never grounds for
deletion — the sweep journals a warning once per file and leaves it alone. The
same goes for a violating file with no grab in history (a manual import):
deleting it without a blocklist entry would just let the release come back.

Stateless like the other sweeps: one ``/episodefile`` GET per series per tick
(~56 series today), far cheaper than a single interactive search.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from app.obs import kinds
from app.obs.journal import get_journal
from app.obs.metrics import SWEEP_REMOVED
from app.services.fanout import gather_instances
from app.store import events as events_store

logger = logging.getLogger(__name__)

_ENGLISH = {"eng", "en", "english"}
_UNTAGGED = {"und", "unknown", ""}
_NON_ALPHA = re.compile(r"[^a-z]")


def _tracks(value) -> list[str]:
    """``"fre/jpn"`` -> ``["fre", "jpn"]``. Anything but letters is dropped per
    track: some muxers write the tag quoted (``"'eng'"`` — The Righteous Gemstones)."""
    return [_NON_ALPHA.sub("", t.lower()) for t in str(value or "").split("/")]


def is_anime(series: dict) -> bool:
    """Sonarr type ``anime`` or the TVDB ``Anime`` genre. The genre matters:
    every anime in the live library is typed ``standard``."""
    return (series.get("seriesType") == "anime"
            or "Anime" in (series.get("genres") or []))


def english_verdict(media_info: dict | None, *, anime: bool) -> str:
    """``ok`` | ``missing`` | ``unknown`` for one file's tracks."""
    if not media_info or "audioLanguages" not in media_info:
        return "unknown"
    audio = _tracks(media_info.get("audioLanguages"))
    subs = _tracks(media_info.get("subtitles"))
    if _ENGLISH & set(audio):
        return "ok"
    if anime and _ENGLISH & set(subs):
        return "ok"
    if _UNTAGGED & set(audio):
        return "unknown"
    if anime and subs != [""] and _UNTAGGED & set(subs):
        return "unknown"
    return "missing"


def _describe(media_info: dict) -> str:
    audio = media_info.get("audioLanguages") or "none"
    subs = media_info.get("subtitles") or "none"
    return f"audio {audio}, subtitles {subs}"


async def _scan(inst) -> list[tuple[dict, dict, str]]:
    """(series, file, verdict) for every non-ok file on one instance."""
    out = []
    for series in await inst.client.list_series():
        anime = is_anime(series)
        for f in await inst.client.episode_files(series["id"]):
            verdict = english_verdict(f.get("mediaInfo"), anime=anime)
            if verdict != "ok":
                out.append((series, f, verdict))
    return out


async def _grab_for(client, episode_ids: list[int], file_id: int) -> tuple[str, dict] | None:
    """(downloadId, grabbed history record) that produced ``file_id``, if any."""
    for eid in episode_ids:
        records = await client.episode_history(eid)
        imported = next((r for r in records
                         if r.get("eventType") == "downloadFolderImported"
                         and str((r.get("data") or {}).get("fileId")) == str(file_id)
                         and r.get("downloadId")), None)
        if imported is None:
            continue
        grab = next((r for r in records if r.get("eventType") == "grabbed"
                     and r.get("downloadId") == imported["downloadId"]), None)
        if grab is not None:
            return imported["downloadId"], grab
    return None


async def _remove_download(inst, ops, series: dict, download_id: str, grab: dict,
                           files: list[dict], episode_ids: list[int], *, now: datetime) -> bool:
    tvdb_id = series.get("tvdbId")
    title = grab.get("sourceTitle") or series.get("title") or download_id
    reason = _describe(files[0].get("mediaInfo") or {})
    op_id = await ops.start(kind="language-cleanup", title=title, tvdb_id=tvdb_id,
                            started_at=now.isoformat(), source="reconciler")
    try:
        for f in files:
            await inst.client.delete_episode_file(f["id"])
        await inst.client.set_episode_monitor(episode_ids, True)
        await inst.client.mark_history_failed(grab["id"])
        await inst.client.command("EpisodeSearch", episodeIds=episode_ids)
        message = (f"Removed {len(files)} file(s) with no English option ({reason}) — "
                   f"{title} (blocklisted, re-searching)")
        await ops.add_step(op_id, {"phase": "remove", "status": "done", "message": message})
        await ops.finish(op_id, result={"removed": True, "downloadId": download_id,
                                        "files": len(files)},
                         finished_at=now.isoformat())
        get_journal().emit(
            kinds.SWEEP_LANGUAGE_REMOVED, message, level="warn", source="sweep",
            tvdb_id=tvdb_id, instance_id=inst.id, operation_id=op_id,
            data={"title": title, "downloadId": download_id, "files": len(files),
                  "fileIds": [f["id"] for f in files], "tracks": reason},
        )
        return True
    except Exception as exc:  # noqa: BLE001 - one failure shouldn't stop the sweep
        await ops.finish(op_id, error=str(exc), finished_at=now.isoformat())
        logger.warning("language sweep failed to remove %s: %s", download_id, exc)
        return False


async def sweep_language(registry, db, ops, *, cap: int, now: datetime) -> int:
    """Remove imported files with no English option; returns downloads removed."""
    # One warning per (instance, series, reason): 127 live files carry no audio
    # tag at all, and a journal event per file would bury everything else.
    warned = {(e.get("instanceId"), (e.get("data") or {}).get("seriesId"),
               (e.get("data") or {}).get("reason"))
              for e in await events_store.query(db, kind=kinds.SWEEP_LANGUAGE_UNKNOWN,
                                                limit=5000)}

    def warn_once(inst, series, files: list[dict], reason: str) -> None:
        key = (inst.id, series["id"], reason)
        if not files or key in warned:
            return
        warned.add(key)
        get_journal().emit(
            kinds.SWEEP_LANGUAGE_UNKNOWN,
            f"{series.get('title')}: {len(files)} file(s) on {inst.id} — {reason}; left in place",
            level="warn", source="sweep", tvdb_id=series.get("tvdbId"), instance_id=inst.id,
            data={"seriesId": series["id"], "reason": reason, "files": len(files),
                  "fileIds": [f["id"] for f in files[:50]],
                  "example": files[0].get("relativePath"),
                  "tracks": _describe(files[0].get("mediaInfo") or {})},
        )

    removed = 0
    for inst, res in await gather_instances(registry, _scan):
        if isinstance(res, Exception):
            continue
        # Group violating files by the download that produced them: a season
        # pack is one blocklist entry and one unit of the cap.
        groups: dict[str, dict] = {}
        episodes_of: dict[int, list[dict]] = {}
        untagged: dict[int, tuple[dict, list]] = {}
        no_grab: dict[int, tuple[dict, list]] = {}
        for series, f, verdict in res:
            if verdict == "unknown":
                untagged.setdefault(series["id"], (series, []))[1].append(f)
                continue
            sid = series["id"]
            if sid not in episodes_of:
                episodes_of[sid] = await inst.client.episodes(sid)
            ep_ids = [e["id"] for e in episodes_of[sid] if e.get("episodeFileId") == f["id"]]
            found = await _grab_for(inst.client, ep_ids, f["id"])
            if found is None:
                no_grab.setdefault(sid, (series, []))[1].append(f)
                continue
            download_id, grab = found
            g = groups.setdefault(download_id, {"series": series, "grab": grab,
                                                "files": [], "episodes": []})
            g["files"].append(f)
            g["episodes"].extend(e for e in ep_ids if e not in g["episodes"])
        for series, fs in untagged.values():
            warn_once(inst, series, fs, "language untagged, can't confirm English")
        for series, fs in no_grab.values():
            warn_once(inst, series, fs, "no English option, but no grab in history to blocklist")
        for download_id, g in groups.items():
            if removed >= cap:
                break
            if await _remove_download(inst, ops, g["series"], download_id, g["grab"],
                                      g["files"], g["episodes"], now=now):
                removed += 1
                SWEEP_REMOVED.inc(sweep="language")
    if removed:
        logger.info("language sweep removed %d download(s)", removed)
    return removed
