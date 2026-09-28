"""The ``bd`` routes behind beadhive-core's lifecycle, review and planning ports, over a FakeBd.

Moved out of the root shell with the routes themselves (bh-o3xuf): the argv each port shapes, the
actor attribution, and the failure mapping onto the core's typed errors. The same routes run
against a real ``bd`` in ``test_ports_real_bd.py``; the policy that composes them is proven in
``packages/beadhive-core/tests``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import beadhive_core as core
from beadhive_bd_cli import (
    CliGateOperations,
    CliIssues,
    CliLeases,
    CliMoleculeFiler,
    CliPlanningGates,
    CliStateOperations,
    CliStateReads,
    names_bead,
)

BEAD = "mr-7"
MAIN = Path("/hive/main")


class FakeBd:
    """A recording ``BdTransport`` with canned processes keyed by the first two argv words.

    Records every call in the shape the shell's engine spawns (``bd -C <cwd> [--actor A] …``);
    ``json`` is built on ``run`` exactly like the shell's (``--json`` appended, None on failure).
    """

    def __init__(self, *, fail=None, show=None, gates=(), stdout=None):
        self.fail = dict(fail or {})
        self.show = show
        self.gates = list(gates)
        self.stdout = dict(stdout or {})
        self.calls = []

    def run(self, args, cwd, actor="", capture=False, text_input=None):
        cmd = ["bd", "-C", str(cwd), *(["--actor", actor] if actor else []), *args]
        self.calls.append(cmd)
        verb = " ".join(args[:2])
        if verb in self.fail:
            return subprocess.CompletedProcess(cmd, self.fail[verb], "", f"Error: {verb} failed")
        if args[:1] == ["show"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.show or {}), "")
        if args[:1] == ["state"]:
            return subprocess.CompletedProcess(cmd, 0, "changes-requested\n", "")
        if verb == "gate list":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.gates), "")
        return subprocess.CompletedProcess(cmd, 0, self.stdout.get(verb, ""), "")

    def json(self, args, cwd, *, strict=False):
        res = self.run([*args, "--json"], cwd, capture=True)
        if res.returncode != 0:
            return None
        try:
            return json.loads(res.stdout or "null")
        except json.JSONDecodeError:
            return None


def _argv(call):
    """The bd argv after the global ``-C <dir>`` and ``--actor <name>`` flags."""
    args = call[1:]
    actor = ""
    while args and args[0] in ("-C", "--actor"):
        actor = args[1] if args[0] == "--actor" else actor
        args = args[2:]
    return actor, args


# ---- lifecycle: CliIssues / CliLeases / CliStateReads ------------------------------------------


def test_lease_routes_keep_the_claim_and_release_argv_attributed_to_the_actor():
    bd = FakeBd()
    leases = CliLeases(bd, MAIN)
    leases.acquire("mr-1", actor="dev/a")
    leases.release("mr-1", actor="dev/a")
    assert [_argv(c) for c in bd.calls] == [
        ("dev/a", ["update", "mr-1", "--claim"]),
        ("dev/a", ["update", "mr-1", "--status", "open", "--assignee", ""]),
    ]


def test_lease_and_assign_failures_carry_the_exit_code_without_repeating_bd():
    bd = FakeBd(fail={"update mr-1": 4, "assign mr-1": 6})
    with pytest.raises(core.WriteFailed) as acquire:
        CliLeases(bd, MAIN).acquire("mr-1", actor="dev/a")
    with pytest.raises(core.WriteFailed) as assign:
        CliIssues(bd, MAIN).assign("mr-1", "dev/b", actor="disp/x", read={})
    with pytest.raises(core.WriteFailed) as release:
        CliLeases(bd, MAIN).release("mr-1", actor="dev/a")
    assert (acquire.value.exit_code, acquire.value.detail) == (4, "")
    assert (assign.value.exit_code, assign.value.detail) == (6, "")
    assert release.value.exit_code == 4


def test_issue_and_state_compatibility_routes_read_through_bd():
    bd = FakeBd(show={"id": "mr-1", "status": "open"})
    assert CliIssues(bd, MAIN).get("mr-1") == {"id": "mr-1", "status": "open"}
    assert CliStateReads(bd, MAIN).get_state("mr-1", "review") == "changes-requested"
    assert [_argv(c)[1][:2] for c in bd.calls] == [["show", "mr-1"], ["state", "mr-1"]]
    assert CliIssues.route == "cli-compatibility"


def test_issue_read_failure_reads_as_no_bead_and_unset_state_as_empty():
    bd = FakeBd(fail={"show mr-1": 1, "state mr-1": 1})
    assert CliIssues(bd, MAIN).get("mr-1") is None
    assert CliStateReads(bd, MAIN).get_state("mr-1", "review") == ""


def test_assign_is_attributed_to_the_orchestrator():
    bd = FakeBd()
    CliIssues(bd, MAIN).assign("mr-1", "dev/b", actor="disp/x", read={})
    assert _argv(bd.calls[-1]) == ("disp/x", ["assign", "mr-1", "dev/b"])


# ---- review: CliGateOperations / CliStateOperations ---------------------------------------------


def _gate(gate_id, blocks, reason, status="open", await_type="human"):
    return {
        "id": gate_id,
        "status": status,
        "description": f"blocks {blocks}\n\nReason: {reason}",
        "await_type": await_type,
    }


def test_gate_lookup_reuses_the_unwindowed_list_and_anchors_the_bead_id():
    bd = FakeBd(
        gates=[
            _gate("g1", "bh-epic.1", "bh:review abc1234"),
            _gate("g10", "bh-epic.10", "security: warden scan"),
            _gate("g-parent", "bh-epic", "release-hold: legal"),
            _gate("g0", "bh-epic.1", "bh:review 1111111", status="closed"),
        ]
    )

    gates = CliGateOperations(bd, MAIN).gates_for("bh-epic.1")

    assert bd.calls == [["bd", "-C", str(MAIN), "gate", "list", "--limit", "0", "--all", "--json"]]
    assert [(g.id, g.status, g.await_type) for g in gates] == [
        ("g1", "open", "human"),
        ("g0", "closed", "human"),
    ]
    assert all(isinstance(g, core.Gate) for g in gates)


def test_gate_lookup_failure_raises_instead_of_reading_as_no_gates():
    with pytest.raises(core.GateLookupFailed):
        CliGateOperations(FakeBd(fail={"gate list": 1}), MAIN).gates_for(BEAD)


def test_gate_resolve_is_attributed_and_carries_the_bd_exit_code():
    bd = FakeBd()
    CliGateOperations(bd, MAIN).resolve("g1", reason="approved by rev/bob", actor="rev/bob")
    assert bd.calls[-1] == [
        "bd", "-C", str(MAIN), "--actor", "rev/bob",
        "gate", "resolve", "g1", "--reason", "approved by rev/bob",
    ]  # fmt: skip

    failing = CliGateOperations(FakeBd(fail={"gate resolve": 3}), MAIN)
    with pytest.raises(core.GateResolveFailed) as failure:
        failing.resolve("g1", reason="x", actor="rev/bob")
    assert failure.value.exit_code == 3


def test_state_route_is_bd_set_state_attributed_to_the_actor():
    bd = FakeBd()
    CliStateOperations(bd, MAIN).set_state(
        BEAD, "review", "changes-requested", reason="changes requested", actor="r/b"
    )
    assert bd.calls[-1] == [
        "bd", "-C", str(MAIN), "--actor", "r/b",
        "set-state", BEAD, "review=changes-requested", "--reason", "changes requested",
    ]  # fmt: skip

    failing = CliStateOperations(FakeBd(fail={f"set-state {BEAD}": 2}), MAIN)
    with pytest.raises(core.StateUpdateFailed) as failure:
        failing.set_state(BEAD, "review", "approved", reason="x", actor="r/b")
    assert failure.value.exit_code == 2


def test_names_bead_matches_a_whole_id_never_a_longer_siblings_prefix():
    assert names_bead("blocks bh-epic.1\n\nReason: x", "bh-epic.1")
    assert not names_bead("blocks bh-epic.10", "bh-epic.1")
    assert not names_bead("blocks bh-epic.1a", "bh-epic.1")
    assert names_bead("BLOCKS BH-EPIC.1", "bh-epic.1")
    assert not names_bead(None, "bh-epic.1")


# ---- planning: CliMoleculeFiler / CliPlanningGates ----------------------------------------------


def test_cli_molecule_filer_raises_molecule_filing_failed_on_bd_create_failure():
    """Molecule filing's CLI-compatibility route reports a failed ``bd create`` as a raised
    error, never a silently-empty id."""
    filer = CliMoleculeFiler(FakeBd(fail={"create title": 1}), Path("."))
    with pytest.raises(core.MoleculeFilingFailed):
        filer._create(core.planning.ApplyCreateItem(title="title"), actor="")


def test_cli_molecule_filer_walks_the_compiled_items_one_bd_call_at_a_time():
    compiled = core.compile_molecule(
        {
            "epic": {"title": "Epic"},
            "issues": [
                {"handle": "a", "title": "A", "acceptance": "works", "priority": 0},
                {"handle": "b", "title": "B", "acceptance": "works", "deps": ["a"]},
            ],
        },
        dimension_fields=(),
    )
    ids = iter(["mr-e", "mr-e.1", "mr-e.2"])

    class Creating(FakeBd):
        def run(self, args, cwd, actor="", capture=False, text_input=None):
            result = super().run(args, cwd, actor, capture, text_input)
            if args[:1] == ["create"]:
                return subprocess.CompletedProcess(result.args, 0, next(ids) + "\n", "")
            return result

    bd = Creating()
    outcome = CliMoleculeFiler(bd, MAIN).apply(compiled, actor="planner")

    creates = [_argv(c) for c in bd.calls if _argv(c)[1][:1] == ["create"]]
    deps = [_argv(c)[1] for c in bd.calls if _argv(c)[1][:2] == ["dep", "add"]]
    assert [args[1] for _actor, args in creates] == ["Epic", "A", "B"]
    assert all(actor == "planner" and args[-1] == "--silent" for actor, args in creates)
    assert "-p" not in creates[0][1]  # the epic's priority is unset, not a falsy 0
    a_args = creates[1][1]
    assert a_args[a_args.index("-p") : a_args.index("-p") + 2] == ["-p", "0"]  # P0 is SET
    assert deps and all(d[2] in {"mr-e", "mr-e.1", "mr-e.2"} for d in deps)
    assert set(outcome.ids.values()) == {"mr-e", "mr-e.1", "mr-e.2"}


def test_kickoff_gate_write_contract_is_the_exact_bd_call():
    """``CliPlanningGates.create_kickoff_gate`` — `bh plan file` and `bh plan repair` share ONE
    implementation of the kickoff-gate contract."""
    bd = FakeBd()
    CliPlanningGates(bd, Path("/hive")).create_kickoff_gate("bh-epic.1", "bh-epic", actor="planner")
    assert [_argv(c) for c in bd.calls] == [
        (
            "planner",
            [
                "gate",
                "create",
                "--type=human",
                "--blocks",
                "bh-epic.1",
                "--reason",
                "kickoff bh-epic",
            ],
        )
    ]


def test_planning_gate_conventions_are_attributed_bd_calls():
    bd = FakeBd(fail={"swarm create": 1})
    gates = CliPlanningGates(bd, MAIN)
    assert gates.create_swarm("bh-epic", actor="planner") is False
    gates.set_kickoff_pending("bh-epic", actor="planner")
    gates.create_release_hold_gate("bh-epic.2", "bh-epic", actor="planner")
    assert [_argv(c) for c in bd.calls] == [
        ("planner", ["swarm", "create", "bh-epic"]),
        (
            "planner",
            ["set-state", "bh-epic", "kickoff=pending", "--reason", "awaiting kickoff approval"],
        ),
        (
            "planner",
            [
                "gate",
                "create",
                "--type=human",
                "--blocks",
                "bh-epic.2",
                "--reason",
                "release-hold: bh-epic — release:breaking held for release",
            ],
        ),
    ]
