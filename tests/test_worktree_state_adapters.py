"""Root adapters for the worktree capability's state ports (bh-qdezo.5, bh-e7s80).

These are the only root tests that reach ``claim_authority`` / ``ghpr`` underneath the
``BeadStateLookup`` / ``ClaimRecords`` / ``MergeEvidence`` ports: every other worktree test
substitutes the composed port in :mod:`beadhive.worktree_state_adapters` instead.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from beadhive import claim_authority, ghpr, worktree_state_adapters
from beadhive.worktree_state_adapters import (
    BeadsSessionBeadStateLookup,
    ClaimAuthorityRecords,
    GhprMergeEvidence,
    RoutedBeadStateLookup,
)


def test_claim_and_merge_adapters_pass_straight_through(monkeypatch, tmp_path) -> None:
    record = tmp_path / "claim.json"
    removed: list[Path | None] = []
    monkeypatch.setattr(claim_authority, "record_path", lambda target: record)
    monkeypatch.setattr(claim_authority, "remove_record_path", removed.append)
    monkeypatch.setattr(ghpr, "merged_pr_for", lambda entry, branch: (entry["repo"], branch))

    records = ClaimAuthorityRecords()
    assert records.record_path(tmp_path) == record
    records.remove_record_path(record)
    assert removed == [record]
    assert GhprMergeEvidence().merged_pr({"repo": "r"}, "wt/x") == ("r", "wt/x")


class _WireRow:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, object]:
        return self.payload


def test_beads_session_lookup_uses_named_core_routes_and_erases_wire_types(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class Table:
        def call_api(self, _session, operation, *args, **kwargs):
            calls.append((operation, args, kwargs))
            if operation == "work.issue.get":
                return _WireRow({"id": args[0], "status": "closed"})
            return SimpleNamespace(items=[_WireRow({"id": "bh-1"})])

    monkeypatch.setattr(
        worktree_state_adapters,
        "_core",
        lambda: SimpleNamespace(default_table=lambda: Table()),
    )
    lookup = BeadsSessionBeadStateLookup(object())
    main = Path("/repo")

    assert lookup.probe(main) == [{"id": "bh-1"}]
    assert lookup.show("bh-2", main) == {"id": "bh-2", "status": "closed"}
    assert lookup.all_issues(main) == [{"id": "bh-1"}]
    assert calls == [
        ("work.issue.list", (), {"limit": 1}),
        ("work.issue.get", ("bh-2",), {}),
        (
            "work.issue.list",
            (),
            {"limit": 0, "all_": True, "include_infra": True},
        ),
    ]


def test_routed_lookup_selects_the_api_before_the_read(monkeypatch) -> None:
    selected: list[tuple[Path, object, frozenset[str]]] = []

    @contextmanager
    def fake_session(main, entry, capabilities):
        selected.append((main, entry, capabilities))
        yield "session"

    monkeypatch.setattr(worktree_state_adapters, "_entry", lambda _main: {"prefix": "bh"})
    monkeypatch.setattr(worktree_state_adapters, "session_factory", fake_session)
    monkeypatch.setattr(
        BeadsSessionBeadStateLookup,
        "show",
        lambda self, bead, main: {"id": bead, "main": str(main)},
    )
    lookup = RoutedBeadStateLookup()

    assert lookup.show("bh-1", Path("/repo")) == {"id": "bh-1", "main": "/repo"}
    assert selected == [(Path("/repo"), {"prefix": "bh"}, frozenset({"issues.get"}))]


def test_routed_lookup_takes_only_the_pre_execution_named_fallback(monkeypatch) -> None:
    class Unavailable(RuntimeError):
        pass

    class Fallback:
        def show(self, bead, main):
            return {"id": bead, "route": "cli", "main": str(main)}

    def unavailable_session(*_args):
        raise Unavailable("service absent")

    allowed: list[tuple[object, BaseException]] = []
    entry = {"prefix": "bh"}
    monkeypatch.setattr(worktree_state_adapters, "_entry", lambda _main: entry)
    monkeypatch.setattr(worktree_state_adapters, "session_factory", unavailable_session)
    monkeypatch.setattr(
        worktree_state_adapters.beads_routing,
        "unavailable_errors",
        lambda: (Unavailable,),
    )
    monkeypatch.setattr(
        worktree_state_adapters.beads_routing,
        "allow_cli_route",
        lambda selected_entry, exc: allowed.append((selected_entry, exc)),
    )

    lookup = RoutedBeadStateLookup(Fallback())
    assert lookup.show("bh-1", Path("/repo")) == {
        "id": "bh-1",
        "route": "cli",
        "main": "/repo",
    }
    assert len(allowed) == 1
    assert allowed[0][0] is entry
    assert isinstance(allowed[0][1], Unavailable)


def test_the_composed_ports_prefer_the_routed_beads_adapter() -> None:
    assert isinstance(worktree_state_adapters.BEAD_STATE_LOOKUP, RoutedBeadStateLookup)
    assert isinstance(worktree_state_adapters.CLAIM_RECORDS, ClaimAuthorityRecords)
    assert isinstance(worktree_state_adapters.MERGE_EVIDENCE, GhprMergeEvidence)
