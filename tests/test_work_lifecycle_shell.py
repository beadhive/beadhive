"""The compatibility shell's lifecycle composition seam (bh-sy36q.1).

Covers only what the shell owns: the named ``bd`` compatibility routes (claim lease, issue
read/assign, state), the pre-execution selection between the api-ready issue route and its ``bd``
compatibility route, and the top-level ``bh work assign`` / ``claim`` / ``abandon`` / ``resume``
composition over the real worktree capabilities (real git, a faked ``bd``). Lifecycle policy is
proven package-locally in ``packages/beadhive-core/tests/test_core_lifecycle_policy.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import beadhive_core as core
from beadhive import bd, cli, work, work_lifecycle
from beadhive_beads_client import BeadsSession, ExpectedContext, RemoteEndpoint
from test_work import _wt, fakebd, hive

__all__ = ["fakebd", "hive"]

MAIN = Path("/hive/main")


class RecordedBd:
    def __init__(self, *, fail=None, show=None):
        self.fail = dict(fail or {})
        self.show = show
        self.calls = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        args = [a for a in cmd[1:]]
        while args and args[0] in ("-C", "--actor"):
            args = args[2:]
        verb = " ".join(args[:2])
        if verb in self.fail:
            return subprocess.CompletedProcess(cmd, self.fail[verb], "", f"Error: {verb} failed")
        if args[:1] == ["show"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.show or {}), "")
        if args[:1] == ["state"]:
            return subprocess.CompletedProcess(cmd, 0, "changes-requested\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def recorded(monkeypatch):
    def install(**kwargs):
        runner = RecordedBd(**kwargs)
        monkeypatch.setattr(bd, "_run", runner)
        return runner

    return install


def _argv(call):
    """The bd argv after the global ``-C <dir>`` and ``--actor <name>`` flags."""
    args = call[1:]
    actor = ""
    while args and args[0] in ("-C", "--actor"):
        actor = args[1] if args[0] == "--actor" else actor
        args = args[2:]
    return actor, args


# ---- named CLI-compatibility routes --------------------------------------------------------


def test_lease_routes_keep_the_claim_and_release_argv_attributed_to_the_actor(recorded):
    runner = recorded()
    leases = work_lifecycle.CliLeases(MAIN)
    leases.acquire("mr-1", actor="dev/a")
    leases.release("mr-1", actor="dev/a")
    assert [_argv(c) for c in runner.calls] == [
        ("dev/a", ["update", "mr-1", "--claim"]),
        ("dev/a", ["update", "mr-1", "--status", "open", "--assignee", ""]),
    ]


def test_lease_and_assign_failures_carry_the_exit_code_without_repeating_bd(recorded):
    recorded(fail={"update mr-1": 4, "assign mr-1": 6})
    with pytest.raises(core.WriteFailed) as acquire:
        work_lifecycle.CliLeases(MAIN).acquire("mr-1", actor="dev/a")
    with pytest.raises(core.WriteFailed) as assign:
        work_lifecycle.CliIssues(MAIN).assign("mr-1", "dev/b", actor="disp/x", read={})
    assert (acquire.value.exit_code, acquire.value.detail) == (4, "")
    assert (assign.value.exit_code, assign.value.detail) == (6, "")


def test_issue_and_state_compatibility_routes_read_through_bd(recorded):
    runner = recorded(show={"id": "mr-1", "status": "open"})
    assert work_lifecycle.CliIssues(MAIN).get("mr-1") == {"id": "mr-1", "status": "open"}
    assert work_lifecycle.CliStateReads(MAIN).get_state("mr-1", "review") == "changes-requested"
    assert [_argv(c)[1][:2] for c in runner.calls] == [["show", "mr-1"], ["state", "mr-1"]]


# ---- pre-execution route selection ---------------------------------------------------------


def test_an_unservable_hive_selects_the_bd_route_before_any_beads_operation(recorded, monkeypatch):
    runner = recorded(show={"id": "mr-1", "status": "open"})

    def unavailable(main, entry):
        raise core.SessionUnavailable("embedded Dolt cannot be served")

    monkeypatch.setattr(work_lifecycle, "session_factory", unavailable)
    with work_lifecycle.commands({}, "", MAIN, {}) as lifecycle:
        assert lifecycle._issues.route == "cli-compatibility"
        assert lifecycle._issues.get("mr-1")["id"] == "mr-1"
    assert _argv(runner.calls[-1])[1][:2] == ["show", "mr-1"]


def test_no_route_is_selected_for_commands_that_never_read_the_bead(recorded, monkeypatch):
    def forbidden(main, entry):
        raise AssertionError("mark_submitted must not open a Beads session")

    runner = recorded()
    monkeypatch.setattr(work_lifecycle, "session_factory", forbidden)
    work_lifecycle.mark_submitted({}, "", {}, MAIN, "mr-1", "abc1234", "dev/a")
    assert _argv(runner.calls[-1]) == (
        "dev/a",
        ["set-state", "mr-1", "review=pending", "--reason", "submitted abc1234"],
    )


# ---- api-ready route: the CLI contract over a transport-fixture session --------------------


def _details(row):
    return {
        "id": row["id"],
        "title": row.get("title", "t"),
        "priority": 2,
        "created_at": "2026-09-27T00:00:00Z",
        "updated_at": "2026-09-27T00:00:00Z",
        "revision": row.get("revision", "rev-1"),
        "status": row.get("status", "open"),
        "assignee": row.get("assignee") or "",
        "issue_type": row.get("issue_type", "task"),
        "labels": list(row.get("labels") or []),
        **{
            key: row[key]
            for key in ("description", "design", "acceptance_criteria")
            if row.get(key)
        },
    }


@pytest.fixture
def served(hive, fakebd, monkeypatch):
    """Serve the api-ready issue route over HTTP; everything else is the real shell."""
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
                "capabilities": sorted(core.LIFECYCLE_CAPABILITIES),
            }
            return httpx.Response(200, json=context)
        if path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": [], "has_more": False})
        bead = path.rsplit("/", 1)[-1]
        if request.method == "GET":
            row = fakebd.beads.get(bead)
            if row is None:
                body = {"status": 404, "title": "nf", "code": "not_found", "request_id": "r"}
                return httpx.Response(404, json=body)
            return httpx.Response(200, json=_details(row))
        body = json.loads(request.content)
        writes.append((request.method, bead, body))
        fakebd.beads[bead]["assignee"] = body["patch"]["assignee"]
        row = _details(fakebd.beads[bead])
        return httpx.Response(200, json={"issue": row, "changed": True, "revision": "rev-2"})

    def session(main, entry):
        assert Path(main) == hive.main and entry
        return BeadsSession(
            RemoteEndpoint("http://127.0.0.1:9"),
            ExpectedContext("p", "db", required_capabilities=core.LIFECYCLE_CAPABILITIES),
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr(work_lifecycle, "session_factory", session)
    return writes


def _bd_reads(fakebd, bead):
    return [args for _actor, args in fakebd.calls if args[:2] == ["show", bead]]


def test_bh_work_assign_writes_the_assignee_over_http_and_provisions(hive, fakebd, served):
    fakebd.seed("mr-7", title="t")

    result = CliRunner().invoke(
        cli.app,
        ["work", "assign", "mr-7", "--to", "dev/carol", "--as", "disp/lead", "--hive", "myrepo"],
    )

    assert result.exit_code == 0, result.output
    assert f"✓ assigned mr-7 → dev/carol; worktree {_wt(hive, 'mr-7')}" in result.stdout
    assert served == [
        (
            "PATCH",
            "mr-7",
            {
                "actor": "disp/lead",
                "patch": {"assignee": "dev/carol"},
                "expected_version": "rev-1",
                "force_close_policy": False,
                "force_assignee_transfer": False,
            },
        )
    ]
    assert not fakebd.did("assign", "mr-7")
    assert fakebd.beads["mr-7"]["status"] == "open"
    assert _wt(hive, "mr-7").exists()


def test_bh_work_claim_output_is_route_independent(hive, fakebd, served, monkeypatch):
    """Byte-stable: the same claim renders identically over the API and the bd route."""
    fakebd.seed("mr-8", title="t", description="the brief")
    fakebd.seed("mr-9", title="t", description="the brief")
    api = CliRunner().invoke(
        cli.app, ["work", "claim", "mr-8", "--as", "dev/a", "--hive", "myrepo"]
    )

    def unavailable(main, entry):
        raise core.SessionUnavailable("no service")

    monkeypatch.setattr(work_lifecycle, "session_factory", unavailable)
    compat = CliRunner().invoke(
        cli.app, ["work", "claim", "mr-9", "--as", "dev/a", "--hive", "myrepo"]
    )

    assert api.exit_code == compat.exit_code == 0, (api.output, compat.output)
    assert api.stdout.replace("mr-8", "mr-X") == compat.stdout.replace("mr-9", "mr-X")
    assert "✓ claimed mr-8 as dev/a" in api.stdout and "## Requirements / goals" in api.stdout
    # The guard and both claim re-verifications read over HTTP; the one remaining bd read on the
    # api route is the worktree capability's own integration-base lookup.
    assert len(_bd_reads(fakebd, "mr-9")) - len(_bd_reads(fakebd, "mr-8")) == 3
    lease = ["update", "mr-8", "--claim"]
    assert ("dev/a", lease) in fakebd.calls  # the lease itself always takes its bd route


def test_bh_work_claim_refusal_renders_to_stderr_and_exits_1(hive, fakebd, served):
    fakebd.seed("mr-1", title="t", assignee="dev/bob")

    result = CliRunner().invoke(
        cli.app, ["work", "claim", "mr-1", "--as", "dev/a", "--hive", "myrepo"]
    )

    assert result.exit_code == 1
    assert "✗ bead mr-1 assigned to dev/bob (not dev/a) — refusing to steal" in result.stderr
    assert not _wt(hive, "mr-1").exists()


def test_claim_provisioning_failure_releases_through_the_bd_routes(
    hive, fakebd, served, monkeypatch
):
    fakebd.seed("mr-2", title="t")

    def disk_full(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(work.worktree, "ensure", disk_full)
    with pytest.raises(OSError):
        work._claim_single_bead(work.config.load(), "myrepo", "mr-2", "dev/a")
    assert fakebd.states["mr-2"]["dispatch"] == "provisioning_failed"
    assert (fakebd.beads["mr-2"]["status"], fakebd.beads["mr-2"]["assignee"]) == ("open", "")


def test_bh_work_abandon_verifies_the_release_over_http(hive, fakebd, served):
    fakebd.seed("mr-3", title="t")
    work.claim(bead="mr-3", as_="dev/carol", hive="myrepo")
    fakebd.calls.clear()

    result = CliRunner().invoke(cli.app, ["work", "abandon", "mr-3", "--rm", "--hive", "myrepo"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip().endswith("✓ abandoned mr-3; worktree removed")
    assert fakebd.states["mr-3"]["review"] == "abandoned"
    assert not _bd_reads(fakebd, "mr-3")
    assert not _wt(hive, "mr-3").exists()


def test_bh_work_resume_prints_feedback_between_its_own_lines(hive, fakebd, served):
    fakebd.seed("mr-4", title="t")
    work.claim(bead="mr-4", as_="dev/carol", hive="myrepo")
    fakebd.states.setdefault("mr-4", {})["review"] = "changes-requested"

    result = CliRunner().invoke(
        cli.app, ["work", "resume", "mr-4", "--as", "dev/carol", "--hive", "myrepo"]
    )

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "── review feedback ──"
    assert lines[-1] == f"✓ resumed mr-4 as dev/carol; worktree {_wt(hive, 'mr-4')}"
    assert fakebd.did("comments", "mr-4") and fakebd.did("update", "mr-4", "--claim")
