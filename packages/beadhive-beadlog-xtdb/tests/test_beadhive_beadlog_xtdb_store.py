"""The bound store: lazy connection, loopback guard, failure translation, no secret in a message.

``beadlog_xtdb.XtdbStoreLoader`` and ``read_state`` are replaced on the library module (the store
imports them lazily, at call time), so nothing here needs a node or a real psycopg connection.
"""

from __future__ import annotations

import pytest

import beadlog_xtdb
from beadhive.modules.beadlog_store.contracts import (
    BeadlogStoreError,
    BeadlogStoreLoader,
    StoreSnapshot,
)
from beadhive_beadlog_xtdb.settings import XtdbSettings
from beadhive_beadlog_xtdb.store import XtdbBeadlogStore, scrub
from xtdb_plugin_fakes import SECRET, Connector, FakeInnerLoader

GROUP = "urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60"


def store_for(dsn: str = "host=127.0.0.1 port=15470", *, connector: Connector, password=SECRET):
    calls: list[str] = []

    def source():
        calls.append("password")
        return password

    store = XtdbBeadlogStore(XtdbSettings(enabled=True, dsn=dsn), source, connect=connector)
    return store, calls


@pytest.fixture
def inner(monkeypatch):
    made: list[FakeInnerLoader] = []

    def factory(connection, *, target):
        loader = FakeInnerLoader()
        loader.connection, loader.target_seen = connection, target
        made.append(loader)
        return loader

    monkeypatch.setattr(beadlog_xtdb, "XtdbStoreLoader", factory)
    return made


def test_the_loader_connects_once_with_the_resolved_password(inner) -> None:
    connector = Connector()
    store, calls = store_for(connector=connector)

    loader = store.loader()
    assert store.loader() is loader
    assert isinstance(loader, BeadlogStoreLoader)
    assert calls == ["password"]
    assert connector.calls == [("host=127.0.0.1 port=15470", SECRET)]
    assert (loader.name, loader.version, loader.target) == (
        "xtdb",
        "0.1.0",
        "xtdb://xtdb@127.0.0.1:15470/xtdb",
    )
    assert loader.load([], dry_run=True) == {"mode": "dry-run"}
    assert tuple(loader.export()) == ()

    store.close()
    assert connector.opened[0].closed
    store.close()


def test_a_non_loopback_dsn_is_refused_before_the_password_is_resolved(inner) -> None:
    connector = Connector()
    store, calls = store_for("host=10.0.0.5 port=5432", connector=connector)

    with pytest.raises(BeadlogStoreError, match="non-loopback host"):
        store.loader()
    assert calls == [] and connector.calls == []


def test_a_connect_failure_is_one_clean_line_without_the_password(inner) -> None:
    failure = OSError(f'connection to "127.0.0.1" failed: password {SECRET} rejected\nDETAIL: x')
    store, _calls = store_for(connector=Connector(fail=failure))

    with pytest.raises(BeadlogStoreError) as caught:
        store.loader()
    message = str(caught.value)
    assert SECRET not in message and "***" in message
    assert "\n" not in message
    assert "bh plugin beadlog-xtdb doctor" in message


@pytest.mark.parametrize(
    "failure",
    [RuntimeError(f"aborted in transaction 2: {SECRET}"), ConnectionError("server closed")],
)
def test_the_guard_turns_aborts_and_driver_errors_into_store_errors(monkeypatch, failure) -> None:
    monkeypatch.setattr(
        beadlog_xtdb, "XtdbStoreLoader", lambda connection, *, target: FakeInnerLoader(failure)
    )
    store, _calls = store_for(connector=Connector())
    loader = store.loader()

    for call in (lambda: loader.load([], dry_run=False), loader.export):
        with pytest.raises(BeadlogStoreError) as caught:
            call()
        assert SECRET not in str(caught.value)
        assert str(caught.value).startswith("XTDB store xtdb://xtdb@127.0.0.1:15470/xtdb: ")


def test_a_refusal_that_is_a_value_error_passes_through(monkeypatch) -> None:
    refusal = beadlog_xtdb.TargetNotFreshError("an initial load needs a fresh database")
    monkeypatch.setattr(
        beadlog_xtdb, "XtdbStoreLoader", lambda connection, *, target: FakeInnerLoader(refusal)
    )
    store, _calls = store_for(connector=Connector())

    with pytest.raises(beadlog_xtdb.TargetNotFreshError):
        store.loader().load([], dry_run=True)


class _Result:
    all_events = ("e1", "e2")

    def note_codes(self):
        return {"comment.id-not-preserved": 1}


def test_snapshot_reads_the_current_state_of_one_group(monkeypatch) -> None:
    seen: dict = {}

    def read_state(connection, **kwargs):
        seen.update(kwargs, connection=connection)
        return _Result()

    monkeypatch.setattr(beadlog_xtdb, "read_state", read_state)
    connector = Connector()
    store, _calls = store_for(connector=connector)

    snapshot = store.snapshot(group=GROUP, extracted_at="2026-11-02T00:00:00Z")

    assert snapshot == StoreSnapshot(
        "xtdb://xtdb@127.0.0.1:15470/xtdb", ("e1", "e2"), {"comment.id-not-preserved": 1}
    )
    assert seen.pop("connection") is connector.opened[0]  # every table exists: read directly
    assert seen == {"extracted_at": "2026-11-02T00:00:00Z", "group": GROUP, "groups": [GROUP]}


def test_snapshot_reads_a_table_xtdb_never_created_as_empty(monkeypatch) -> None:
    """XTDB creates a table on first insert: a store without edges has no ``bead_edges``."""
    selected: dict = {}

    def read_state(connection, **kwargs):
        for table in ("beads", "bead_edges"):
            cursor = connection.execute(f"SELECT *, _valid_from FROM {table}", [])
            selected[table] = cursor.fetchall()
        return _Result()

    monkeypatch.setattr(beadlog_xtdb, "read_state", read_state)
    connector = Connector()
    store, _calls = store_for(connector=connector)
    store._connect().tables = ("beads", "bead_labels")

    store.snapshot(group=GROUP, extracted_at="2026-11-02T00:00:00Z")

    assert selected == {"beads": [("row",)], "bead_edges": []}
    assert not any("bead_edges" in sql for sql in connector.opened[0].statements)


def test_snapshot_of_a_store_that_never_held_a_bead_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(beadlog_xtdb, "read_state", lambda *a, **k: pytest.fail("no bead table"))
    connector = Connector()
    store, _calls = store_for(connector=connector)
    store._connect().tables = ()

    snapshot = store.snapshot(group=GROUP, extracted_at="2026-11-02T00:00:00Z")
    assert snapshot == StoreSnapshot("xtdb://xtdb@127.0.0.1:15470/xtdb", (), {})


def test_scrub_keeps_the_first_line_and_masks_the_secret() -> None:
    assert scrub(f"a {SECRET} b\nsecond", SECRET) == "a *** b"
    assert scrub("plain", None) == "plain"
    assert scrub("", SECRET) == ""
