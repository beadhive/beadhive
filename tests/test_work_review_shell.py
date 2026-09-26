"""The compatibility shell's review composition seam (bh-bwnys.1).

Covers only what the shell owns: the ``GateOperations`` / ``StateOperations`` adapters over the
existing named ``bd`` CLI routes, the hive's Beads session selection, and the top-level
``bh work approve`` / ``bounce`` adapter (arguments, actor resolution, rendering, exit codes).
Review policy itself is proven package-locally in ``packages/beadhive-core/tests``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import beadhive_core as core
from beadhive import bd, cli, work, work_review
from beadhive_beads_client import BeadsSession, ExpectedContext, RemoteEndpoint
from test_work import hive

__all__ = ["hive"]

BEAD = "mr-7"
MAIN = Path("/hive/main")


class RecordedBd:
    """Canned ``bd`` processes keyed by subcommand; records every argv."""

    def __init__(self, gates=(), *, fail=None):
        self.gates = list(gates)
        self.fail = dict(fail or {})
        self.calls = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        args = [a for a in cmd[1:] if a]
        while args and args[0] in ("-C", "--actor"):
            args = args[2:]
        verb = " ".join(args[:2])
        if verb in self.fail:
            return subprocess.CompletedProcess(cmd, self.fail[verb], "", f"Error: {verb} failed")
        if verb == "gate list":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.gates), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def recorded(monkeypatch):
    def install(gates=(), **kwargs):
        runner = RecordedBd(gates, **kwargs)
        monkeypatch.setattr(bd, "_run", runner)
        return runner

    return install


def _gate(gate_id, blocks, reason, status="open", await_type="human"):
    return {
        "id": gate_id,
        "status": status,
        "description": f"blocks {blocks}\n\nReason: {reason}",
        "await_type": await_type,
    }


# ---- GateOperations / StateOperations over the CLI compatibility routes ----------------------


def test_gate_lookup_reuses_the_unwindowed_list_and_anchors_the_bead_id(recorded):
    runner = recorded(
        [
            _gate("g1", "bh-epic.1", "bh:review abc1234"),
            _gate("g10", "bh-epic.10", "security: warden scan"),
            _gate("g-parent", "bh-epic", "release-hold: legal"),
            _gate("g0", "bh-epic.1", "bh:review 1111111", status="closed"),
        ]
    )

    gates = work_review.CliGateOperations(MAIN).gates_for("bh-epic.1")

    assert runner.calls == [
        ["bd", "-C", str(MAIN), "gate", "list", "--limit", "0", "--all", "--json"]
    ]
    assert [(g.id, g.status, g.await_type) for g in gates] == [
        ("g1", "open", "human"),
        ("g0", "closed", "human"),
    ]
    assert all(isinstance(g, core.Gate) for g in gates)


def test_gate_lookup_failure_raises_instead_of_reading_as_no_gates(recorded):
    recorded(fail={"gate list": 1})

    with pytest.raises(core.GateLookupFailed):
        work_review.CliGateOperations(MAIN).gates_for(BEAD)


def test_gate_resolve_is_attributed_and_carries_the_bd_exit_code(recorded):
    runner = recorded()
    gates = work_review.CliGateOperations(MAIN)

    gates.resolve("g1", reason="approved by rev/bob", actor="rev/bob")
    assert runner.calls[-1] == [
        "bd", "-C", str(MAIN), "--actor", "rev/bob",
        "gate", "resolve", "g1", "--reason", "approved by rev/bob",
    ]  # fmt: skip

    recorded(fail={"gate resolve": 3})
    with pytest.raises(core.GateResolveFailed) as failure:
        gates.resolve("g1", reason="x", actor="rev/bob")
    assert failure.value.exit_code == 3


def test_state_route_is_bd_set_state_attributed_to_the_actor(recorded):
    runner = recorded()
    states = work_review.CliStateOperations(MAIN)

    states.set_state(BEAD, "review", "changes-requested", reason="changes requested", actor="r/b")
    assert runner.calls[-1] == [
        "bd", "-C", str(MAIN), "--actor", "r/b",
        "set-state", BEAD, "review=changes-requested", "--reason", "changes requested",
    ]  # fmt: skip

    recorded(fail={f"set-state {BEAD}": 2})
    with pytest.raises(core.StateUpdateFailed) as failure:
        states.set_state(BEAD, "review", "approved", reason="x", actor="r/b")
    assert failure.value.exit_code == 2


# ---- session selection -----------------------------------------------------------------------


def test_hive_session_resolves_the_one_supervised_service(monkeypatch):
    calls = []
    monkeypatch.setattr(
        work_review.host_beads,
        "resolve_session",
        lambda main, caps, *, entry: calls.append((main, caps, entry)) or "session",
    )

    assert work_review.hive_session(MAIN, {"repo": "r"}) == "session"
    assert calls == [(MAIN, core.REVIEW_CAPABILITIES, {"repo": "r"})]


def test_an_unservable_hive_becomes_a_fail_closed_session_refusal(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise work_review.host_beads.HiveNotServable("myrepo uses embedded Dolt")

    monkeypatch.setattr(work_review.host_beads, "resolve_session", refuse)

    with pytest.raises(core.SessionUnavailable, match="embedded Dolt"):
        work_review.hive_session(MAIN, {})


def test_approve_without_a_running_service_names_the_start_command(hive, recorded):
    """Nothing is spawned: with no supervised service the command refuses before any gate op."""
    (hive.main / ".beads").mkdir(exist_ok=True)
    (hive.main / ".beads" / "metadata.json").write_text(
        json.dumps({"dolt_mode": "server", "dolt_database": "mr", "project_id": "p"})
    )
    runner = recorded()

    result = CliRunner().invoke(
        cli.app, ["work", "approve", BEAD, "--as", "rev/bob", "--hive", "myrepo"]
    )

    assert result.exit_code == 1
    assert "✗ Beads service unavailable:" in result.stderr
    assert "bh host beads start --hive" in result.stderr
    assert runner.calls == []


# ---- the top-level adapter -------------------------------------------------------------------


def _issue(labels, assignee="dev/alice"):
    return {
        "id": BEAD,
        "title": "t",
        "priority": 2,
        "created_at": "2026-09-26T00:00:00Z",
        "updated_at": "2026-09-26T00:00:00Z",
        "revision": "rev-1",
        "status": "in_progress",
        "assignee": assignee,
        "labels": labels,
    }


@pytest.fixture
def shell(hive, monkeypatch, recorded):
    """Wire the real adapter to a transport-fixture Beads session and a recorded bd."""
    writes = []

    def handle(request):
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v0/beads/context":
            context = {
                "api_version": "v0",
                "bd_version": "1.3.0",
                "schema_version": 1,
                "backend": "dolt",
                "dolt_mode": "server",
                "database": "db",
                "project_id": "p",
                "capabilities": sorted(core.REVIEW_CAPABILITIES),
            }
            return httpx.Response(200, json=context)
        if path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": [], "has_more": False})
        if request.method == "GET":
            return httpx.Response(200, json=_issue(["review:pending"]))
        body = json.loads(request.content)
        writes.append((request.method, body))
        if request.method == "PATCH":
            return httpx.Response(
                200, json={"issue": _issue([]), "changed": True, "revision": "rev-2"}
            )
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "issue_id": BEAD,
                "author": body["author"],
                "text": body["text"],
                "created_at": "2026-09-26T00:00:00Z",
            },
        )

    def session(main, entry):
        assert Path(main) == hive.main and entry
        return BeadsSession(
            RemoteEndpoint("http://127.0.0.1:9"),
            ExpectedContext("p", "db", required_capabilities=core.REVIEW_CAPABILITIES),
            transport=httpx.MockTransport(handle),
        )

    # Everything else is the real shell: config file, hive lookup and actor precedence.
    monkeypatch.setattr(work_review, "session_factory", session)
    gates = [_gate("g1", BEAD, "bh:review abc1234")]
    return writes, recorded(gates)


def test_bh_work_approve_keeps_its_command_contract(shell):
    writes, runner = shell

    result = CliRunner().invoke(cli.app, ["work", "approve", BEAD, "--hive", "myrepo"])

    assert result.exit_code == 0, result.output
    assert f"✓ approved {BEAD}: resolved review gate(s) g1 as dev/default" in result.stdout
    assert ["--actor", "dev/default", "gate", "resolve", "g1"] == runner.calls[-1][3:8]
    assert writes == [
        (
            "PATCH",
            {
                "actor": "dev/default",
                "patch": {"remove_labels": ["review:pending"]},
                "expected_version": "rev-1",
                "force_close_policy": False,
                "force_assignee_transfer": False,
            },
        )
    ]


def test_bh_work_bounce_keeps_its_command_contract(shell):
    writes, runner = shell

    result = CliRunner().invoke(
        cli.app, ["work", "bounce", BEAD, "-m", "fix it", "--as", "rev/bob", "--hive", "myrepo"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip().endswith(
        f"✓ bounced {BEAD} (review=changes-requested) as rev/bob — developer picks it up "
        f"with `bh work resume {BEAD}`"
    )
    feedback = "changes requested by rev/bob: fix it"
    assert runner.calls[-2][-3:] == ["g1", "--reason", feedback]
    assert runner.calls[-1][5:] == [
        "set-state",
        BEAD,
        "review=changes-requested",
        "--reason",
        feedback,
    ]
    assert writes == [("POST", {"author": "rev/bob", "text": feedback})]


def test_refusals_render_to_stderr_with_the_policy_exit_code(shell):
    _writes, runner = shell
    runner.fail["gate resolve"] = 5

    result = CliRunner().invoke(
        cli.app, ["work", "approve", BEAD, "--as", "rev/bob", "--hive", "myrepo"]
    )

    assert result.exit_code == 5
    assert f"✗ failed to resolve review gate g1 for {BEAD}" in result.stderr

    runner.gates = []
    with pytest.raises(work.typer.Exit) as exit_:
        work.approve(bead=BEAD, as_="rev/bob", hive="myrepo")
    assert exit_.value.exit_code == 1
