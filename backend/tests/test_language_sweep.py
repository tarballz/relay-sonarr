"""English-option sweep: every imported file must carry an English option.

Live incident (2026-11-08): Neon Genesis Evangelion imported 26 episodes of
"CUSTOM MULTi-FRENCH/VOSTFR ... (VF)" — French + Japanese audio, French-only
subtitles. Nothing rejected it: Sonarr v4 ignores the deprecated language
profile and its title parser reads MULTi/VOSTFR as Unknown. A title custom
format now catches tagged releases up front; this sweep is the backstop for
the ones whose title hides it, reading the tracks Sonarr's mediaInfo found.

The rule: English audio always qualifies; English subtitles qualify only for
anime. An untagged (``und``) track might be English, so it is never grounds
for deletion — the sweep warns once and leaves the file alone.
"""
import json
from datetime import datetime, timezone

import httpx
import respx

from app.obs import kinds
from app.services.language import english_verdict, is_anime, sweep_language
from app.store import events as events_store
from app.store.operations import OperationStore
from tests.test_chain import A, B, make_registry

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def _mi(audio, subs=""):
    return {"audioLanguages": audio, "subtitles": subs}


# --- verdict ------------------------------------------------------------------

def test_english_audio_always_qualifies():
    assert english_verdict(_mi("eng"), anime=False) == "ok"
    assert english_verdict(_mi("jpn/eng", "fre"), anime=False) == "ok"
    assert english_verdict(_mi("fre/eng"), anime=True) == "ok"


def test_english_subtitles_qualify_only_for_anime():
    assert english_verdict(_mi("jpn", "eng"), anime=True) == "ok"
    assert english_verdict(_mi("fre", "eng"), anime=False) == "missing"


def test_evangelion_release_is_missing_english():
    assert english_verdict(_mi("fre/jpn", "fre/fre"), anime=True) == "missing"


def test_no_subtitles_on_foreign_anime_is_missing():
    assert english_verdict(_mi("jpn", ""), anime=True) == "missing"


def test_untagged_audio_is_unknown_not_missing():
    assert english_verdict(_mi("und"), anime=False) == "unknown"
    assert english_verdict(_mi(""), anime=False) == "unknown"
    assert english_verdict(_mi("fre/und"), anime=False) == "unknown"


def test_untagged_subtitles_on_anime_are_unknown():
    assert english_verdict(_mi("jpn", "und"), anime=True) == "unknown"


def test_missing_media_info_is_unknown():
    assert english_verdict(None, anime=False) == "unknown"
    assert english_verdict({}, anime=True) == "unknown"


def test_language_codes_are_case_and_spacing_tolerant():
    assert english_verdict(_mi("JPN / ENG"), anime=False) == "ok"
    assert english_verdict(_mi("English"), anime=False) == "ok"


def test_quoted_language_tags_are_read():
    # The Righteous Gemstones (live): 36 files whose muxer wrote audio as "'eng'".
    assert english_verdict(_mi("'eng'", "eng"), anime=False) == "ok"
    assert english_verdict(_mi('"fre"/"jpn"', "'fre'"), anime=True) == "missing"


def test_is_anime_by_type_or_genre():
    assert is_anime({"seriesType": "anime", "genres": []})
    # Every anime in the live library is typed "standard" — the genre decides.
    assert is_anime({"seriesType": "standard", "genres": ["Animation", "Anime"]})
    assert not is_anime({"seriesType": "standard", "genres": ["Animation"]})
    assert not is_anime({})


# --- sweep --------------------------------------------------------------------

EVA = {"id": 46, "tvdbId": 70350, "title": "Neon Genesis Evangelion",
       "seriesType": "standard", "genres": ["Animation", "Anime"]}
GEAR = {"id": 7, "tvdbId": 74608, "title": "Top Gear", "seriesType": "standard",
        "genres": ["Documentary"]}
DL = "DB4C4F79ABD76B7B5A2567882BF8424F66463849"


def _file(fid, sid, audio, subs="", ep_season=1):
    return {"id": fid, "seriesId": sid, "seasonNumber": ep_season,
            "relativePath": f"Season 1/file-{fid}.mkv", "mediaInfo": _mi(audio, subs)}


def _episode(eid, sid, num, fid):
    return {"id": eid, "seriesId": sid, "seasonNumber": 1, "episodeNumber": num,
            "episodeFileId": fid}


def _history(eid, fid, download_id=DL, grab_id=5215):
    return httpx.Response(200, json={"records": [
        {"id": 9000 + eid, "eventType": "downloadFolderImported", "downloadId": download_id,
         "episodeId": eid, "data": {"fileId": str(fid)}},
        {"id": grab_id, "eventType": "grabbed", "downloadId": download_id, "episodeId": eid,
         "sourceTitle": "Neon Genesis Evangelion S01 CUSTOM MULTi-FRENCH/VOSTFR 1080p"},
    ]})


def _mock_library(series_a, files_by_series, episodes_by_series, *, series_b=()):
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=list(series_a)))
    respx.get(f"{B}/api/v3/series").mock(return_value=httpx.Response(200, json=list(series_b)))

    def files(request):
        sid = int(request.url.params["seriesId"])
        return httpx.Response(200, json=files_by_series.get(sid, []))

    def episodes(request):
        sid = int(request.url.params["seriesId"])
        return httpx.Response(200, json=episodes_by_series.get(sid, []))

    respx.get(f"{A}/api/v3/episodefile").mock(side_effect=files)
    respx.get(f"{A}/api/v3/episode").mock(side_effect=episodes)


async def _events(journal, db, kind):
    await journal.flush()
    return await events_store.query(db, kind=kind)


@respx.mock
async def test_sweep_deletes_fails_remonitors_and_researches_a_pack(db, journal):
    """Evangelion: one season-pack download, many files — one unit of work."""
    ops = OperationStore(db)
    files = [_file(2719, 46, "fre/jpn", "fre/fre"), _file(2720, 46, "fre/jpn", "fre/fre")]
    eps = [_episode(5730, 46, 1, 2719), _episode(5731, 46, 2, 2720)]
    _mock_library([EVA], {46: files}, {46: eps})
    respx.get(f"{A}/api/v3/history").mock(
        side_effect=lambda req: _history(int(req.url.params["episodeId"]),
                                         {5730: 2719, 5731: 2720}[int(req.url.params["episodeId"])]))
    del1 = respx.delete(f"{A}/api/v3/episodefile/2719").mock(return_value=httpx.Response(200))
    del2 = respx.delete(f"{A}/api/v3/episodefile/2720").mock(return_value=httpx.Response(200))
    monitor = respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(202, json=[]))
    failed = respx.post(f"{A}/api/v3/history/failed/5215").mock(return_value=httpx.Response(200))
    cmd = respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    removed = await sweep_language(make_registry(), db, ops, cap=25, now=NOW)

    assert removed == 1                      # one download, not two files
    assert del1.called and del2.called
    assert failed.call_count == 1            # the pack is blocklisted once
    assert json.loads(monitor.calls.last.request.content) == {
        "episodeIds": [5730, 5731], "monitored": True}
    body = json.loads(cmd.calls.last.request.content)
    assert body == {"name": "EpisodeSearch", "episodeIds": [5730, 5731]}
    op = (await ops.recent())[0]
    assert op["kind"] == "language-cleanup"
    (event,) = await _events(journal, db, kinds.SWEEP_LANGUAGE_REMOVED)
    assert event["tvdbId"] == 70350
    assert event["data"]["downloadId"] == DL
    assert event["data"]["files"] == 2


@respx.mock
async def test_delete_happens_before_mark_failed(db, journal):
    """With the file still on disk at cutoff, the re-search would reject every
    replacement as 'not an upgrade' — so the file must go first."""
    order = []
    files = [_file(2719, 46, "fre/jpn", "fre/fre")]
    _mock_library([EVA], {46: files}, {46: [_episode(5730, 46, 1, 2719)]})
    respx.get(f"{A}/api/v3/history").mock(return_value=_history(5730, 2719))
    respx.delete(f"{A}/api/v3/episodefile/2719").mock(
        side_effect=lambda r: order.append("delete") or httpx.Response(200))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(202, json=[]))
    respx.post(f"{A}/api/v3/history/failed/5215").mock(
        side_effect=lambda r: order.append("failed") or httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    await sweep_language(make_registry(), db, OperationStore(db), cap=25, now=NOW)

    assert order == ["delete", "failed"]


@respx.mock
async def test_compliant_and_unknown_files_are_left_alone(db, journal):
    files_eva = [_file(1, 46, "jpn", "eng")]                    # anime + eng subs: ok
    files_gear = [_file(2, 7, "eng"), _file(3, 7, "und")]       # ok, unknown
    _mock_library([EVA, GEAR], {46: files_eva, 7: files_gear},
                  {46: [_episode(10, 46, 1, 1)], 7: [_episode(20, 7, 1, 2),
                                                     _episode(21, 7, 2, 3)]})
    delete = respx.delete(url__regex=rf"{A}/api/v3/episodefile/\d+")

    removed = await sweep_language(make_registry(), db, OperationStore(db), cap=25, now=NOW)

    assert removed == 0
    assert not delete.called
    (warn,) = await _events(journal, db, kinds.SWEEP_LANGUAGE_UNKNOWN)
    assert warn["data"]["fileIds"] == [3] and warn["tvdbId"] == 74608


@respx.mock
async def test_unknown_files_warn_once_per_series_not_per_file(db, journal):
    """127 live files carry no audio tag at all; one event per series, not 127."""
    files = [_file(i, 7, "") for i in range(1, 6)]
    _mock_library([GEAR], {7: files}, {7: []})

    await sweep_language(make_registry(), db, OperationStore(db), cap=25, now=NOW)

    (warn,) = await _events(journal, db, kinds.SWEEP_LANGUAGE_UNKNOWN)
    assert warn["data"]["files"] == 5
    assert warn["data"]["seriesId"] == 7


@respx.mock
async def test_unknown_warning_is_journaled_once_per_file(db, journal):
    files = [_file(3, 7, "und")]
    _mock_library([GEAR], {7: files}, {7: [_episode(21, 7, 2, 3)]})

    for _ in range(3):   # each its own tick: the journal is flushed between them
        await sweep_language(make_registry(), db, OperationStore(db), cap=25, now=NOW)
        await journal.flush()

    assert len(await _events(journal, db, kinds.SWEEP_LANGUAGE_UNKNOWN)) == 1


@respx.mock
async def test_non_anime_with_only_english_subtitles_is_removed(db, journal):
    files = [_file(4, 7, "fre", "eng")]
    _mock_library([GEAR], {7: files}, {7: [_episode(22, 7, 3, 4)]})
    respx.get(f"{A}/api/v3/history").mock(return_value=_history(22, 4, "ABC", 77))
    delete = respx.delete(f"{A}/api/v3/episodefile/4").mock(return_value=httpx.Response(200))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(202, json=[]))
    respx.post(f"{A}/api/v3/history/failed/77").mock(return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    removed = await sweep_language(make_registry(), db, OperationStore(db), cap=25, now=NOW)

    assert removed == 1 and delete.called


@respx.mock
async def test_file_without_a_grab_is_warned_not_deleted(db, journal):
    """A manual import has no grab to blocklist: deleting it would just let the
    same release come straight back, so keep the file and say so — once."""
    files = [_file(2719, 46, "fre/jpn", "fre/fre")]
    _mock_library([EVA], {46: files}, {46: [_episode(5730, 46, 1, 2719)]})
    respx.get(f"{A}/api/v3/history").mock(return_value=httpx.Response(200, json={"records": []}))
    delete = respx.delete(url__regex=rf"{A}/api/v3/episodefile/\d+")

    for _ in range(2):
        removed = await sweep_language(make_registry(), db, OperationStore(db), cap=25, now=NOW)
        await journal.flush()

    assert removed == 0 and not delete.called
    (warn,) = await _events(journal, db, kinds.SWEEP_LANGUAGE_UNKNOWN)
    assert warn["data"]["fileIds"] == [2719]
    assert "no grab" in warn["message"].lower()


@respx.mock
async def test_cap_counts_downloads(db, journal):
    files = [_file(1, 46, "fre", "fre"), _file(2, 46, "fre", "fre")]
    eps = [_episode(10, 46, 1, 1), _episode(11, 46, 2, 2)]
    _mock_library([EVA], {46: files}, {46: eps})
    downloads = {10: ("D1", 101), 11: ("D2", 102)}
    respx.get(f"{A}/api/v3/history").mock(side_effect=lambda req: _history(
        int(req.url.params["episodeId"]), {10: 1, 11: 2}[int(req.url.params["episodeId"])],
        *downloads[int(req.url.params["episodeId"])]))
    delete = respx.delete(url__regex=rf"{A}/api/v3/episodefile/\d+").mock(
        return_value=httpx.Response(200))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(202, json=[]))
    respx.post(url__regex=rf"{A}/api/v3/history/failed/\d+").mock(
        return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    removed = await sweep_language(make_registry(), db, OperationStore(db), cap=1, now=NOW)

    assert removed == 1 and delete.call_count == 1


@respx.mock
async def test_one_instance_down_does_not_stop_the_other(db, journal):
    respx.get(f"{B}/api/v3/series").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{A}/api/v3/series").mock(return_value=httpx.Response(200, json=[GEAR]))
    respx.get(f"{A}/api/v3/episodefile").mock(return_value=httpx.Response(200, json=[
        _file(4, 7, "fre")]))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        _episode(22, 7, 3, 4)]))
    respx.get(f"{A}/api/v3/history").mock(return_value=_history(22, 4, "ABC", 77))
    respx.delete(f"{A}/api/v3/episodefile/4").mock(return_value=httpx.Response(200))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(202, json=[]))
    respx.post(f"{A}/api/v3/history/failed/77").mock(return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))

    assert await sweep_language(make_registry(), db, OperationStore(db), cap=25, now=NOW) == 1


@respx.mock
async def test_failed_removal_is_recorded_not_raised(db, journal):
    ops = OperationStore(db)
    _mock_library([GEAR], {7: [_file(4, 7, "fre")]}, {7: [_episode(22, 7, 3, 4)]})
    respx.get(f"{A}/api/v3/history").mock(return_value=_history(22, 4, "ABC", 77))
    respx.delete(f"{A}/api/v3/episodefile/4").mock(return_value=httpx.Response(500))

    removed = await sweep_language(make_registry(), db, ops, cap=25, now=NOW)

    assert removed == 0
    assert (await ops.recent())[0]["error"]


# --- tick wiring --------------------------------------------------------------

def _tick_routes():
    from tests.test_search_stall import _empty
    _empty(B)
    _empty(A)
    # The poller and reconcile phases see no series; only the sweep's scan does.
    respx.get(f"{A}/api/v3/series").mock(side_effect=[
        httpx.Response(200, json=[GEAR])] * 10)
    respx.get(f"{A}/api/v3/episodefile").mock(return_value=httpx.Response(200, json=[
        _file(4, 7, "fre")]))
    respx.get(f"{A}/api/v3/episode").mock(return_value=httpx.Response(200, json=[
        _episode(22, 7, 3, 4)]))
    respx.get(f"{A}/api/v3/history").mock(return_value=_history(22, 4, "ABC", 77))
    respx.put(f"{A}/api/v3/episode/monitor").mock(return_value=httpx.Response(202, json=[]))
    respx.post(f"{A}/api/v3/history/failed/77").mock(return_value=httpx.Response(200))
    respx.post(f"{A}/api/v3/command").mock(return_value=httpx.Response(201, json={"id": 1}))
    return respx.delete(f"{A}/api/v3/episodefile/4").mock(return_value=httpx.Response(200))


@respx.mock
async def test_tick_runs_language_sweep(db, journal):
    from app.reconciler import Reconciler
    from tests.test_chain import _nosleep
    deleted = _tick_routes()
    rec = Reconciler(make_registry(), db, OperationStore(db), clock=lambda: NOW, sleep=_nosleep)

    await rec.tick()

    assert deleted.called
    assert any(op["kind"] == "language-cleanup" for op in await rec.ops.recent())


@respx.mock
async def test_tick_skips_language_sweep_when_disabled(db, journal):
    from app.reconciler import Reconciler
    from tests.test_chain import _nosleep
    deleted = _tick_routes()
    rec = Reconciler(make_registry(), db, OperationStore(db), clock=lambda: NOW,
                     sleep=_nosleep, language_cleanup=False)

    await rec.tick()

    assert not deleted.called


def test_language_cleanup_env_flag(monkeypatch):
    from app.main import _language_cleanup_enabled
    monkeypatch.delenv("LANGUAGE_CLEANUP_ENABLED", raising=False)
    assert _language_cleanup_enabled() is True
    monkeypatch.setenv("LANGUAGE_CLEANUP_ENABLED", "false")
    assert _language_cleanup_enabled() is False
