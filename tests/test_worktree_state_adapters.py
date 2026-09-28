"""Root's argv-era adapters for the worktree capability's state ports (bh-qdezo.5, bh-qdezo.9).

These are the only root tests that reach ``bd`` / ``claim_authority`` / ``ghpr`` underneath the
``BeadStateLookup`` / ``ClaimRecords`` / ``MergeEvidence`` ports: every other worktree test
substitutes the composed port in :mod:`beadhive.worktree_state_adapters` instead.
"""

from __future__ import annotations

from pathlib import Path

from beadhive import bd, claim_authority, ghpr, worktree_state_adapters
from beadhive.worktree_state_adapters import (
    ArgvBeadStateLookup,
    ClaimAuthorityRecords,
    GhprMergeEvidence,
)


def test_argv_bead_state_lookup_keeps_the_exact_bd_argv_shapes(monkeypatch) -> None:
    calls: list[tuple[str, object, str]] = []
    monkeypatch.setattr(bd, "json", lambda args, cwd: calls.append(("json", args, cwd)) or [])
    monkeypatch.setattr(bd, "show", lambda bead, cwd: calls.append(("show", bead, cwd)) or {})
    lookup = ArgvBeadStateLookup()
    main = Path("/repo")

    assert lookup.probe(main) == []
    assert lookup.show("bh-1", main) == {}
    assert lookup.all_issues(main) == []

    assert calls == [
        ("json", ["list"], "/repo"),
        ("show", "bh-1", "/repo"),
        ("json", ["list", "--all", "--include-infra", "--limit", "0"], "/repo"),
    ]


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


def test_the_composed_ports_are_the_argv_adapters_by_default() -> None:
    assert isinstance(worktree_state_adapters.BEAD_STATE_LOOKUP, ArgvBeadStateLookup)
    assert isinstance(worktree_state_adapters.CLAIM_RECORDS, ClaimAuthorityRecords)
    assert isinstance(worktree_state_adapters.MERGE_EVIDENCE, GhprMergeEvidence)
