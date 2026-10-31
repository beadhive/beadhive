"""The bd ``events``-table reader: one bounded SELECT, every production row shape, counted skips.

No real ``bd`` runs here. ``REAL_ROWS`` are rows of the production ``events`` table (36,979 rows,
2026-08-07 to 2026-10-31; ``old_value`` JSON trimmed to a few members), one per shape.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from typing import Any

import pytest

from beadlog_sources import BdHistoryError, HistoryEvent, history_argv, parse_history, read_history
from beadlog_sources.bd_history import HISTORY_SQL, decode_rows, parse_event_row


def js(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))


REAL_ROWS: list[dict[str, Any]] = [
    {
        "issue_id": "bh-lko4x.1",
        "event_type": "claimed",
        "actor": "dev/gc-pack",
        "old_value": js(
            {
                "id": "bh-lko4x.1",
                "title": "Map Beadflow AGF to a registry-ready Gas City pack",
                "status": "open",
                "priority": 2,
                "assignee": "dev/gc-pack",
            }
        ),
        "new_value": js({"assignee": "dev/gc-pack", "status": "in_progress"}),
        "comment": None,
        "created_at": "2026-09-28T14:15:48Z",
        "id": "005a9b8e-237e-5c7d-9f7a-dde69c67f2f9",
    },
    {
        "issue_id": "bh-merge-slot",
        "event_type": "status_changed",
        "actor": "Brian Cripe",
        "old_value": js(
            {
                "id": "bh-merge-slot",
                "title": "Merge Slot",
                "status": "in_progress",
                "priority": 0,
                "metadata": {"holder": "Brian Cripe|host=6ae345b9|pid=2883545|ts=1793315699"},
            }
        ),
        "new_value": js({"metadata": "{}", "status": "open"}),
        "comment": None,
        "created_at": "2026-10-29T23:15:34Z",
        "id": "00394573-e3d4-59f5-8bdf-72185903bc68",
    },
    {
        "issue_id": "bh-zu7l",
        "event_type": "status_changed",
        "actor": "bd-defer-wake",
        "old_value": "deferred",
        "new_value": "open",
        "comment": None,
        "created_at": "2026-09-26T05:14:33Z",
        "id": "03062b6d-8056-5e47-8d35-b3f214f72f5a",
    },
    {
        "issue_id": "bh-crm1w",
        "event_type": "closed",
        "actor": "Brian Cripe",
        "old_value": "",
        "new_value": "",
        "comment": None,
        "created_at": "2026-10-11T06:07:52Z",
        "id": "00946517-a9d2-5b64-a0c5-12f6d9f9cb26",
    },
    {
        "issue_id": "bh-qt874",
        "event_type": "closed",
        "actor": "dev/cursor",
        "old_value": "",
        "new_value": "merged",
        "comment": None,
        "created_at": "2026-10-13T22:23:30Z",
        "id": "0004a758-f229-578b-b815-1a0f32d87007",
    },
    {
        "issue_id": "bh-2p33n",
        "event_type": "closed",
        "actor": "Brian Cripe",
        "old_value": js(
            {
                "id": "bh-2p33n",
                "title": "Bound stateful attestation concurrency to fixture capacity",
                "status": "in_progress",
                "priority": 2,
                "assignee": "disp/stateful-capacity",
            }
        ),
        "new_value": js(
            {
                "assignee": "",
                "notes": "Superseded after the same reviewed fix landed on main throug",
                "status": "closed",
            }
        ),
        "comment": None,
        "created_at": "2026-09-21T17:52:52Z",
        "id": "a1125298-203a-59cd-89b5-a85fcb06b8d1",
    },
    {
        "issue_id": "bh-q0lol",
        "event_type": "reopened",
        "actor": "disp/codex-root",
        "old_value": "",
        "new_value": "Recover accidental local-main landing; re-land only into bh-j5uyb",
        "comment": None,
        "created_at": "2026-09-06T06:32:38Z",
        "id": "179ef9a5-6023-59f8-86ba-0ccf28aa079f",
    },
    {
        "issue_id": "bh-t5l6a",
        "event_type": "reopened",
        "actor": "dev/operator-xeno",
        "old_value": js(
            {
                "id": "bh-t5l6a",
                "title": "fix(validation): honor configured profiles at every gate bou",
                "notes": "Regression evidence: schema/resolver rejected configured out",
                "status": "closed",
                "priority": 2,
                "assignee": "dev/operator-xeno",
                "close_reason": "merged",
                "metadata": {"git.commits": '["733b9cfb4e84f9a52d11715a8ad25d596a942c95"]'},
            }
        ),
        "new_value": js(
            {
                "notes": "Code fix merged with all ten keys green: 10450 stateful test",
                "status": "in_progress",
            }
        ),
        "comment": None,
        "created_at": "2026-10-14T07:21:51Z",
        "id": "639028b0-f7af-531d-bc3c-af33615432c5",
    },
    {
        "issue_id": "bh-rpnfx",
        "event_type": "label_added",
        "actor": "Brian Cripe",
        "old_value": None,
        "new_value": None,
        "comment": "Added label: provider:github",
        "created_at": "2026-09-26T14:50:19Z",
        "id": "00030648-95fd-5cdc-8c4d-1275790dec20",
    },
    {
        "issue_id": "bh-5wu0u",
        "event_type": "label_added",
        "actor": "Brian Cripe",
        "old_value": "",
        "new_value": "component:backfill",
        "comment": None,
        "created_at": "2026-10-30T19:23:22Z",
        "id": "002fc02a-ef56-5606-99a5-70e589471e48",
    },
    {
        "issue_id": "bh-wisp-p7v",
        "event_type": "label_added",
        "actor": "Brian Cripe",
        "old_value": "",
        "new_value": "",
        "comment": "Added label: repo:beadhive",
        "created_at": "2026-08-09T11:20:18Z",
        "id": "d71ee901-8960-5b0f-8a4c-472e78bb88d6",
    },
    {
        "issue_id": "bh-ummb9.4",
        "event_type": "label_removed",
        "actor": "disp/bees",
        "old_value": None,
        "new_value": None,
        "comment": "Removed label: review:pending",
        "created_at": "2026-08-20T02:47:41Z",
        "id": "003af568-6b2b-5740-8690-6ab8fbaa0aee",
    },
    {
        "issue_id": "bh-lniwf.1",
        "event_type": "label_removed",
        "actor": "Brian Cripe",
        "old_value": "review:pending",
        "new_value": "",
        "comment": None,
        "created_at": "2026-10-16T13:44:31Z",
        "id": "00891ac6-89d7-53c9-96eb-39adbacb2d4b",
    },
    {
        "issue_id": "bh-7ip8t",
        "event_type": "dependency_added",
        "actor": "Brian Cripe",
        "old_value": "",
        "new_value": "Added dependency: bh-7ip8t related bh-lsu9l",
        "comment": None,
        "created_at": "2026-09-28T13:44:11Z",
        "id": "00362b74-8998-5240-9080-a47c046fc8a1",
    },
    {
        "issue_id": "bh-i6e5g",
        "event_type": "dependency_removed",
        "actor": "Brian Cripe",
        "old_value": "",
        "new_value": "Removed dependency on bh-b5v4y",
        "comment": None,
        "created_at": "2026-08-19T03:46:21Z",
        "id": "032cf831-2c55-5625-adc0-5e96e4cd5d41",
    },
    {
        "issue_id": "bh-0s715",
        "event_type": "commented",
        "actor": "plan/herdr-presentation",
        "old_value": None,
        "new_value": None,
        "comment": "Post-main Herdr presentation follow-up replan: execute bh-0s715.7.3 in fanout.",
        "created_at": "2026-08-28T15:36:30Z",
        "id": "0039ee04-841a-5059-a31d-0470e98026b1",
    },
    {
        "issue_id": "bh-32379",
        "event_type": "lease_reclaimed",
        "actor": "disp/claude",
        "old_value": "dev/migration",
        "new_value": "",
        "comment": None,
        "created_at": "2026-10-05T17:14:35Z",
        "id": "0443ecdf-b4fd-526e-a0eb-8becf1cee794",
    },
    {
        "issue_id": "bh-ajt7i",
        "event_type": "created",
        "actor": "dev/r1fku-discovery",
        "old_value": "",
        "new_value": "",
        "comment": None,
        "created_at": "2026-08-26T06:50:50Z",
        "id": "0000c829-17a3-517b-8236-d0d418f72e0f",
    },
    {
        "issue_id": "bh-5xf0l",
        "event_type": "updated",
        "actor": "Brian Cripe",
        "old_value": "",
        "new_value": "",
        "comment": None,
        "created_at": "2026-10-12T14:35:44Z",
        "id": "003905ac-a66e-52b8-836e-6319e7bb045c",
    },
    {
        "issue_id": "bh-ym56t.5",
        "event_type": "updated",
        "actor": "Brian Cripe",
        "old_value": js(
            {
                "id": "bh-ym56t.5",
                "title": "Map the Herdr integration god module into cohesive implement",
                "status": "open",
                "priority": 2,
            }
        ),
        "new_value": js({"notes": "DEEP REPLAN 2026-09-25: do not decompose or move Herdr workt"}),
        "comment": None,
        "created_at": "2026-09-25T02:24:57Z",
        "id": "006734f0-68d1-5733-8e43-02755c148995",
    },
]
BY_ID = {row["id"]: row for row in REAL_ROWS}


def parsed(row_id: str) -> HistoryEvent:
    result = parse_event_row(BY_ID[row_id])
    assert isinstance(result, HistoryEvent), result
    return result


class CountingRunner:
    """A fake ``bd`` that returns ``rows`` as ``bd sql --json`` output and counts its calls."""

    def __init__(self, rows: Any, *, banner: str = "") -> None:
        self.output = banner + json.dumps(rows, indent=2)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(list(argv))
        return self.output


# -- row shapes seen in production ---------------------------------------------------------


def test_claimed_carries_old_new_json() -> None:
    event = parsed("005a9b8e-237e-5c7d-9f7a-dde69c67f2f9")
    assert (event.kind, event.bead, event.actor) == ("claimed", "bh-lko4x.1", "dev/gc-pack")
    assert (event.assignee, event.old_status, event.new_status) == (
        "dev/gc-pack",
        "open",
        "in_progress",
    )
    assert event.at == "2026-09-28T14:15:48Z"


def test_status_changed_json_and_defer_wake_text() -> None:
    json_form = parsed("00394573-e3d4-59f5-8bdf-72185903bc68")
    assert (json_form.old_status, json_form.new_status) == ("in_progress", "open")
    assert json_form.changes["metadata"] == "{}"
    text_form = parsed("03062b6d-8056-5e47-8d35-b3f214f72f5a")
    assert text_form.actor == "bd-defer-wake"
    assert (text_form.old_status, text_form.new_status) == ("deferred", "open")
    assert not text_form.changes


def test_closed_reason_text_empty_and_json() -> None:
    assert parsed("0004a758-f229-578b-b815-1a0f32d87007").reason == "merged"
    bare = parsed("00946517-a9d2-5b64-a0c5-12f6d9f9cb26")
    assert (bare.reason, bare.new_status) == (None, "closed")
    as_json = parsed("a1125298-203a-59cd-89b5-a85fcb06b8d1")
    assert as_json.new_status == "closed"
    assert as_json.old_status == "in_progress"
    assert as_json.reason and as_json.reason.startswith("Superseded after")


def test_reopened_text_reason_and_json_payload() -> None:
    text = parsed("179ef9a5-6023-59f8-86ba-0ccf28aa079f")
    assert text.reason and text.reason.startswith("Recover accidental local-main landing")
    assert text.new_status == "open"
    as_json = parsed("639028b0-f7af-531d-bc3c-af33615432c5")
    assert (as_json.old_status, as_json.new_status) == ("closed", "in_progress")


def test_label_comment_only_and_value_forms() -> None:
    assert parsed("00030648-95fd-5cdc-8c4d-1275790dec20").label == "provider:github"  # comment
    assert parsed("002fc02a-ef56-5606-99a5-70e589471e48").label == "component:backfill"  # new_value
    assert parsed("d71ee901-8960-5b0f-8a4c-472e78bb88d6").label == "repo:beadhive"  # both set
    assert parsed("003af568-6b2b-5740-8690-6ab8fbaa0aee").label == "review:pending"  # comment
    removed = parsed("00891ac6-89d7-53c9-96eb-39adbacb2d4b")  # old_value only
    assert (removed.kind, removed.label) == ("label_removed", "review:pending")


def test_dependency_text_added_has_type_removed_does_not() -> None:
    added = parsed("00362b74-8998-5240-9080-a47c046fc8a1")
    assert (added.bead, added.dep_type, added.dep_target) == ("bh-7ip8t", "related", "bh-lsu9l")
    removed = parsed("032cf831-2c55-5625-adc0-5e96e4cd5d41")
    assert (removed.bead, removed.dep_type, removed.dep_target) == ("bh-i6e5g", None, "bh-b5v4y")


@pytest.mark.parametrize("dep_type", ["blocks", "parent-child", "relates-to", "discovered-from"])
def test_dependency_types_with_hyphens(dep_type: str) -> None:
    row = dict(BY_ID["00362b74-8998-5240-9080-a47c046fc8a1"])
    row["new_value"] = f"Added dependency: bh-a1.2 {dep_type} bh-b3"
    event = parse_event_row(row)
    assert isinstance(event, HistoryEvent)
    assert (event.dep_type, event.dep_target) == (dep_type, "bh-b3")


def test_commented_lease_reclaimed_created_and_empty_updated() -> None:
    comment = parsed("0039ee04-841a-5059-a31d-0470e98026b1")
    assert comment.kind == "commented"
    assert comment.reason and comment.reason.startswith("Post-main Herdr presentation")
    reclaimed = parsed("0443ecdf-b4fd-526e-a0eb-8becf1cee794")
    assert (reclaimed.actor, reclaimed.assignee) == ("disp/claude", "dev/migration")
    created = parsed("0000c829-17a3-517b-8236-d0d418f72e0f")
    assert (created.kind, created.bead, created.at) == (
        "created",
        "bh-ajt7i",
        "2026-08-26T06:50:50Z",
    )
    assert not parsed("003905ac-a66e-52b8-836e-6319e7bb045c").changes  # updated, empty payload
    notes = parsed("006734f0-68d1-5733-8e43-02755c148995")
    assert set(notes.changes) == {"notes"}


def test_every_production_event_type_is_covered_by_a_fixture() -> None:
    kinds = {parsed(row_id).kind for row_id in BY_ID}
    assert kinds == {
        "created",
        "updated",
        "status_changed",
        "claimed",
        "closed",
        "reopened",
        "label_added",
        "label_removed",
        "dependency_added",
        "dependency_removed",
        "commented",
        "lease_reclaimed",
    }


# -- defensive: malformed rows and unknown types are counted, never fatal ------------------


def mutate(row_id: str, **changes: Any) -> dict[str, Any]:
    return {**BY_ID[row_id], **changes}


def test_malformed_rows_are_skipped_and_counted_by_reason() -> None:
    good = BY_ID["0000c829-17a3-517b-8236-d0d418f72e0f"]
    rows: list[Any] = [
        good,
        mutate("0000c829-17a3-517b-8236-d0d418f72e0f", id="x1", created_at="not a date"),
        mutate("0000c829-17a3-517b-8236-d0d418f72e0f", id="x2", created_at=None),
        mutate("0000c829-17a3-517b-8236-d0d418f72e0f", id="x3", issue_id=""),
        mutate("0000c829-17a3-517b-8236-d0d418f72e0f", id=None),
        mutate("0000c829-17a3-517b-8236-d0d418f72e0f", id="x4", event_type=None),
        mutate("005a9b8e-237e-5c7d-9f7a-dde69c67f2f9", id="x5", new_value='{"assignee": '),
        mutate("005a9b8e-237e-5c7d-9f7a-dde69c67f2f9", id="x6", new_value='{"status":"open"}'),
        mutate("005a9b8e-237e-5c7d-9f7a-dde69c67f2f9", id="x7", old_value='{"status": '),
        mutate("00362b74-8998-5240-9080-a47c046fc8a1", id="x8", new_value="Added dependency: ???"),
        mutate("032cf831-2c55-5625-adc0-5e96e4cd5d41", id="x9", new_value=""),
        mutate("00030648-95fd-5cdc-8c4d-1275790dec20", id="x10", comment="  ", new_value=None),
        mutate("0039ee04-841a-5059-a31d-0470e98026b1", id="x11", comment=None),
        "not a row",
    ]
    read = parse_history(rows)
    assert [event.row_id for event in read.events] == [good["id"]]
    assert read.rows == len(rows)
    assert dict(read.skipped) == {
        "bad_timestamp": 2,
        "missing_issue_id": 1,
        "missing_row_id": 1,
        "missing_event_type": 1,
        "bad_json_new_value": 1,
        "claim_payload": 1,
        "bad_json_old_value": 1,
        "dependency_text": 2,
        "label_text": 1,
        "comment_text": 1,
        "not_an_object": 1,
    }
    assert any("skipped 2 row(s): bad_timestamp" in line for line in read.warnings)


def test_unknown_event_types_are_counted_not_fatal() -> None:
    base = BY_ID["0000c829-17a3-517b-8236-d0d418f72e0f"]
    rows = [
        base,
        {**base, "id": "u1", "event_type": "promoted"},
        {**base, "id": "u2", "event_type": "promoted"},
        {**base, "id": "u3", "event_type": "archived"},
    ]
    read = parse_history(rows)
    assert len(read.events) == 1
    assert dict(read.unknown_types) == {"promoted": 2, "archived": 1}
    assert "unknown event type 'promoted': 2 row(s)" in read.warnings
    assert not read.skipped


def test_query_level_failures_are_fatal_but_typed() -> None:
    with pytest.raises(BdHistoryError):
        parse_history({"not": "a list"})
    with pytest.raises(BdHistoryError):
        decode_rows("no json here\n")
    with pytest.raises(BdHistoryError):
        decode_rows("[\n  {broken")


# -- ordering ------------------------------------------------------------------------------


def test_second_resolution_ties_order_by_row_id_deterministically() -> None:
    base = BY_ID["00030648-95fd-5cdc-8c4d-1275790dec20"]
    rows = [
        {**base, "id": "ffffffff-0000-0000-0000-000000000001"},
        {**base, "id": "00000000-0000-0000-0000-000000000009"},
        {
            **base,
            "id": "aaaaaaaa-0000-0000-0000-000000000002",
            "created_at": "2026-09-26T14:50:18Z",
        },
        {**base, "id": "80000000-0000-0000-0000-000000000003"},
    ]
    forward = [event.row_id for event in parse_history(rows).events]
    backward = [event.row_id for event in parse_history(list(reversed(rows))).events]
    assert (
        forward
        == backward
        == [
            "aaaaaaaa-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000009",
            "80000000-0000-0000-0000-000000000003",
            "ffffffff-0000-0000-0000-000000000001",
        ]
    )


def test_offset_timestamps_normalize_to_utc() -> None:
    row = mutate("0000c829-17a3-517b-8236-d0d418f72e0f", created_at="2026-08-26T08:50:50+02:00")
    assert parsed_event(row).at == "2026-08-26T06:50:50Z"


def parsed_event(row: dict[str, Any]) -> HistoryEvent:
    event = parse_event_row(row)
    assert isinstance(event, HistoryEvent)
    return event


# -- the store call: one, bounded, SELECT-only ---------------------------------------------


def test_full_hive_read_is_one_store_call_regardless_of_bead_count() -> None:
    template = BY_ID["00030648-95fd-5cdc-8c4d-1275790dec20"]
    rows = [{**template, "id": f"id-{n:06d}", "issue_id": f"bh-{n % 700}"} for n in range(5000)]
    runner = CountingRunner(rows)
    read = read_history("/hive", runner=runner)
    assert len(read.events) == 5000
    assert len({event.bead for event in read.events}) == 700
    assert len(runner.calls) == 1  # never per bead, never `bd history`
    assert "history" not in runner.calls[0]


def test_the_one_call_is_a_select_over_the_events_table() -> None:
    runner = CountingRunner([])
    assert read_history("/hive", binary="bd2", runner=runner).events == ()
    (argv,) = runner.calls
    assert argv[:5] == ["bd2", "-C", "/hive", "sql", "--json"]
    assert argv == history_argv("/hive", binary="bd2")
    sql = argv[-1]
    assert sql == HISTORY_SQL
    assert sql.upper().startswith("SELECT ")
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", ";"):
        assert word not in sql.upper()
    for column in ("issue_id", "event_type", "actor", "old_value", "new_value", "comment"):
        assert column in sql
    assert sql.endswith("FROM events")


def test_banner_lines_before_the_array_are_tolerated() -> None:
    runner = CountingRunner(REAL_ROWS, banner="Warning: .beads has permissions 0775\n")
    assert len(read_history("/hive", runner=runner).events) == len(REAL_ROWS)


def test_default_runner_wraps_failures_as_bd_history_error(tmp_path: Any) -> None:
    with pytest.raises(BdHistoryError, match="cannot run"):
        read_history(tmp_path, binary=str(tmp_path / "no-such-bd"))


def test_timeout_is_passed_through_and_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float | None] = []

    def expire(argv: Sequence[str], **kwargs: Any) -> Any:
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", expire)
    with pytest.raises(BdHistoryError, match="timed out after 42s"):
        read_history("/hive", timeout=42)
    assert seen == [42]
    with pytest.raises(BdHistoryError):
        read_history("/hive")
    assert seen[-1] == 300.0  # the generous default
