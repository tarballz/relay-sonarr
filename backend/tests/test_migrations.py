"""One-time data repairs applied when an existing database is opened."""
from app.db import Database


def _insert_op(db, kind, tvdb_id):
    db._conn.execute(
        "INSERT INTO operation(kind, title, tvdb_id, source) VALUES(?, 't', ?, 'reconciler')",
        (kind, tvdb_id),
    )
    db._conn.commit()


def _tvdb_by_kind(db):
    return {r["kind"]: r["tvdb_id"]
            for r in db._conn.execute("SELECT kind, tvdb_id FROM operation")}


def test_sweep_tvdb_repair_nulls_sonarr_series_ids_once(tmp_path):
    path = str(tmp_path / "relay.db")
    db = Database(path)
    _insert_op(db, "stalled-cleanup", 5)    # really a per-instance Sonarr seriesId
    _insert_op(db, "dangerous-cleanup", 0)  # the old `or 0` fallback
    _insert_op(db, "reconcile", 99)         # a genuine tvdb id: untouched
    # Simulate a database written before the repair existed.
    db._conn.execute("DELETE FROM meta WHERE key='fix_sweep_tvdb_v1'")
    db._conn.commit()
    db.close()

    db = Database(path)
    assert _tvdb_by_kind(db) == {
        "stalled-cleanup": None, "dangerous-cleanup": None, "reconcile": 99,
    }

    # Runs once: a post-fix sweep row with a real tvdb id survives the next open.
    db._conn.execute("DELETE FROM operation")
    _insert_op(db, "stalled-cleanup", 77)
    db.close()
    db = Database(path)
    assert _tvdb_by_kind(db) == {"stalled-cleanup": 77}
    db.close()


def test_ticks_left_running_are_marked_interrupted_on_open(tmp_path):
    path = str(tmp_path / "relay.db")
    db = Database(path)
    db._conn.execute(
        "INSERT INTO tick(trigger, started_at, status) VALUES('schedule', '2026-09-14T00:00:00+00:00', 'running')"
    )
    db._conn.commit()
    db.close()

    db = Database(path)
    row = db._conn.execute("SELECT status, error, finished_at FROM tick").fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "interrupted (restart)"
    assert row["finished_at"] == "2026-09-14T00:00:00+00:00"
    db.close()


def test_operation_has_tick_id_column(tmp_path):
    db = Database(str(tmp_path / "relay.db"))
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(operation)")}
    assert "tick_id" in cols
    db.close()


def test_imported_backoff_repair_resets_attempts_once(tmp_path):
    # Silo S01E01 accrued attempts=582 from a stale failure flipping an imported row.
    path = str(tmp_path / "relay.db")
    db = Database(path)
    db._conn.execute(
        "INSERT INTO placement(tvdb_id, season, episode, state, attempts, next_retry_at) "
        "VALUES(403245, 1, 1, 'imported', 582, '2026-10-24T23:26:09+00:00'),"
        "      (1, 1, 1, 'failed', 3, '2026-10-24T00:00:00+00:00')")
    db._conn.execute("DELETE FROM meta WHERE key='reset_imported_backoff_2026_10'")
    db._conn.commit()
    db.close()

    db = Database(path)
    rows = {r["tvdb_id"]: (r["attempts"], r["next_retry_at"])
            for r in db._conn.execute("SELECT * FROM placement")}
    assert rows[403245] == (0, None)
    assert rows[1] == (3, "2026-10-24T00:00:00+00:00")  # real backoff untouched
    db.close()
