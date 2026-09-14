"""Shared fixtures. Sonarr-mocking helpers still live in tests/test_chain.py."""
import pytest

from app.db import Database
from app.obs.journal import Journal, set_journal


@pytest.fixture(autouse=True)
def _reset_journal():
    """The journal is process-wide; never let one test's journal leak into another."""
    yield
    set_journal(None)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "relay.db"))
    yield database
    database.close()


@pytest.fixture
def journal(db):
    j = Journal(db)
    set_journal(j)
    return j
