"""Unit tests for `ws escalate`.

Acceptance criteria covered:
  * Writes an hq-native bead with ``origin:escalation`` + ``intake:untriaged`` — EXACTLY ONE.
  * Captures ``--tool`` + free-text title as metadata.
  * Tags the raiser's role derived from ``--as`` / seat prefix (dev/ → developer, etc.).
  * No HQ initialised → fails gracefully with a pointer at ``ws hq init``.
  * ``origin:escalation`` is a valid value in STATE_DIMENSIONS (validated clean; bogus origin
    values are still rejected).
  * The ``role_from_seat`` prefix map covers all declared seat types.

Stubbing strategy: mirrors ``test_report.py`` — monkeypatch ``report.run`` + the registry
seams so no real bd binary is needed.  HQ is simulated by a tmp .beads dir.
"""

from __future__ import annotations

import json
from collections import namedtuple

import pytest

from beadhive import escalate, registry, report, state
from beadhive.state import ORIGIN_ESCALATION, STATE_DIMENSIONS

Completed = namedtuple("Completed", "returncode stdout stderr")

# ---- helpers ----------------------------------------------------------------

_HQ_ENTRY = {
    "provider": registry.HQ_PROVIDER,
    "org": registry.HQ_ORG,
    "repo": registry.HQ_REPO,
    "prefix": registry.HQ_PREFIX,
    "kind": registry.HQ_KIND,
}


def _cfg_with_hq():
    return {"managed_repos": [dict(_HQ_ENTRY)]}


def _cfg_without_hq():
    return {"managed_repos": []}


class _Recorder:
    """Fake ``report.run`` that records every bd invocation and returns canned responses."""

    def __init__(self, new_id="hq-esc-1"):
        self.new_id = new_id
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        rest = cmd[3:]  # drop ["bd", "-C", "<dir>"]
        if rest[:1] == ["--actor"]:
            rest = rest[2:]
        if rest[:1] == ["--json"]:
            rest = rest[1:]
        verb = rest[0] if rest else ""
        if verb == "create":
            return Completed(0, json.dumps({"id": self.new_id}), "")
        return Completed(0, "", "")

    def all_tokens(self) -> list[str]:
        return [tok for cmd in self.calls for tok in cmd]

    def has_verb(self, *verb_tokens) -> bool:
        vt = list(verb_tokens)
        return any(
            any(cmd[i: i + len(vt)] == vt for i in range(len(cmd)))
            for cmd in self.calls
        )

    def set_state_values(self) -> list[str]:
        """All ``<dim>=<value>`` args ever passed to ``bd set-state``."""
        values = []
        for cmd in self.calls:
            for i, tok in enumerate(cmd):
                if tok == "set-state" and i + 2 < len(cmd):
                    values.append(cmd[i + 2])  # cmd[i+1]=bead_id, cmd[i+2]=dim=value
        return values


def _wire(monkeypatch, rec, tmp_path, *, hq_present=True):
    """Point escalate + report at a fake bd and a tmp HQ store."""
    # Patch both module-level `run` references (report.run and escalate.run).
    monkeypatch.setattr(report.bd, "_run", rec)
    monkeypatch.setattr(escalate, "run", rec)
    # Intake validates only the NEW bead's labels; default them clean.
    monkeypatch.setattr(report.validate, "bead_violations", lambda *a, **kw: [])

    hq_dir = tmp_path / "hq"
    if hq_present:
        (hq_dir / ".beads").mkdir(parents=True)

    monkeypatch.setattr(report.registry, "hive_dir", lambda e: hq_dir)
    monkeypatch.setattr(report.registry, "resolve_hive", lambda cfg, hive: dict(_HQ_ENTRY))

    return hq_dir


# ---- state.py: escalation in STATE_DIMENSIONS ------------------------------


def test_escalation_is_valid_origin_value():
    """``origin:escalation`` must be in the closed STATE_DIMENSIONS vocabulary."""
    assert "escalation" in STATE_DIMENSIONS["origin"]


def test_origin_escalation_constant_matches_dimension():
    """The ``ORIGIN_ESCALATION`` constant must agree with STATE_DIMENSIONS."""
    assert ORIGIN_ESCALATION == "origin:escalation"
    dim, val = ORIGIN_ESCALATION.split(":", 1)
    assert val in STATE_DIMENSIONS[dim]


def test_bogus_origin_value_not_in_dimensions():
    """A bogus origin value must NOT be accepted (closed dimension)."""
    assert "bogus" not in STATE_DIMENSIONS["origin"]


def test_is_escalation_origin_predicate():
    assert state.is_escalation_origin(["origin:escalation"])
    assert not state.is_escalation_origin(["origin:report"])
    assert not state.is_escalation_origin([])


# ---- role_from_seat ---------------------------------------------------------


@pytest.mark.parametrize("seat,expected_role", [
    ("dev/dev1", "developer"),
    ("disp/alpha", "dispatcher"),
    # control-plane split (superintendent → four seats)
    ("super/hq", "supervisor"),
    ("dir/ops", "director"),
    ("cust/keys", "custodian"),
    ("ctrl/gauge", "controller"),
    ("merge/owner", "merger"),
    ("review/bot", "reviewer"),
    # Assurance / roadmap seats
    ("warden/sec", "warden"),
    ("release/cut", "releaser"),
    ("ops/deploy", "operator"),
    ("unknown/x", "unknown/x"),  # unrecognised prefix → pass through
    ("contrib/up", "contrib/up"),  # contributor prefix intentionally unmapped → pass through
    ("", ""),                    # empty → no role label
])
def test_role_from_seat(seat, expected_role):
    assert escalate.role_from_seat(seat) == expected_role


# ---- file_escalation: no HQ registered -------------------------------------


def test_no_hq_fails_gracefully_with_init_pointer():
    """When no kind=hq entry exists, escalation must fail with a clear ``bh hq init`` hint."""
    code, error, new_id = escalate.file_escalation(
        "test problem", cfg=_cfg_without_hq()
    )
    assert code == 1
    assert "bh hq init" in error
    assert new_id == ""


# ---- file_escalation: self-check (exactly one origin:escalation / intake:untriaged bead) ---


def test_escalate_lands_exactly_one_origin_escalation_bead(tmp_path, monkeypatch):
    """Self-check: a single escalate call with no rig creates EXACTLY ONE bead stamped
    ``origin:escalation`` + ``intake:untriaged`` in HQ."""
    rec = _Recorder(new_id="hq-esc-42")
    _wire(monkeypatch, rec, tmp_path, hq_present=True)

    code, error, new_id = escalate.file_escalation(
        "bd create is broken", cfg=_cfg_with_hq()
    )

    assert (code, error, new_id) == (0, "", "hq-esc-42")

    # Exactly one create call.
    create_calls = [cmd for cmd in rec.calls if "create" in cmd]
    assert len(create_calls) == 1

    # origin=escalation must be stamped (not origin=report).
    sv = rec.set_state_values()
    assert "origin=escalation" in sv
    assert "origin=report" not in sv

    # intake=untriaged must be stamped.
    assert "intake=untriaged" in sv


def test_escalate_stamps_tool_label(tmp_path, monkeypatch):
    """``--tool <name>`` is recorded as a ``tool=<name>`` set-state on the bead."""
    rec = _Recorder()
    _wire(monkeypatch, rec, tmp_path, hq_present=True)

    code, error, _ = escalate.file_escalation(
        "ws bd broke", tool="ws bd", cfg=_cfg_with_hq()
    )

    assert code == 0
    assert "tool=ws bd" in rec.set_state_values()


def test_escalate_stamps_role_from_seat(tmp_path, monkeypatch):
    """Seat prefix ``dev/`` is translated to ``role=developer`` and stamped."""
    rec = _Recorder()
    _wire(monkeypatch, rec, tmp_path, hq_present=True)

    code, error, _ = escalate.file_escalation(
        "tool failure", seat="dev/dev-escalate", cfg=_cfg_with_hq()
    )

    assert code == 0
    assert "role=developer" in rec.set_state_values()


def test_escalate_stamps_dispatcher_role(tmp_path, monkeypatch):
    """Seat prefix ``disp/`` maps to ``role=dispatcher``."""
    rec = _Recorder()
    _wire(monkeypatch, rec, tmp_path, hq_present=True)

    code, error, _ = escalate.file_escalation(
        "disp tool issue", seat="disp/lead", cfg=_cfg_with_hq()
    )

    assert code == 0
    assert "role=dispatcher" in rec.set_state_values()


def test_escalate_no_source_system_overload(tmp_path, monkeypatch):
    """The retired ``source_system=report`` overload must NOT appear — born-native only."""
    rec = _Recorder()
    _wire(monkeypatch, rec, tmp_path, hq_present=True)

    escalate.file_escalation("any problem", cfg=_cfg_with_hq())

    # Check only positional/flag tokens (not directory path arguments which may contain
    # arbitrary substrings like "source_system" from the pytest tmp dir name).
    flag_tokens = [
        tok for cmd in rec.calls
        for tok in cmd
        if not tok.startswith("/") and not tok.startswith("~")
    ]
    assert "source_system" not in " ".join(flag_tokens)
    assert not rec.has_verb("import")


def test_escalate_files_as_chore(tmp_path, monkeypatch):
    """Escalations are always filed as ``chore`` (control-plane signal, not bug/feature)."""
    rec = _Recorder()
    _wire(monkeypatch, rec, tmp_path, hq_present=True)

    escalate.file_escalation("some problem", cfg=_cfg_with_hq())

    # Find the --type / -t argument in the create call.
    for cmd in rec.calls:
        if "create" in cmd:
            for i, tok in enumerate(cmd):
                if tok in ("-t", "--type") and i + 1 < len(cmd):
                    assert cmd[i + 1] == "chore"
                    return
    pytest.fail("no --type argument found in create call")


def test_escalate_targets_hq_triplet(tmp_path, monkeypatch):
    """The bead is stamped with the synthetic HQ triplet (local/factory/hq)."""
    rec = _Recorder()
    _wire(monkeypatch, rec, tmp_path, hq_present=True)

    escalate.file_escalation("problem", cfg=_cfg_with_hq())

    all_tokens = " ".join(rec.all_tokens())
    assert "provider:local" in all_tokens
    assert "org:factory" in all_tokens
    assert "repo:hq" in all_tokens


# ---- report.file_report: origin parameter backward compat -------------------


def test_file_report_origin_defaults_to_report(tmp_path, monkeypatch):
    """Existing ``file_report`` callers without the ``origin`` kwarg still get origin=report."""
    rec = _Recorder(new_id="wid-legacy")
    monkeypatch.setattr(report.bd, "_run", rec)
    monkeypatch.setattr(report.validate, "bead_violations", lambda *a, **kw: [])
    hive_dir = tmp_path / "rig"
    (hive_dir / ".beads").mkdir(parents=True)
    monkeypatch.setattr(report.registry, "hive_dir", lambda e: hive_dir)
    monkeypatch.setattr(
        report.registry, "resolve_hive",
        lambda cfg, hive: {"provider": "github", "org": "acme", "repo": "wid", "prefix": "wid"},
    )

    code, _error, new_id = report.file_report(
        "wid", "old call site", "bug", "crew/old", cfg={"managed_repos": []}
    )

    sv = []
    for cmd in rec.calls:
        for i, tok in enumerate(cmd):
            if tok == "set-state" and i + 2 < len(cmd):
                sv.append(cmd[i + 2])

    assert "origin=report" in sv
    assert "origin=escalation" not in sv
