"""The ``bd`` read routes in :mod:`beadhive_bd_cli.reads`, over a FakeBd transport.

Moved out of the root shell's ``beadhive.bd`` with the routes themselves (bh-o3xuf); the shell's
``bd.show`` / ``bd.children`` / ``bd.child_rows`` / ``bd.state`` are forwards to these, over its
own ``bd`` invocation seam.
"""

from __future__ import annotations

import json
import subprocess
from collections import namedtuple
from pathlib import Path

from beadhive_bd_cli import SubprocessBd, reads

_CP = namedtuple("CP", "returncode stdout stderr")


class FakeBd:
    """A ``BdTransport`` whose processes come from ``responder(cmd)``; ``cmd`` is the argv the
    shell's engine would spawn (``bd -C <cwd> …``). ``json`` is built on ``run`` exactly like the
    shell's: ``--json`` appended, None on a failed or unparseable read."""

    def __init__(self, responder):
        self._responder = responder

    def run(self, args, cwd, actor="", capture=False, text_input=None):
        cmd = ["bd", "-C", str(cwd), *(["--actor", actor] if actor else []), *args]
        return self._responder(cmd)

    def json(self, args, cwd, *, strict=False):
        res = self.run([*args, "--json"], cwd, capture=True)
        if res.returncode != 0:
            return None
        try:
            return json.loads(res.stdout or "null")
        except json.JSONDecodeError:
            return None


# ---- children: membership is the parent EDGE, not the id string (bh-89mrf) ----------------


def test_children_drops_a_prefix_match_that_is_not_really_a_child():
    """bd resolves `--parent` by dotted-id PREFIX, so a bead detached from its epic still comes
    back on the strength of its id. Reproduces the live case: bhui-5mhu.3 was detached at the
    planning plane (`parent: None`, absent from the reverse dep tree) yet still blocked
    `bh work finish bhui-5mhu` as an open child, and re-parenting was a no-op against the guard.
    Only rows carrying the edge survive."""
    rows = [
        {"id": "bhui-5mhu.1", "parent": "bhui-5mhu", "status": "closed"},
        {"id": "bhui-5mhu.2", "parent": "bhui-5mhu", "status": "closed"},
        {"id": "bhui-5mhu.3", "parent": None, "status": "open"},  # detached — the false blocker
    ]
    bd = FakeBd(lambda cmd: _CP(0, json.dumps(rows), ""))

    kids = reads.children(bd, "bhui-5mhu", "/hive")

    assert [k["id"] for k in kids] == ["bhui-5mhu.1", "bhui-5mhu.2"]


def test_children_keeps_a_re_parented_bead_and_drops_a_foreign_one():
    """The edge is authoritative in BOTH directions: a bead whose id does not look like a child
    still counts when it carries the edge, and a bead re-parented AWAY does not."""
    rows = [
        {"id": "bh-standalone", "parent": "bh-epic", "status": "open"},  # adopted: no dotted id
        {"id": "bh-epic.9", "parent": "bh-other-epic", "status": "open"},  # re-parented away
    ]
    bd = FakeBd(lambda cmd: _CP(0, json.dumps(rows), ""))

    kids = reads.children(bd, "bh-epic", "/hive")

    assert [k["id"] for k in kids] == ["bh-standalone"]


def test_children_returns_none_on_a_read_failure_not_an_empty_list():
    """The None contract survives the filter: callers must still tell "cannot list children"
    (which refuses to land) from "this epic has no children"."""
    bd = FakeBd(lambda cmd: _CP(1, "", "Error: no such epic"))

    assert reads.children(bd, "bh-epic", "/hive") is None


def test_children_forwards_extra_flags():
    """`--all` (plan verify needs closed siblings to tell a genuine root from a satisfied one)
    is passed through to bd rather than dropped by the wrapper."""
    seen = {}

    def _capture(cmd):
        seen["cmd"] = cmd
        return _CP(0, "[]", "")

    bd = FakeBd(_capture)
    reads.children(bd, "bh-epic", "/hive", ["--all"])

    assert "--all" in seen["cmd"]
    assert "--parent" in seen["cmd"] and "bh-epic" in seen["cmd"]


def test_child_rows_distinguishes_empty_closed_history_and_read_failure():
    """The historical query has three honest outcomes: [] for a real empty history, event rows
    when closed infrastructure exists, and None when the read itself failed."""
    event = {"id": "bh-1.e1", "issue_type": "event", "status": "closed"}
    responses = iter([_CP(0, "[]", ""), _CP(0, json.dumps([event]), ""), _CP(1, "", "boom")])
    seen = []

    def _read(cmd):
        seen.append(cmd)
        return next(responses)

    bd = FakeBd(_read)

    assert reads.child_rows(bd, "bh-1", "/hive", ["--include-infra"], include_closed=True) == []
    assert reads.child_rows(bd, "bh-1", "/hive", ["--include-infra"], include_closed=True) == [
        event
    ]
    assert reads.child_rows(bd, "bh-1", "/hive", ["--include-infra"], include_closed=True) is None
    assert all("--all" in cmd and "--limit" in cmd for cmd in seen)


def test_children_accepts_the_edge_in_either_representation():
    """bd states the parent edge two ways in one row — a top-level `parent`, and a `parent-child`
    entry in `dependencies` (the form `bd dep tree` walks). A real row carries both, but `parent`
    is simply ABSENT from a parentless row rather than null, so a reader trusting only one
    representation decides membership on which field bd happened to emit."""
    rows = [
        {"id": "e.1", "parent": "e"},  # top-level only
        {"id": "e.2", "dependencies": [{"depends_on_id": "e", "type": "parent-child"}]},
        {
            "id": "e.3",
            "parent": "e",
            "dependencies": [{"depends_on_id": "e", "type": "parent-child"}],
        },
        {"id": "e.4", "dependencies": [{"depends_on_id": "e", "type": "blocks"}]},  # NOT a child
        {"id": "e.5"},  # detached: neither representation
    ]
    bd = FakeBd(lambda cmd: _CP(0, json.dumps(rows), ""))

    kids = reads.children(bd, "e", "/hive")

    assert [k["id"] for k in kids] == ["e.1", "e.2", "e.3"]


# ---- show / state ------------------------------------------------------------------------------


def test_show_unwraps_a_one_list_and_reads_a_failure_as_no_bead():
    responses = iter(
        [
            _CP(0, json.dumps([{"id": "bh-1"}]), ""),
            _CP(0, json.dumps({"id": "bh-2"}), ""),
            _CP(0, "[]", ""),
            _CP(1, "", "Error: no issue found"),
        ]
    )
    bd = FakeBd(lambda cmd: next(responses))
    assert reads.show(bd, "bh-1", "/hive") == {"id": "bh-1"}
    assert reads.show(bd, "bh-2", "/hive") == {"id": "bh-2"}
    assert reads.show(bd, "bh-3", "/hive") is None
    assert reads.show(bd, "bh-4", "/hive") is None


def test_show_forwards_strict_to_the_transport():
    seen = {}

    class Strict(FakeBd):
        def json(self, args, cwd, *, strict=False):
            seen["strict"] = strict
            return {"id": args[1]}

    assert reads.show(Strict(None), "bh-1", "/hive", strict=True) == {"id": "bh-1"}
    assert seen == {"strict": True}


def test_state_is_the_trimmed_value_and_empty_when_unset_or_failed():
    responses = iter([_CP(0, "approved\n", ""), _CP(0, "", ""), _CP(1, "", "boom")])
    seen = []

    def _state(cmd):
        seen.append(cmd)
        return next(responses)

    bd = FakeBd(_state)
    assert reads.state(bd, "bh-1", "review", "/hive") == "approved"
    assert reads.state(bd, "bh-1", "review", "/hive") == ""
    assert reads.state(bd, "bh-1", "review", "/hive") == ""
    assert seen[0] == ["bd", "-C", "/hive", "state", "bh-1", "review"]


# ---- ready ---------------------------------------------------------------------------------------


def test_ready_forward_is_the_captured_bd_ready_argv():
    seen = []
    bd = FakeBd(lambda cmd: seen.append(cmd) or _CP(0, "ready table\n", ""))
    result = reads.ready(bd, "/hive", ["--limit", "5"])
    assert result.stdout == "ready table\n"
    assert seen == [["bd", "-C", "/hive", "ready", "--limit", "5"]]


def test_ready_rows_appends_json_once_and_reads_a_failure_as_none():
    seen = []
    responses = iter([_CP(0, json.dumps([{"id": "bh-1"}]), ""), _CP(1, "", "boom")])

    def _ready(cmd):
        seen.append(cmd)
        return next(responses)

    bd = FakeBd(_ready)
    assert reads.ready_rows(bd, "/hive", ["--limit", "0", "--json"]) == [{"id": "bh-1"}]
    assert reads.ready_rows(bd, "/hive", ["--limit", "0"]) is None
    assert seen[0] == ["bd", "-C", "/hive", "ready", "--limit", "0", "--json"]


def test_status_snapshot_uses_exact_bounded_activity_free_argv(monkeypatch):
    seen = []

    def process(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"summary":{"total_issues":0}}', "")

    monkeypatch.setattr("beadhive_bd_cli.transport.subprocess.run", process)
    result = reads.status_snapshot(SubprocessBd(timeout=120), Path("/fixture/hq"), timeout=10)

    assert result.returncode == 0
    assert seen[0][0] == ["bd", "-C", "/fixture/hq", "status", "--json", "--no-activity"]
    assert seen[0][1]["capture_output"] is True
    assert seen[0][1]["timeout"] == 10
