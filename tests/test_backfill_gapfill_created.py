"""Gap fill scoped to the beads one backfill run created (``backfill.timeline.gapfill.on_created``).

The Dolt store is a fake command runner; no network, no live hive."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import beadlog
from beadhive.backfill_gapfill import created_ids, gapfill_created, on_created_enabled
from beadhive.modules.config.contracts import BackfillGapfillConfig, BackfillTimelineConfig
from beadlog_sources import BdGapfillError, BdReadError, GapfillReader, baseline_events

STAMP = "2026-11-12T00:00:00Z"
GROUP = "wd"


def _version(commit: str, when: str, **extra: Any) -> tuple[str, str, dict[str, Any]]:
    row = {"priority": 2, "issue_type": "task", **extra}
    return commit, when, row


def _history(bead: str, closed: bool = True):
    first = _version(
        "a" * 31 + bead[-1], "2026-07-26T09:00:00.000000Z", title=f"Bead {bead}", status="open"
    )
    if not closed:
        return [first]
    return [
        first,
        _version(
            "b" * 31 + bead[-1],
            "2026-08-13T10:00:00.000000Z",
            title=f"Bead {bead}",
            status="closed",
        ),
    ]


# every bead changed once, so an unscoped gap fill would produce events for all of them
DOLT = {f"wd-{n}": _history(f"wd-{n}") for n in (1, 2, 3, 4)}


class Dolt:
    def __init__(self, fail_on_scan: int | None = None) -> None:
        self.scans: list[list[str]] = []
        self.statements: list[str] = []
        self.fail_on_scan = fail_on_scan

    def __call__(self, argv: Sequence[str]) -> str:
        mode, sql = argv[4], argv[5]
        self.statements.append(sql)
        if mode == "--csv":
            ids = re.findall(r"'([^']+)'", sql[sql.index(" IN (") :])
            self.scans.append(ids)
            if self.fail_on_scan == len(self.scans):
                raise BdGapfillError("bd sql timed out after 180s")
            lines = ["id,commit_hash,d,h"]
            for bead in ids:
                for commit, when, row in DOLT.get(bead, ()):
                    lines.append(f"{bead},{commit},{when},{row['status']}")
            return "\n".join(lines) + "\n"
        import json

        points = re.findall(r"AS OF '([A-Za-z0-9]+)' WHERE id = '([^']+)'", sql)
        return json.dumps(
            [
                {"commit_hash": c, "id": b, **row}
                for c, b in points
                for commit, _, row in DOLT[b]
                if commit == c
            ]
        )


def _baseline(*beads: str):
    rows = [
        {
            "id": b,
            "issue_type": "task",
            "status": "closed",
            "title": f"Bead {b}",
            "priority": 2,
            "labels": [],
            "created_at": "2026-08-10T09:00:00Z",
            "updated_at": "2026-08-20T00:00:00Z",
        }
        for b in beads
    ]
    return tuple(baseline_events(rows, group=GROUP, extracted_at=STAMP))


def _factory(dolt: Dolt):
    def make(cwd: Path, settings: BackfillGapfillConfig) -> GapfillReader:
        return GapfillReader(cwd, batch_size=settings.batch_size, runner=dolt)

    return make


ON = BackfillGapfillConfig(on_created=True, batch_size=1, timeout_seconds=5)


def test_only_the_created_ids_are_touched(tmp_path) -> None:
    dolt = Dolt()
    # the hive holds wd-1..wd-4; the run created wd-2 and wd-3 only
    result = gapfill_created(
        tmp_path,
        GROUP,
        ["wd-3", "wd-2"],
        ON,
        baseline=_baseline("wd-1", "wd-2", "wd-3", "wd-4"),
        reader=_factory(dolt),
        extracted_at=STAMP,
    )
    assert result is not None
    assert dolt.scans == [["wd-2"], ["wd-3"]]  # wd-1 and wd-4 never even queried
    assert all(s.startswith("SELECT ") for s in dolt.statements)
    assert result.events and {e.bead.id for e in result.events} == {"wd-2", "wd-3"}
    assert result.beads_queried == 2


def test_events_of_other_beads_are_dropped_even_if_the_reader_returns_them(tmp_path) -> None:
    class Greedy(GapfillReader):
        def read(self, ids, cursor=None):
            return super().read(["wd-1", *ids], cursor)  # a reader that over-reads

    def make(cwd, settings):
        return Greedy(cwd, batch_size=5, runner=Dolt())

    result = gapfill_created(
        tmp_path,
        GROUP,
        ["wd-2"],
        ON,
        baseline=_baseline("wd-1", "wd-2"),
        reader=make,
        extracted_at=STAMP,
    )
    assert result is not None
    assert {e.bead.id for e in result.events} == {"wd-2"}


def test_off_by_default_nothing_runs(tmp_path) -> None:
    def boom(cwd, settings):
        raise AssertionError("the created-bead gap fill ran although on_created is off")

    assert BackfillGapfillConfig().on_created is False
    assert BackfillTimelineConfig().gapfill.on_created is False
    assert on_created_enabled(BackfillGapfillConfig()) is False
    assert on_created_enabled(None) is False
    # `enabled` alone does not switch it on
    assert (
        gapfill_created(tmp_path, GROUP, ["wd-2"], BackfillGapfillConfig(enabled=True), reader=boom)
        is None
    )
    assert gapfill_created(tmp_path, GROUP, ["wd-2"], None, reader=boom) is None


def test_on_created_is_independent_of_enabled_and_no_ids_runs_nothing(tmp_path) -> None:
    dolt = Dolt()
    assert BackfillGapfillConfig(on_created=True).enabled is False
    assert gapfill_created(tmp_path, GROUP, [], ON, reader=_factory(dolt)) is None
    assert dolt.statements == []


def test_config_validation() -> None:
    assert BackfillGapfillConfig.model_validate({"on_created": True}).on_created is True
    with pytest.raises(ValidationError):
        BackfillGapfillConfig.model_validate({"on_created": "sometimes"})
    with pytest.raises(ValidationError):
        BackfillGapfillConfig.model_validate({"on_created": True, "batch_size": 0})
    with pytest.raises(ValidationError):
        BackfillGapfillConfig.model_validate({"on_created": True, "unknown": 1})


def test_bounded_by_batch_size_and_resumable(tmp_path) -> None:
    ids = ["wd-1", "wd-2", "wd-3", "wd-4"]
    state = tmp_path / "run.gapfill.json"
    settings = BackfillGapfillConfig(on_created=True, batch_size=2, timeout_seconds=5)
    base = _baseline(*ids)
    broken = Dolt(fail_on_scan=2)
    with pytest.raises(BdReadError, match="progress is saved in .*re-run to resume"):
        gapfill_created(
            tmp_path,
            GROUP,
            ids,
            settings,
            baseline=base,
            state=state,
            reader=_factory(broken),
            extracted_at=STAMP,
        )
    assert broken.scans == [["wd-1", "wd-2"], ["wd-3", "wd-4"]] and state.exists()
    resume = Dolt()
    result = gapfill_created(
        tmp_path,
        GROUP,
        ids,
        settings,
        baseline=base,
        state=state,
        reader=_factory(resume),
        extracted_at=STAMP,
    )
    assert resume.scans == [["wd-3", "wd-4"]]  # continues at the next batch
    assert result is not None and result.resumed_from == 2
    assert {e.bead.id for e in result.events} == set(ids)


def test_created_ids_come_from_the_create_events() -> None:
    events = _baseline("wd-2", "wd-1")
    assert created_ids(events) == ["wd-1", "wd-2"]
    assert created_ids([e for e in events if e.kind is not beadlog.EventKind.CREATE]) == []
