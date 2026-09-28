"""`beadhive_bd_cli.coordination` — the wrappers over `bd gate`/`bd merge-slot`/`bd heartbeat`/
`bd reclaim` (bh-c6dk.3; moved out of the root shell with the package by bh-o3xuf). Command
construction and JSON-parsing are unit-tested here against a recording FakeBd transport, pinned
to the exact JSON shapes verified against a real `bd` binary (see the module docstring's shape
notes). The genuinely load-bearing PROPERTIES — merge-slot exclusivity under real concurrency,
and a stale lease reclaimed while a heartbeated one survives — need a real `bd` process and are
proven in ``test_coordination_int.py`` instead; mocking those away would be the vacuous-guard
failure mode this bead explicitly calls out.
"""

from __future__ import annotations

import json
from collections import namedtuple

from beadhive_bd_cli import coordination as coord

Completed = namedtuple("Completed", "returncode stdout stderr")


class FakeBd:
    """A recording ``BdTransport``: every call's argv is recorded in the shape the shell's engine
    spawns (``bd -C <cwd> [--actor A] <args>``), and ``returns`` is handed back in call order."""

    def __init__(self, returns):
        self.calls = []
        self._queue = list(returns)

    def run(self, args, cwd, actor="", capture=False, text_input=None):
        self.calls.append(["bd", "-C", str(cwd), *(["--actor", actor] if actor else []), *args])
        return self._queue.pop(0) if self._queue else Completed(0, "{}", "")

    def json(self, args, cwd, *, strict=False):
        raise AssertionError("the coordination wrappers parse run()'s output themselves")


def _mock_run(returns):
    """A FakeBd programmed to hand back `returns` (consumed in call order), and its call log."""
    bd = FakeBd(returns)
    return bd, bd.calls


# ---- _parse_json_tail: bd's --json output isn't always PURE json ------------------------------


def test_parse_json_tail_handles_prefixed_progress_lines():
    """Verified live: `bd gate check --json` still prints one `✓ <id>: resolved - ...` line per
    gate it closes, THEN the JSON payload — `--json` does not suppress those. A wrapper that
    assumed stdout was pure JSON would silently report every successful gate_check as a parse
    failure the moment any gate actually got resolved."""
    stdout = (
        "✓ zz-4na: resolved - timer expired 1s ago\n"
        "\n"
        "Checked 1 gates: 1 resolved, 0 escalated, 0 errors\n"
        "{\n"
        '  "checked": 1,\n'
        '  "resolved": 1,\n'
        '  "escalated": 0,\n'
        '  "errors": 0\n'
        "}\n"
    )
    bd, _ = _mock_run([Completed(0, stdout, "")])

    got = coord.gate_check(bd, "/hive")

    assert got.ok is True
    assert got.checked == 1
    assert got.resolved == 1


# ---- gate -------------------------------------------------------------------------------------


def test_gate_create_builds_command_and_parses_id():
    bd, calls = _mock_run([Completed(0, json.dumps({"id": "zz-d0h", "status": "open"}), "")])

    got = coord.gate_create(
        bd, "/hive", blocks="zz-obk", gate_type="human", reason="need review", actor="dev/a"
    )

    assert got.ok is True
    assert got.gate_id == "zz-d0h"
    assert calls[0] == [
        "bd",
        "-C",
        "/hive",
        "--actor",
        "dev/a",
        "gate",
        "create",
        "--blocks",
        "zz-obk",
        "--type",
        "human",
        "--reason",
        "need review",
        "--json",
    ]


def test_gate_create_reports_bd_failure_without_a_second_call():
    bd, calls = _mock_run([Completed(1, "", "Error: issue not found")])

    got = coord.gate_create(bd, "/hive", blocks="zz-missing")

    assert got.ok is False
    assert "not found" in got.error
    assert len(calls) == 1  # never retries/re-creates on a parse/exit failure


def test_gate_check_parses_counts():
    bd, _ = _mock_run(
        [
            Completed(
                0,
                json.dumps(
                    {"checked": 3, "resolved": 2, "escalated": 0, "errors": 0, "dry_run": False}
                ),
                "",
            )
        ],
    )

    got = coord.gate_check(bd, "/hive", gate_type="timer")

    assert got.ok is True
    assert (got.checked, got.resolved, got.escalated, got.errors) == (3, 2, 0, 0)


def test_gate_check_human_only_hive_reports_zero_checked():
    """The property: `gate check` evaluates timer/bead/gh gates but LEAVES a human gate open —
    exercised here via the exact JSON bd emits for a hive whose only open gate is `human`
    (verified live: `checked=0`). A wrapper that claimed anything else about human gates would
    fail this."""
    bd, _ = _mock_run(
        [Completed(0, json.dumps({"checked": 0, "resolved": 0, "escalated": 0, "errors": 0}), "")],
    )

    got = coord.gate_check(bd, "/hive")

    assert got.ok is True
    assert got.checked == 0
    assert got.resolved == 0


def test_gate_resolve_success():
    bd, calls = _mock_run([Completed(0, "", "")])

    got = coord.gate_resolve(bd, "/hive", "zz-d0h", reason="approved")

    assert got.ok is True
    assert calls[0][-6:] == ["gate", "resolve", "zz-d0h", "--reason", "approved", "--json"]


def test_gate_resolve_already_resolved_is_reported_ok_not_as_a_failure():
    """Verified against a real bd binary: resolving an ALREADY-closed gate still exits 0 — bd
    treats a redundant resolve as a success, not an error. The wrapper must not invent a
    failure bd itself doesn't report (a resolve-only script must be able to run twice safely)."""
    bd, _ = _mock_run([Completed(0, "✓ Gate resolved: zz-d0h\n  Reason: second resolve", "")])

    got = coord.gate_resolve(bd, "/hive", "zz-d0h", reason="second resolve")

    assert got.ok is True


def test_gate_resolve_failure_reports_error():
    bd, _ = _mock_run([Completed(1, "", "Error: gate zz-nope not found")])

    got = coord.gate_resolve(bd, "/hive", "zz-nope")

    assert got.ok is False
    assert "not found" in got.error


# ---- merge-slot ---------------------------------------------------------------------------------


def test_merge_slot_create_is_ok_on_success():
    bd, _ = _mock_run([Completed(0, json.dumps({"id": "zz-merge-slot", "status": "open"}), "")])
    assert coord.merge_slot_create(bd, "/hive") is True


def test_merge_slot_check_open_slot():
    bd, _ = _mock_run(
        [
            Completed(
                0, json.dumps({"available": True, "holder": None, "waiters": None, "id": "x"}), ""
            )
        ],
    )

    got = coord.merge_slot_check(bd, "/hive")

    assert got.ok is True
    assert got.held is False
    assert got.holder == ""
    assert got.waiters == ()


def test_merge_slot_check_held_with_waiters():
    bd, _ = _mock_run(
        [
            Completed(
                0,
                json.dumps(
                    {"available": False, "holder": "agentA", "waiters": ["agentB"], "id": "x"}
                ),
                "",
            )
        ],
    )

    got = coord.merge_slot_check(bd, "/hive")

    assert got.held is True
    assert got.holder == "agentA"
    assert got.waiters == ("agentB",)


def test_merge_slot_check_not_found_reports_exists_false_not_held():
    """A slot that was never `create`d exits non-zero with a plain "not found" message (no
    JSON). Must never be read as `held=True` — that would wedge a caller trying to decide
    whether to acquire."""
    bd, _ = _mock_run([Completed(1, "", "no merge slot found for this rig")])

    got = coord.merge_slot_check(bd, "/hive")

    assert got.ok is False
    assert got.exists is False
    assert got.held is False


def test_merge_slot_acquire_won():
    bd, _ = _mock_run(
        [Completed(0, json.dumps({"acquired": True, "holder": "agentA", "id": "x"}), "")],
    )

    got = coord.merge_slot_acquire(bd, "/hive", "agentA")

    assert got.acquired is True
    assert got.holder == "agentA"
    assert got.waiting is False


def test_merge_slot_acquire_refused_outright_without_wait():
    bd, _ = _mock_run(
        [Completed(1, json.dumps({"acquired": False, "holder": "agentA", "id": "x"}), "")],
    )

    got = coord.merge_slot_acquire(bd, "/hive", "agentB", wait=False)

    assert got.acquired is False
    assert got.waiting is False
    assert got.holder == "agentA"


def test_merge_slot_acquire_queues_as_waiter_with_wait():
    bd, _ = _mock_run(
        [
            Completed(
                1,
                json.dumps(
                    {
                        "acquired": False,
                        "holder": "agentA",
                        "id": "x",
                        "waiting": True,
                        "position": 1,
                    }
                ),
                "",
            )
        ],
    )

    got = coord.merge_slot_acquire(bd, "/hive", "agentB", wait=True)

    assert got.acquired is False
    assert got.waiting is True
    assert got.position == 1


def test_merge_slot_release_wrong_holder_fails():
    """Failure mode: releasing with a `--holder` bd doesn't recognize as the current one is
    REFUSED — verified live (`{"error": "slot held by a, not wrong"}`, exit 1). This is bd's own
    guard against a stale/dead-holder cleanup racing and stealing a live merge's slot."""
    bd, _ = _mock_run([Completed(1, json.dumps({"error": "slot held by a, not wrong"}), "")])

    got = coord.merge_slot_release(bd, "/hive", holder="wrong")

    assert got.ok is False
    assert "slot held by a" in got.error


def test_merge_slot_release_success():
    bd, _ = _mock_run([Completed(0, json.dumps({"id": "x", "released": True}), "")])

    got = coord.merge_slot_release(bd, "/hive", holder="a")

    assert got.ok is True


# ---- heartbeat ------------------------------------------------------------------------------


def test_heartbeat_success():
    bd, _ = _mock_run([Completed(0, json.dumps({"id": "zz-obk", "status": "heartbeat"}), "")])

    got = coord.heartbeat(bd, "/hive", "zz-obk", actor="dev/a")

    assert got.ok is True


def test_heartbeat_refused_when_not_the_current_holder():
    """Failure mode: verified live — heartbeating a bead you don't hold fails loudly rather
    than silently refreshing someone else's lease."""
    bd, _ = _mock_run(
        [Completed(1, json.dumps({"error": "heartbeat zz-obk: issue already claimed by X"}), "")],
    )

    got = coord.heartbeat(bd, "/hive", "zz-obk", actor="dev/imposter")

    assert got.ok is False
    assert "already claimed" in got.error


# ---- reclaim -----------------------------------------------------------------------------------


def test_reclaim_reports_only_what_bd_reclaimed():
    """The wrapper must faithfully narrow to exactly the ids bd's own stale-lease judgment
    named — never all in_progress issues, never none. A wrapper bug that reported every
    in_progress issue as reclaimed (or dropped the list bd gave it) would still pass a test
    that only checks `ok is True`; asserting the id set catches both directions."""
    bd, _ = _mock_run(
        [
            Completed(
                0,
                json.dumps(
                    {
                        "count": 1,
                        "reclaimed": [{"id": "zz-05b", "previous_owner": "dev/dead"}],
                        "scoped": False,
                    }
                ),
                "",
            )
        ],
    )

    got = coord.reclaim(bd, "/hive", older_than="0s")

    assert got.ok is True
    assert got.count == 1
    assert got.reclaimed_ids == ("zz-05b",)
    # the still-live bead must never appear, even though it was never mentioned in this fixture
    assert "zz-zts" not in got.reclaimed_ids


def test_reclaim_builds_filter_flags():
    bd, calls = _mock_run([Completed(0, json.dumps({"count": 0, "reclaimed": None}), "")])

    coord.reclaim(
        bd,
        "/hive",
        older_than="10m",
        label=["lane-a"],
        assignee=["dev/x", "dev/y"],
        ids=["zz-1"],
        any_replica=True,
    )

    cmd = calls[0]
    assert cmd[cmd.index("reclaim") :] == [
        "reclaim",
        "--older-than",
        "10m",
        "--label",
        "lane-a",
        "--assignee",
        "dev/x",
        "--assignee",
        "dev/y",
        "--id",
        "zz-1",
        "--any-replica",
        "--json",
    ]


def test_reclaim_no_stale_leases_reports_empty():
    """The other half of the property, mock-side: nothing stale -> nothing reclaimed. Paired
    with the real-bd version in test_coordination_int.py, which additionally proves a
    heartbeated lease specifically survives (not just "no leases happened to be stale")."""
    bd, _ = _mock_run([Completed(0, json.dumps({"count": 0, "reclaimed": None}), "")])

    got = coord.reclaim(bd, "/hive")

    assert got.ok is True
    assert got.count == 0
    assert got.reclaimed_ids == ()
