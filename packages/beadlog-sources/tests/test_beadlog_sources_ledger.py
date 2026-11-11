"""beadlog-sources: the ledger directory port and its in-memory fake."""

from __future__ import annotations

import pytest

from beadlog_sources import (
    AMBIGUOUS,
    FOREIGN,
    LOCAL,
    UNRESOLVED,
    CachedLedgerDirectory,
    LedgerDirectory,
    LedgerError,
    Resolution,
    resolve,
    resolve_ref,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory


def _directory() -> InMemoryLedgerDirectory:
    return InMemoryLedgerDirectory({"bh": ["bh-1", "bh-2", "shared-1"], "hq": ["hq-9", "shared-1"]})


def test_fake_satisfies_port() -> None:
    assert isinstance(_directory(), LedgerDirectory)


def test_local_foreign_unresolved() -> None:
    d = _directory()
    assert resolve(d, "bh-1", local="bh") == Resolution(LOCAL, "bh-1", ledger="bh")
    assert resolve(d, "hq-9", local="bh") == Resolution(FOREIGN, "hq-9", ledger="hq")
    assert resolve(d, "bh-404", local="bh") == Resolution(UNRESOLVED, "bh-404")


def test_local_wins_and_foreign_owner_is_shadow_candidate() -> None:
    r = resolve(_directory(), "shared-1", local="bh")
    assert r == Resolution(LOCAL, "shared-1", ledger="bh", candidates=("hq",))


def test_two_foreign_ledgers_is_ambiguous_not_guessed() -> None:
    r = resolve(_directory(), "shared-1", local="other")
    assert r.kind == AMBIGUOUS
    assert r.ledger is None
    assert r.candidates == ("bh", "hq")


def test_unregistered_prefix_with_matching_shape_stays_unresolved() -> None:
    d = _directory()
    # shaped like a bh id, prefix of a registered ledger, but not in any id set
    assert resolve(d, "bh-3", local="bh").kind == UNRESOLVED
    # shaped like an id of an unregistered ledger
    assert resolve(d, "zz-1", local="bh").kind == UNRESOLVED
    assert d.owners("zz-1") == ()


def test_contains_and_unregistered_ledger() -> None:
    d = _directory()
    assert d.contains("bh", "bh-1")
    assert not d.contains("hq", "bh-1")
    assert not d.contains("nope", "bh-1")
    assert d.ledgers() == ("bh", "hq")


def test_id_sets_are_loaded_once_per_run() -> None:
    d = _directory()
    for _ in range(3):
        d.owners("bh-1")
        d.contains("hq", "hq-9")
    assert d.loads == {"bh": 1, "hq": 1}


def test_loader_is_lazy() -> None:
    calls: list[str] = []

    def load() -> list[str]:
        calls.append("x")
        return ["x-1"]

    d = CachedLedgerDirectory({"x": load})
    assert calls == []
    assert d.contains("x", "x-1")
    assert d.contains("x", "x-1")
    assert calls == ["x"]


def test_ids_of_unregistered_ledger_raises() -> None:
    with pytest.raises(LedgerError):
        _directory().ids("nope")


def test_empty_ledger_name_refused() -> None:
    with pytest.raises(LedgerError):
        CachedLedgerDirectory({"": lambda: []})


def test_unknown_resolution_kind_refused() -> None:
    with pytest.raises(LedgerError):
        Resolution("maybe", "bh-1")


def test_resolve_ref_only_bead_tokens() -> None:
    d = _directory()
    assert resolve_ref(d, ev.ref("hq-9"), local="bh") == Resolution(FOREIGN, "hq-9", ledger="hq")
    assert resolve_ref(d, ev.ref("a1b2c3d", kind="sha"), local="bh") is None
