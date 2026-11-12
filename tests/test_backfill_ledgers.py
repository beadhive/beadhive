"""LedgerDirectory adapters: bd routes + HQ hive registry (no network, no real HQ, no bd)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from beadhive import backfill_ledgers as bl
from beadhive import bd
from beadlog_sources import FOREIGN, LOCAL, UNRESOLVED, LedgerDirectory, resolve


def _cfg():
    def entry(prefix, repo):
        return {"prefix": prefix, "provider": "github", "org": "acme", "repo": repo, "kind": "own"}

    return {
        "managed_repos": [
            entry("aa", "alpha"),
            entry("bb", "beta"),
            {"prefix": "hq", "provider": "local", "org": "factory", "repo": "hq", "kind": "hq"},
        ]
    }


class FakeStores:
    """A fake bd read: store path -> ids; counts reads; marks some paths offline."""

    def __init__(self, root: Path, stores: dict[str, list[str]], offline=()):
        self.root = root
        self.by_path = {str(root / "github" / "acme" / repo): ids for repo, ids in stores.items()}
        self.offline = {str(root / "github" / "acme" / repo) for repo in offline}
        self.reads: list[str] = []

    def __call__(self, path):
        self.reads.append(str(path))
        if str(path) in self.offline:
            raise OSError("unreachable")
        return self.by_path[str(path)]


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    return FakeStores(tmp_path, {"alpha": ["aa-1", "aa-2"], "beta": ["bb-1"]})


def test_two_fixture_ledgers_resolve_and_unregistered_stays_unresolved(stores):
    directory = bl.ledger_directory_for("aa", _cfg(), read_ids=stores)
    assert isinstance(directory, LedgerDirectory)
    assert directory.ledgers() == ("aa", "bb")  # the HQ singleton is not a ledger
    assert resolve(directory, "aa-1", local="aa").kind == LOCAL
    foreign = resolve(directory, "bb-1", local="aa")
    assert (foreign.kind, foreign.ledger) == (FOREIGN, "bb")
    # "cc" is not registered, and "aa-9" only *looks* like aa's: membership, not prefix
    assert resolve(directory, "cc-1", local="aa").kind == UNRESOLVED
    assert resolve(directory, "aa-9", local="aa").kind == UNRESOLVED
    assert directory.warnings == ()


def test_each_ledger_read_once_per_run(stores):
    directory = bl.ledger_directory_for("aa", _cfg(), read_ids=stores)
    for token in ("aa-1", "bb-1", "zz-1", "aa-2", "aa-1"):
        directory.owners(token)
        directory.contains("bb", token)
    assert sorted(stores.reads) == sorted(set(stores.reads))
    assert len(stores.reads) == 2


def test_offline_ledger_is_a_warning_not_an_error(stores, caplog):
    stores.offline = {str(stores.root / "github" / "acme" / "beta")}
    directory = bl.ledger_directory_for("aa", _cfg(), read_ids=stores)
    with caplog.at_level(logging.WARNING, logger=bl.logger.name):
        assert resolve(directory, "bb-1", local="aa").kind == UNRESOLVED
        assert resolve(directory, "aa-1", local="aa").kind == LOCAL
    assert [(w.ledger, w.reason) for w in directory.warnings] == [("bb", "unreachable")]
    assert "bb" in caplog.text
    # failure is cached: asking again does not re-read or re-warn
    directory.owners("bb-2")
    assert len(directory.load_all()) == 1
    assert sum(p.endswith("beta") for p in stores.reads) == 1


def test_explicit_bd_routes_adapter(tmp_path):
    read = {tmp_path / "x": ["x-1"], tmp_path / "y": ["y-1"]}
    directory = bl.bd_routes_directory(
        {"x": tmp_path / "x", "y": str(tmp_path / "y")}, read.__getitem__
    )
    assert directory.owners("y-1") == ("y",)
    assert directory.owners("z-1") == ()


def test_routes_extend_and_override_registry(stores, tmp_path):
    directory = bl.ledger_directory_for(
        "aa",
        _cfg(),
        routes={"extra": tmp_path / "e"},
        read_ids=lambda p: ["e-1"] if p.name == "e" else stores(p),
    )
    assert directory.ledgers() == ("aa", "bb", "extra")
    assert directory.owners("e-1") == ("extra",)


def test_unregistered_local_hive_raises(stores):
    with pytest.raises((SystemExit, Exception)):
        bl.ledger_directory_for("nope", _cfg(), read_ids=stores)


def test_bd_export_ids_reads_through_bd_export(tmp_path, monkeypatch):
    rows = (
        '{"id":"aa-1","status":"open"}\n'
        '{"id":"aa-2","status":"closed"}\n'
        '{"_type":"memory","key":"k"}\n'
    )
    monkeypatch.setattr(bd, "export_jsonl", lambda cwd, all_=True, output=None: rows)
    assert bl.bd_export_ids(tmp_path) == ["aa-1", "aa-2"]
    monkeypatch.setattr(bd, "export_jsonl", lambda cwd, all_=True, output=None: None)
    with pytest.raises(OSError, match="bd export failed"):
        bl.bd_export_ids(tmp_path)
    with pytest.raises(OSError, match="no checkout"):
        bl.bd_export_ids(tmp_path / "missing")
