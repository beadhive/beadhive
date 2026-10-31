"""First-parent land parser: the three land variants, refresh/merge-main ignored, one git call.

``REAL_LOG`` lines are verbatim ``git log --format='%H%x09%cI%x09%s'`` rows from this
repository (refresh and merge-main subjects live on the epic container branches, not on
``main``), including committer dates with a local offset.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from beadhive_backfill.merges import (
    LandKind,
    classify_subject,
    first_parent_argv,
    parse_first_parent,
    read_land_merges,
)

REAL_LOG = "\n".join(
    "\t".join(row)
    for row in [
        (
            "b590eb143b195b3c449d1a735f4f096a196fe087",
            "2026-10-31T13:12:07Z",
            "chore(merge): molecule bh-ebopn",
        ),
        (
            "9e6f45efd6e847613e9a3f9fd0b88c09354b6f07",
            "2026-10-30T22:31:37Z",
            "chore(merge): bead bh-gf10j",
        ),
        (
            "e05c1ffacb7bc8b2421947ef87ef8838031ef879",
            "2026-10-30T20:44:20Z",
            "docs(backfill): record the backfill platform ADR",
        ),
        (
            "4d4b6b7315b4d6ce48a9d2bb080e8ebf84bf0554",
            "2026-10-30T01:56:28Z",
            "bump: version 0.31.0 → 0.32.0",
        ),
        (
            "69424ec8ea3416ceb4391d4e51b5d2c28c1ef8d3",
            "2026-10-15T19:39:46Z",
            "chore(merge): batch authority-p2",
        ),
        (
            "694d6a2fc55d093902fe5a803386919caa1694d1",
            "2026-09-22T18:04:06Z",
            "chore(merge): batch bh-xg1r9",
        ),
        (
            "6e4513e082603cd0501ad7ba44aa8b754361635d",
            "2026-09-15T22:57:50-07:00",
            "chore(merge): bead bh-f62lu.1",
        ),
        (
            "3fc64cbf23d47ae332a8b682ed47bd62cbd2ffeb",
            "2026-07-12T13:57:52-07:00",
            "chore(merge): merge bh-deid",
        ),
        (
            "822a4058fcee0dc66e6ca5c5ec7181d39ff2705f",
            "2026-07-11T21:05:08-07:00",
            "merge molecule bh-nf1",
        ),
        ("e4ab0d52e5b878293e7fd053716c887489ffe7d0", "2026-07-11T18:27:35-07:00", "merge bh-6iv"),
        (
            "e8f9ba8686417364458e88d347e5ba9c07bc793c",
            "2026-10-31T08:08:49Z",
            "chore(merge): refresh wt/bead/epic/bh-ebopn from main",
        ),
        (
            "6bc314196c24a95385545ef6fc4243ddcb568d08",
            "2026-08-24T08:39:20Z",
            "Merge branch 'main' into wt/bead/epic/bh-e8s3i",
        ),
        (
            "4adaa5f39744d3f1e700a21a4d7651fbe33a9398",
            "2026-09-21T06:17:54Z",
            "chore(merge): checkpoint E4 latency wave bh-mgn1k",
        ),
        (
            "2f3b567048f3450aa26a109c4c3453855c616b93",
            "2026-09-18T18:55:07-07:00",
            "chore(merge): reconcile main with origin/main",
        ),
        (
            "569e84f8907dc1223c8419acfc93589f7c9bdf41",
            "2026-10-05T11:10:15-07:00",
            "chore(merge): docs/bead-groups-north-star",
        ),
    ]
)


def test_classifies_the_three_land_variants() -> None:
    scan = parse_first_parent(REAL_LOG.splitlines())
    current = [merge for merge in scan.merges if not merge.legacy]
    assert [(m.kind, m.target) for m in current] == [
        (LandKind.MOLECULE, "bh-ebopn"),
        (LandKind.BEAD, "bh-gf10j"),
        (LandKind.BATCH, "authority-p2"),  # a batch names its group, not its members
        (LandKind.BATCH, "bh-xg1r9"),
        (LandKind.BEAD, "bh-f62lu.1"),  # dotted child id stays whole
    ]
    first = scan.merges[0]
    assert first.sha == "b590eb143b195b3c449d1a735f4f096a196fe087"
    assert first.committed_at == "2026-10-31T13:12:07Z"
    assert first.subject == "chore(merge): molecule bh-ebopn"


def test_committer_dates_normalize_to_utc() -> None:
    scan = parse_first_parent(REAL_LOG.splitlines())
    dotted = next(m for m in scan.merges if m.target == "bh-f62lu.1")
    assert dotted.committed_at == "2026-09-16T05:57:50Z"


def test_refresh_and_merge_main_subjects_are_ignored_and_counted() -> None:
    scan = parse_first_parent(REAL_LOG.splitlines())
    targets = {merge.target for merge in scan.merges}
    assert "bh-xxkf7" not in targets and "bh-e8s3i" not in targets
    assert scan.skipped["refresh"] == 1
    assert scan.skipped["merge_main"] == 1
    # version bumps, docs commits and the other chore(merge) housekeeping are not lands either
    assert scan.skipped["other"] == 5
    assert not scan.skipped["malformed_line"]


@pytest.mark.parametrize(
    "subject",
    [
        "chore(merge): refresh wt/bead/epic/bh-xxkf7 from main",
        "chore(merge): refresh wt/bead/epic/bh-qdezo from main",
        "Merge branch 'main' into wt/bead/epic/bh-e8s3i",
        "Merge remote-tracking branch 'origin/main' into wt/bead/epic/bh-e8s3i",
    ],
)
def test_never_a_land(subject: str) -> None:
    assert classify_subject(subject) in {"refresh", "merge_main"}


def test_pre_convention_subjects_are_flagged_legacy() -> None:
    legacy = [m for m in parse_first_parent(REAL_LOG.splitlines()).merges if m.legacy]
    assert [(m.kind, m.target, m.subject) for m in legacy] == [
        (LandKind.BEAD, "bh-deid", "chore(merge): merge bh-deid"),
        (LandKind.MOLECULE, "bh-nf1", "merge molecule bh-nf1"),
        (LandKind.BEAD, "bh-6iv", "merge bh-6iv"),
    ]


def test_malformed_lines_are_counted_not_fatal() -> None:
    scan = parse_first_parent(
        [
            "not a log line",
            "zz\t2026-10-31T13:12:07Z\tchore(merge): bead bh-a",
            "b590eb143b195b3c449d1a735f4f096a196fe087\tyesterday\tchore(merge): bead bh-a",
            "",
            "b590eb143b195b3c449d1a735f4f096a196fe087\t2026-10-31T13:12:07Z\tchore(merge): bead bh-a",  # noqa: E501
        ]
    )
    assert [m.target for m in scan.merges] == ["bh-a"]
    assert dict(scan.skipped) == {"malformed_line": 2, "bad_date": 1}


def test_full_history_is_one_git_call() -> None:
    calls: list[list[str]] = []

    def fake(argv: Sequence[str]) -> str:
        calls.append(list(argv))
        return REAL_LOG + "\n"

    scan = read_land_merges("/repo", "main", runner=fake)
    assert len(scan.merges) == 8
    assert len(calls) == 1
    assert calls[0] == first_parent_argv("main", root="/repo")
    assert calls[0][:4] == ["git", "-C", "/repo", "log"] and "--first-parent" in calls[0]
    assert calls[0][-2:] == ["main", "--"]
