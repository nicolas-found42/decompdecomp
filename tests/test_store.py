"""Readers reuse a bounded set of connections without sharing a transaction."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from decompnet import store


def test_many_read_threads_open_only_a_bounded_number_of_connections(tmp_path, monkeypatch):
    db = store.Store(tmp_path / "project.db")
    with db.tx() as writer:
        writer.execute("INSERT INTO settings VALUES('example','value')")
    connect = sqlite3.connect
    opened = []

    def counted(*args, **kwargs):
        opened.append(True)
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counted)
    workers = 24
    start = threading.Barrier(workers)

    def read():
        start.wait(5)
        return db.one("SELECT value FROM settings WHERE key='example'")[0]

    with ThreadPoolExecutor(workers) as pool:
        assert list(pool.map(lambda _: read(), range(workers))) == ["value"] * workers
    assert 0 < len(opened) <= 12


def test_close_releases_the_writer_and_pooled_readers(tmp_path):
    db = store.Store(tmp_path / "project.db")
    with db._reader() as first, db._reader() as second:
        assert first is not second

    db.close()
    db.close()

    for connection in (db.conn, first, second):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.one("SELECT 1")


@pytest.mark.parametrize("path", ["disk", ":memory:"])
def test_a_read_inside_a_write_sees_uncommitted_changes_and_rollback(tmp_path, path):
    db = store.Store(tmp_path / "project.db" if path == "disk" else path)
    with pytest.raises(ValueError), db.tx() as writer:
        writer.execute("INSERT INTO settings VALUES('example','temporary')")
        assert db.one("SELECT value FROM settings WHERE key='example'")[0] == "temporary"
        raise ValueError("rollback")
    assert db.one("SELECT value FROM settings WHERE key='example'") is None
    with db.tx() as writer:
        writer.execute("INSERT INTO settings VALUES('example','committed')")
    assert db.one("SELECT value FROM settings WHERE key='example'")[0] == "committed"
