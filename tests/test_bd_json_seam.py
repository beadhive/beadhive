"""Unit tests for the public bd.json seam —.

Two paths only (per acceptance criteria):
  * JSON return path  — non-zero exit → None; valid JSON stdout → parsed dict/list.
  * None path         — non-zero exit → None; invalid JSON stdout → None.

The seam patches ``ws.bd.run`` so no real ``bd`` binary is needed.
"""

from __future__ import annotations

import json
from collections import namedtuple

import pytest

from beadhive import bd as bd_mod

_CP = namedtuple("CP", "returncode stdout stderr")


def test_bd_json_returns_parsed_dict_on_success(monkeypatch):
    """Happy path: bd exits 0 with valid JSON → bd.json returns the parsed object."""
    payload = {"id": "mr-1", "status": "open"}

    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(0, json.dumps(payload), ""))

    result = bd_mod.json(["show", "mr-1"], "/some/hive")

    assert result == payload


def test_bd_json_returns_parsed_list_on_success(monkeypatch):
    """bd.json handles a list-shaped response (bd list returns an array)."""
    payload = [{"id": "mr-1"}, {"id": "mr-2"}]

    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(0, json.dumps(payload), ""))

    result = bd_mod.json(["list"], "/some/hive")

    assert result == payload


def test_bd_json_appends_json_flag(monkeypatch):
    """bd.json appends --json to the command itself; callers must NOT pass it."""
    recorded = []

    def fake_run(cmd, **_kw):
        recorded.append(list(cmd))
        return _CP(0, "null", "")

    bd_mod.json(["show", "mr-1"], "/hive")  # no monkeypatch yet — just verify flag is appended

    monkeypatch.setattr(bd_mod, "_run", fake_run)
    bd_mod.json(["show", "mr-1"], "/hive")

    assert recorded[0][-1] == "--json"
    assert "show" in recorded[0]
    assert "mr-1" in recorded[0]


def test_bd_json_returns_none_on_nonzero_exit(monkeypatch):
    """None path (non-zero exit): bd.json returns None, never raises."""
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(1, "", "Error: not found"))

    result = bd_mod.json(["show", "missing"], "/hive")

    assert result is None


def test_bd_json_returns_none_on_invalid_json(monkeypatch):
    """None path (bad JSON): bd exits 0 but stdout is not JSON → bd.json returns None."""
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(0, "not valid json }{", ""))

    result = bd_mod.json(["show", "mr-1"], "/hive")

    assert result is None


def test_bd_json_returns_none_on_empty_stdout(monkeypatch):
    """bd exits 0 with empty stdout → None (json.loads('null') returns None, not an error)."""
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(0, "", ""))

    result = bd_mod.json(["show", "mr-1"], "/hive")

    # json.loads("null") == None in Python, so the contract is preserved
    assert result is None


# ---- bd.children: membership is the parent EDGE, not the id string (bh-89mrf) ----------------


def test_children_drops_a_prefix_match_that_is_not_really_a_child(monkeypatch):
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
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(0, json.dumps(rows), ""))

    kids = bd_mod.children("bhui-5mhu", "/hive")

    assert [k["id"] for k in kids] == ["bhui-5mhu.1", "bhui-5mhu.2"]


def test_children_keeps_a_re_parented_bead_and_drops_a_foreign_one(monkeypatch):
    """The edge is authoritative in BOTH directions: a bead whose id does not look like a child
    still counts when it carries the edge, and a bead re-parented AWAY does not."""
    rows = [
        {"id": "bh-standalone", "parent": "bh-epic", "status": "open"},  # adopted: no dotted id
        {"id": "bh-epic.9", "parent": "bh-other-epic", "status": "open"},  # re-parented away
    ]
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(0, json.dumps(rows), ""))

    kids = bd_mod.children("bh-epic", "/hive")

    assert [k["id"] for k in kids] == ["bh-standalone"]


def test_children_returns_none_on_a_read_failure_not_an_empty_list(monkeypatch):
    """The None contract survives the filter: callers must still tell "cannot list children"
    (which refuses to land) from "this epic has no children"."""
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(1, "", "Error: no such epic"))

    assert bd_mod.children("bh-epic", "/hive") is None


def test_children_forwards_extra_flags(monkeypatch):
    """`--all` (plan verify needs closed siblings to tell a genuine root from a satisfied one)
    is passed through to bd rather than dropped by the wrapper."""
    seen = {}

    def _capture(cmd, **_kw):
        seen["cmd"] = cmd
        return _CP(0, "[]", "")

    monkeypatch.setattr(bd_mod, "_run", _capture)
    bd_mod.children("bh-epic", "/hive", ["--all"])

    assert "--all" in seen["cmd"]
    assert "--parent" in seen["cmd"] and "bh-epic" in seen["cmd"]


def test_children_accepts_the_edge_in_either_representation(monkeypatch):
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
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(0, json.dumps(rows), ""))

    kids = bd_mod.children("e", "/hive")

    assert [k["id"] for k in kids] == ["e.1", "e.2", "e.3"]


# ---- bd.err_detail: a reason the operator can act on (bh-f8rdk) -------------------------------


def test_err_detail_extracts_the_message_from_the_multiline_json_bd_emits():
    """The reported failure: bd's SQL error is a multi-line JSON object, so `err_line` returned
    the bare `{` and the operator saw `bulk copy from 'bh' failed (issues: {)` — the truncation
    that made the real cause undiagnosable without re-running by hand."""
    payload = '{\n  "error": "duplicate primary key given",\n  "query": "INSERT INTO `issues`"\n}\n'
    res = _CP(1, payload, "")

    assert bd_mod.err_line(res) == "{"  # the defect, still true of err_line's documented contract
    assert bd_mod.err_detail(res) == "duplicate primary key given"


def test_err_detail_unwraps_a_nested_error_payload():
    """A message nested one level down is still found, and the outer key is kept for context."""
    res = _CP(1, '{"error": {"message": "table `issues` does not exist"}}', "")

    assert bd_mod.err_detail(res) == "table `issues` does not exist"


def test_err_detail_keeps_working_for_the_plain_one_line_error():
    """The common shape is unchanged — this is a widening, not a replacement."""
    assert bd_mod.err_detail(_CP(1, "", "Error: something broke\n")) == "Error: something broke"


def test_err_detail_skips_structural_lines_when_the_json_will_not_parse():
    """Truncated / non-JSON output still yields the first line carrying information rather than
    a bracket — the failure mode being fixed must not reappear via the fallback path."""
    res = _CP(1, "{\n  connection refused talking to the sql-server\n", "")

    assert bd_mod.err_detail(res) == "connection refused talking to the sql-server"


def test_err_detail_falls_back_to_the_exit_code_rather_than_an_empty_reason():
    """A reason is never empty: silence plus a non-zero exit still tells the operator something."""
    assert bd_mod.err_detail(_CP(3, "", "")) == "exit 3"
    assert bd_mod.err_detail(_CP(3, "{}", "")) == "exit 3"


# ---- strict=True: an absent binary is an ERROR, not a null (bh-8x452) ------------------------


def test_strict_raises_binary_missing_instead_of_returning_none(monkeypatch):
    """The None contract is ambiguous in exactly one way that matters: None means "no such bead"
    to most callers, so an absent bd read as a fact about the operator's data. On the CLI the
    narration fixes that. On a STRUCTURED surface it cannot — `bh mcp serve` hands the agent the
    return value and writes the narration to the server's stderr, which the agent never reads, so
    the agent received `null`. Pre-0.11.1 it received a ResourceError naming the cause, making
    this a REGRESSION rather than a pre-existing gap."""
    from beadhive import run as run_mod

    def _no_such_binary(*_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory: 'bd'")

    monkeypatch.setattr(run_mod.subprocess, "run", _no_such_binary)

    with pytest.raises(bd_mod.BinaryMissing) as excinfo:
        bd_mod.json(["show", "bh-1"], "/tmp", strict=True)
    assert "`bd` is not on PATH" in str(excinfo.value)

    with pytest.raises(bd_mod.BinaryMissing):
        bd_mod.show("bh-1", "/tmp", strict=True)

    # Default stays None-on-error — bh doctor's whole job is reporting on a broken seat.
    monkeypatch.setattr(bd_mod, "_MISSING_BINARY_WARNED", set())
    assert bd_mod.json(["show", "bh-1"], "/tmp") is None


def test_strict_does_not_raise_for_an_ordinary_bd_failure(monkeypatch):
    """Only the ABSENT-BINARY case is promoted to an exception. A bd that ran and exited non-zero
    (a genuinely missing bead, a bad query) must still be the ordinary None, or every not-found
    would become an error on the MCP surface."""
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(1, "", "Error: no such issue"))

    assert bd_mod.json(["show", "nope"], "/tmp", strict=True) is None
    assert bd_mod.show("nope", "/tmp", strict=True) is None


# ---- strict_reads(): strictness as a property of the SURFACE (bh-fzh4h) ---------------------


def _absent_bd(monkeypatch):
    """Make every bd invocation look like an uninstalled binary."""
    from beadhive import run as run_mod

    def _no_such_binary(*_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory: 'bd'")

    monkeypatch.setattr(run_mod.subprocess, "run", _no_such_binary)
    monkeypatch.setattr(bd_mod, "_MISSING_BINARY_WARNED", set())


def test_strict_reads_makes_an_INDIRECT_read_strict(monkeypatch):
    """The whole point of the ContextVar over another parameter. bh-8x452 threaded `strict=` into
    the call sites someone remembered; the resources that reach bd through a helper which takes no
    such flag kept returning a plausible empty result (bh-fzh4h). A read made deep inside the
    block — by code that never heard of strictness — must raise."""
    _absent_bd(monkeypatch)

    def _reached_indirectly():
        """Stands in for triage.intake_payload / work_show.show_payload / worktree.status_rows:
        an ordinary caller of the non-strict seam."""
        return bd_mod.json(["list"], "/tmp")

    assert _reached_indirectly() is None  # outside the block: unchanged None-on-error contract

    with pytest.raises(bd_mod.BinaryMissing) as excinfo:
        with bd_mod.strict_reads():
            _reached_indirectly()
    assert "`bd` is not on PATH" in str(excinfo.value)


def test_strict_reads_restores_the_previous_value_on_exit(monkeypatch):
    """Scoped, not a global switch: leaving the block (even by exception) puts the surface back to
    the None contract the CLI depends on, and nesting is safe."""
    _absent_bd(monkeypatch)

    with pytest.raises(bd_mod.BinaryMissing):
        with bd_mod.strict_reads():
            with bd_mod.strict_reads():
                bd_mod.json(["list"], "/tmp")

    assert bd_mod.json(["list"], "/tmp") is None


def test_strict_reads_does_not_promote_an_ordinary_bd_failure(monkeypatch):
    """Same narrowing as `strict=True`: bd ran and exited non-zero is an ANSWER (no such bead),
    not a failed lookup. Only an absent binary raises, or every not-found becomes an error."""
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(1, "", "Error: no such issue"))

    with bd_mod.strict_reads():
        assert bd_mod.json(["show", "nope"], "/tmp") is None
        assert bd_mod.show("nope", "/tmp") is None


def test_strict_reads_leaves_a_successful_read_alone(monkeypatch):
    """Strictness changes only the absent-binary path — a good read returns exactly what it did."""
    payload = [{"id": "mr-1"}]
    monkeypatch.setattr(bd_mod, "_run", lambda cmd, **_kw: _CP(0, json.dumps(payload), ""))

    with bd_mod.strict_reads():
        assert bd_mod.json(["list"], "/tmp") == payload


def test_both_channels_state_the_absence_with_one_message(monkeypatch, capsys):
    """bd reports the same fact two ways — stderr narration for a human at a CLI, BinaryMissing for
    a strict caller whose consumer never sees stderr. bh-fzh4h asked that they converge rather than
    drift, so both are built from `_missing_binary_message`: same claim, same remedy, differing
    only in the voice the narration can use."""
    _absent_bd(monkeypatch)

    assert bd_mod.json(["list"], "/tmp") is None
    narrated = capsys.readouterr().err

    with pytest.raises(bd_mod.BinaryMissing) as excinfo:
        bd_mod.json(["list"], "/tmp", strict=True)
    raised = str(excinfo.value)

    for claim in ("`bd` is not on PATH", "FAILED LOOKUP, not an answer about your data", "PATH"):
        assert claim in narrated
        assert claim in raised
    assert "doctor` names the remedy" in narrated and "doctor` names the remedy" in raised
