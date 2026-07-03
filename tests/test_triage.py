"""Tests for the intake triage surface — the source-agnostic queue, dedup surfacing, and the four
type-aware dispositions (bead).

The seam mirrors `test_report.py`: patch the one `ws.triage.run` symbol with a fake `bd` that
dispatches on the verb (returning canned `show` / `list` / `find-duplicates` JSON, success for
writes) and records every argv, then assert the exact beads-native invocations each disposition
composes — that the queue keys on the shared `intake:untriaged` state regardless of intake channel
(the closed `origin` dimension, resolved via `state.channel_of`), that dedup reuses
`bd find-duplicates`, and that every disposition CLEARS intake via an event-sourced `bd set-state`
transition (never a yanked label).
"""

from __future__ import annotations

import json
from collections import namedtuple

from ws import state, triage

Completed = namedtuple("Completed", "returncode stdout stderr")


class _Bd:
    """Fake `triage.run` dispatching on the bd verb. `show`/`list`/`find-duplicates` return canned
    JSON; every write (`update`/`close`/`set-state`/`assign`) succeeds. Records every argv."""

    def __init__(self, *, bead=None, rows=None, pairs=None):
        self._bead = bead or {
            "id": "wid-1",
            "title": "login is broken",
            "issue_type": "bug",
            "labels": [state.INTAKE_UNTRIAGED, state.ORIGIN_REPORT],
        }
        self._rows = rows if rows is not None else []
        self._pairs = pairs if pairs is not None else []
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        rest = cmd[3:]  # past `bd -C <dir>`
        if rest[:1] == ["--actor"]:
            rest = rest[2:]
        verb = rest[0] if rest else ""
        if verb == "show":
            return Completed(0, json.dumps(self._bead), "")
        if verb == "list":
            return Completed(0, json.dumps(self._rows), "")
        if verb == "find-duplicates":
            return Completed(0, json.dumps({"pairs": self._pairs}), "")
        return Completed(0, "", "")

    def has(self, *tokens) -> bool:
        t = list(tokens)
        return any(
            any(c[i : i + len(t)] == t for i in range(len(c) - len(t) + 1)) for c in self.calls
        )

    def actor_of(self, verb) -> str:
        for c in self.calls:
            if "--actor" in c:
                i = c.index("--actor")
                after = c[i + 2 :]
                if after and after[0] == verb:
                    return c[i + 1]
        return ""


CWD = "/rig"


# ---- source-agnostic queue --------------------------------------------------


def test_list_intake_keys_on_intake_label_source_agnostic(monkeypatch):
    """The queue is one `bd list --label intake:untriaged --status open` — source-agnostic, so a
    report (origin:report label), a github import and a legacy import (native source_system) all
    appear in one queue regardless of their intake channel."""
    rows = [
        {"id": "wid-1", "labels": [state.INTAKE_UNTRIAGED, state.ORIGIN_REPORT]},
        {"id": "wid-2", "source_system": "github", "labels": [state.INTAKE_UNTRIAGED]},
        {"id": "wid-3", "source_system": "import", "labels": [state.INTAKE_UNTRIAGED]},
    ]
    bd = _Bd(rows=rows)
    monkeypatch.setattr(triage, "run", bd)

    got = triage.list_intake(CWD)

    assert [r["id"] for r in got] == ["wid-1", "wid-2", "wid-3"]
    assert bd.has("list", "--label", state.INTAKE_UNTRIAGED, "--status", "open")


def test_list_intake_narrows_to_one_channel(monkeypatch):
    """`--source github` narrows to the resolved `origin` CHANNEL client-side (bd has no such
    filter) — imports derive the channel from `source_system`, reports from the `origin:` label."""
    rows = [
        {"id": "wid-1", "labels": [state.INTAKE_UNTRIAGED, state.ORIGIN_REPORT]},
        {"id": "wid-2", "source_system": "github", "labels": [state.INTAKE_UNTRIAGED]},
    ]
    monkeypatch.setattr(triage, "run", _Bd(rows=rows))

    assert [r["id"] for r in triage.list_intake(CWD, source="github")] == ["wid-2"]
    assert [r["id"] for r in triage.list_intake(CWD, source="report")] == ["wid-1"]


# ---- dedup (reuse bd find-duplicates) ---------------------------------------


def test_find_dupes_reuses_bd_find_duplicates(monkeypatch):
    pairs = [{"issue_a_id": "wid-1", "issue_b_id": "wid-9", "similarity": 0.7}]
    bd = _Bd(pairs=pairs)
    monkeypatch.setattr(triage, "run", bd)

    got = triage.find_dupes(CWD, threshold=0.4)

    assert got == pairs
    assert bd.has("find-duplicates", "--threshold", "0.4", "--method", "mechanical")


def test_dupes_touching_filters_to_involved_beads():
    pairs = [
        {"issue_a_id": "wid-1", "issue_b_id": "wid-9"},
        {"issue_a_id": "wid-5", "issue_b_id": "wid-6"},
    ]
    assert triage.dupes_touching(pairs, ["wid-1"]) == [pairs[0]]


# ---- accept -----------------------------------------------------------------


def test_accept_sets_type_priority_and_clears_intake(monkeypatch):
    """accept is type-aware (sets type+priority) and clears intake via an event-sourced set-state
    to the terminal `accepted` value — the bead stays open as backlog."""
    bd = _Bd()
    monkeypatch.setattr(triage, "run", bd)

    code, error, msg = triage.accept(CWD, "wid-1", "crew/mgr", issue_type="feature", priority="1")

    assert (code, error) == (0, "")
    assert bd.has("update", "wid-1", "--type", "feature", "--priority", "1")
    assert bd.has("set-state", "wid-1", "intake=accepted")
    assert bd.actor_of("set-state") == "crew/mgr"  # provenance rides --actor
    assert not bd.has("close", "wid-1")  # accept keeps the bead open
    assert "accepted" in msg


def test_accept_without_type_or_priority_still_clears_intake(monkeypatch):
    bd = _Bd()
    monkeypatch.setattr(triage, "run", bd)

    code, error, _ = triage.accept(CWD, "wid-1", "crew/mgr")

    assert (code, error) == (0, "")
    assert not bd.has("update", "wid-1")  # nothing to update
    assert bd.has("set-state", "wid-1", "intake=accepted")


# ---- reject -----------------------------------------------------------------


def test_reject_closes_with_reporter_visible_reason(monkeypatch):
    bd = _Bd()
    monkeypatch.setattr(triage, "run", bd)

    code, error, msg = triage.reject(CWD, "wid-1", "crew/mgr", reason="works as intended")

    assert (code, error) == (0, "")
    assert bd.has("set-state", "wid-1", "intake=rejected")  # cleared + audit-recorded
    assert bd.has("close", "wid-1", "--reason", "works as intended")
    assert "works as intended" in msg


def test_reject_requires_a_reason(monkeypatch):
    bd = _Bd()
    monkeypatch.setattr(triage, "run", bd)

    code, error, _ = triage.reject(CWD, "wid-1", "crew/mgr", reason="")

    assert code == 1
    assert "reason" in error
    assert bd.calls == []  # fails before any write


# ---- reroute ----------------------------------------------------------------


def test_reroute_to_rig_refiles_and_closes_original(monkeypatch):
    """reroute --to re-files the report into the right rig (reusing ws report, so provenance +
    intake re-stamp there), then closes the original as rerouted — type-aware (preserves bug)."""
    bd = _Bd()
    monkeypatch.setattr(triage, "run", bd)
    filed = {}

    def fake_file_report(rig, title, rtype, actor, cfg=None):
        filed.update(rig=rig, title=title, rtype=rtype, actor=actor)
        return 0, "", "oth-7"

    from ws import report

    monkeypatch.setattr(report, "file_report", fake_file_report)

    code, error, msg = triage.reroute(CWD, "wid-1", "crew/mgr", to_rig="other")

    assert (code, error) == (0, "")
    assert filed == {
        "rig": "other",
        "title": "login is broken",
        "rtype": "bug",
        "actor": "crew/mgr",
    }
    assert bd.has("set-state", "wid-1", "intake=rerouted")
    assert bd.has("close", "wid-1", "--reason", "rerouted to other as oth-7")
    assert "oth-7" in msg


def test_reroute_to_super_bounces_without_clearing_intake(monkeypatch):
    """reroute --super reassigns to the superintendent seat and LEAVES intake untriaged, so it
    stays in the fleet-wide inbox for them to route."""
    bd = _Bd()
    monkeypatch.setattr(triage, "run", bd)

    code, error, msg = triage.reroute(CWD, "wid-1", "crew/mgr", superintendent="super/anna")

    assert (code, error) == (0, "")
    assert bd.has("assign", "wid-1", "super/anna")
    assert not bd.has("set-state", "wid-1", "intake=rerouted")  # stays untriaged
    assert not bd.has("close", "wid-1")
    assert "super/anna" in msg


def test_reroute_requires_exactly_one_destination(monkeypatch):
    bd = _Bd()
    monkeypatch.setattr(triage, "run", bd)

    code, error, _ = triage.reroute(CWD, "wid-1", "crew/mgr")  # neither --to nor --super

    assert code == 1
    assert "exactly one" in error
    assert bd.calls == []


# ---- promote ----------------------------------------------------------------


def test_promote_hands_to_planner_via_intake_promoted(monkeypatch):
    """promote is a HAND-OFF only: it sets intake=promoted (the planner's adopt queue key, read by
    ) and does NOT adopt / close / create anything."""
    bd = _Bd()
    monkeypatch.setattr(triage, "run", bd)

    code, error, msg = triage.promote(CWD, "wid-1", "crew/mgr")

    assert (code, error) == (0, "")
    assert bd.has("set-state", "wid-1", "intake=promoted")
    assert not bd.has("close", "wid-1")
    assert not bd.has("update", "wid-1")
    assert "jf5k" in msg
    # the promoted state is the queue predicate jf5k reads
    assert state.is_promoted([state.INTAKE_PROMOTED])


# ---- guard: only untriaged intake is disposable -----------------------------


def test_disposition_refuses_a_non_intake_bead(monkeypatch):
    """A bead without intake:untriaged (already triaged, or never intake) can't be disposed — a
    disposition never re-triages."""
    bd = _Bd(bead={"id": "wid-1", "title": "x", "issue_type": "bug", "labels": []})
    monkeypatch.setattr(triage, "run", bd)

    for call in (
        lambda: triage.accept(CWD, "wid-1", "crew/mgr"),
        lambda: triage.reject(CWD, "wid-1", "crew/mgr", reason="r"),
        lambda: triage.promote(CWD, "wid-1", "crew/mgr"),
        lambda: triage.reroute(CWD, "wid-1", "crew/mgr", to_rig="other"),
    ):
        code, error, _ = call()
        assert code == 1
        assert "not an untriaged intake bead" in error
        assert not bd.has("set-state", "wid-1", "intake=accepted")


# ---- state vocabulary -------------------------------------------------------


def test_disposition_state_maps_each_verb_to_a_terminal_intake_value():
    assert state.disposition_state("accept") == "accepted"
    assert state.disposition_state("reject") == "rejected"
    assert state.disposition_state("reroute") == "rerouted"
    assert state.disposition_state("promote") == "promoted"
    assert state.disposition_state("bogus") is None
    # every terminal value is a member of the closed intake dimension (so it validates clean)
    for value in state.DISPOSITION_STATE.values():
        assert value in state.STATE_DIMENSIONS["intake"]
