"""The beadhive-core cutover's one composition decision and its bounded rollback (bh-sy36q.6).

Shell-level compatibility across the cutover: every migrated cohort seam opens its Beads session
through :mod:`beadhive.beads_routing`; ``BH_BEADS_ROUTE=cli`` selects the CLI-compatibility route
for all of them before any Beads operation; a mistyped value fails the command with one diagnostic
and is never mistaken for "service unavailable"; and the installed ``bh`` command names, options,
exit codes and output of the migrated verbs are the same on both routes.

The full Click parameter inventory of ``bh work`` / ``bh plan`` is additionally hash-pinned by
``tests/test_cli_projection.py``; the explicit table here names the migrated verbs' contract so a
reviewer can read it, and so a cutover regression names the verb that broke.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import click
import pytest
import typer.main
from typer.testing import CliRunner

import beadhive_core as core
from beadhive import (
    beads_routing,
    cli,
    dispatch_state,
    host_beads,
    plan_filing,
    work_lifecycle,
    work_queue,
)
from test_work import _wt, fakebd, hive
from test_work_lifecycle_shell import served

__all__ = ["fakebd", "hive", "served"]

MAIN = Path("/hive/main")
ENTRY = {"repo": "r"}

#: (seam module, its capability set): every migrated cohort's composition seam.
SEAMS = [
    pytest.param(work_queue, "QUEUE_CAPABILITIES", id="ready-schedule-claim-next"),
    pytest.param(work_lifecycle, "LIFECYCLE_CAPABILITIES", id="lifecycle"),
    pytest.param(plan_filing, "PLANNING_CAPABILITIES", id="molecule-filing"),
    pytest.param(dispatch_state, "DISPATCH_CAPABILITIES", id="dispatch-reads"),
]


@pytest.fixture(autouse=True)
def _default_route(monkeypatch):
    monkeypatch.delenv(beads_routing.ROUTE_ENV, raising=False)


@pytest.fixture
def resolved(monkeypatch):
    """Record every attempt to resolve the supervised service; answer with a sentinel."""
    calls = []

    def resolve(main, caps, *, entry):
        calls.append((main, caps, entry))
        return "session"

    monkeypatch.setattr(host_beads, "resolve_session", resolve)
    return calls


# ---- the switch ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "api", "API", " api "])
def test_the_default_route_is_beadhive_core(monkeypatch, value):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, value)
    assert beads_routing.selected_route() == "api"


@pytest.mark.parametrize("value", ["cli", "CLI", " cli\n"])
def test_the_rollback_route_is_cli_compatibility(monkeypatch, value):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, value)
    assert beads_routing.selected_route() == "cli"


def test_a_mistyped_route_is_refused_with_one_diagnostic(monkeypatch, capsys):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "bd")

    with pytest.raises(beads_routing.BeadsRouteInvalid) as refused:
        beads_routing.selected_route()

    message = (
        "BH_BEADS_ROUTE='bd' is not a Beads route; use 'api' (default) or 'cli' "
        "(rollback to the CLI-compatibility route)"
    )
    assert refused.value.exit_code == 2
    assert str(refused.value) == message
    assert capsys.readouterr().err == f"✗ {message}\n"
    # Never one of the errors a seam maps onto its CLI-compatibility route.
    assert not isinstance(refused.value, (*beads_routing.unavailable_errors(), OSError, ValueError))


# ---- one composition point -------------------------------------------------------------------


@pytest.mark.parametrize(("seam", "capabilities"), SEAMS)
def test_every_cohort_seam_opens_its_session_through_the_one_composition_point(
    resolved, seam, capabilities
):
    assert seam.session_factory is seam.hive_session
    assert seam.hive_session(MAIN, ENTRY) == "session"
    assert resolved == [(MAIN, getattr(core, capabilities), ENTRY)]


@pytest.mark.parametrize(("seam", "capabilities"), SEAMS)
def test_rollback_refuses_every_cohort_session_before_resolving_the_service(
    monkeypatch, resolved, seam, capabilities
):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "cli")

    with pytest.raises(core.SessionUnavailable, match="BH_BEADS_ROUTE=cli: rollback"):
        seam.hive_session(MAIN, ENTRY)

    assert resolved == []


def test_an_unservable_or_unaddressable_hive_is_a_session_refusal(monkeypatch):
    def unservable(*_a, **_k):
        raise host_beads.HiveNotServable("myrepo uses embedded Dolt")

    monkeypatch.setattr(host_beads, "resolve_session", unservable)
    with pytest.raises(core.SessionUnavailable, match="embedded Dolt"):
        beads_routing.hive_session(MAIN, ENTRY, frozenset())

    def unaddressable(*_a, **_k):
        raise KeyError("repo")

    monkeypatch.setattr(host_beads, "resolve_session", unaddressable)
    with pytest.raises(core.SessionUnavailable, match="cannot address hive from entry"):
        beads_routing.hive_session(MAIN, {"prefix": "x"}, frozenset())


# ---- rollback selects each cohort's CLI-compatibility route ----------------------------------


def test_rollback_selects_the_cli_route_for_ready_schedule_and_claim_next(monkeypatch, resolved):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "cli")

    assert work_queue.open_ready(MAIN, ENTRY, limit=0) is None
    assert work_queue.open_children(MAIN, ENTRY, "bh-epic") is None
    assert work_queue.claim_next(MAIN, ENTRY, "dev/alice") is None
    assert resolved == []


def test_rollback_selects_the_cli_route_for_every_dispatch_read(monkeypatch, resolved):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "cli")

    assert dispatch_state.open_molecule_progress(MAIN, ENTRY, "bh-epic") is None
    assert dispatch_state.open_local_loop_state(MAIN, ENTRY, "bh-1") is None
    assert dispatch_state.open_swarm_members(MAIN, ENTRY, "bh-epic") is None
    assert dispatch_state.open_event_rows(MAIN, ENTRY, "bh-1") is None
    assert dispatch_state.open_poll_ready(MAIN, ENTRY) is None
    assert resolved == []


def test_rollback_selects_the_cli_molecule_filer(monkeypatch, resolved):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "cli")

    with plan_filing._filer(MAIN, ENTRY) as filer:
        assert isinstance(filer, plan_filing.CliMoleculeFiler)
    assert resolved == []


def test_a_mistyped_route_fails_every_seam_instead_of_rerouting(monkeypatch, resolved):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "http")

    with pytest.raises(beads_routing.BeadsRouteInvalid):
        work_queue.open_ready(MAIN, ENTRY, limit=0)
    with pytest.raises(beads_routing.BeadsRouteInvalid):
        dispatch_state.open_poll_ready(MAIN, ENTRY)
    with pytest.raises(beads_routing.BeadsRouteInvalid):
        with plan_filing._filer(MAIN, ENTRY):
            pass
    with pytest.raises(beads_routing.BeadsRouteInvalid):
        work_lifecycle.SelectedIssues(MAIN, ENTRY, ExitStack()).get("bh-1")
    assert resolved == []


# ---- the installed CLI across the cutover ----------------------------------------------------


@pytest.fixture
def composed(served, monkeypatch):
    """The real composition: the default session factory, resolving to the served HTTP fixture."""
    serve = work_lifecycle.session_factory
    monkeypatch.setattr(work_lifecycle, "session_factory", work_lifecycle.hive_session)
    monkeypatch.setattr(
        host_beads, "resolve_session", lambda main, _caps, *, entry: serve(main, entry)
    )
    return served


def test_bh_work_claim_is_byte_identical_under_the_rollback(hive, fakebd, composed, monkeypatch):
    fakebd.seed("mr-8", title="t", description="the brief")
    fakebd.seed("mr-9", title="t", description="the brief")
    runner = CliRunner()

    api = runner.invoke(cli.app, ["work", "claim", "mr-8", "--as", "dev/a", "--hive", "myrepo"])
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "cli")
    rollback = runner.invoke(
        cli.app, ["work", "claim", "mr-9", "--as", "dev/a", "--hive", "myrepo"]
    )

    assert api.exit_code == rollback.exit_code == 0, (api.output, rollback.output)
    assert api.stdout.replace("mr-8", "mr-X") == rollback.stdout.replace("mr-9", "mr-X")
    assert _wt(hive, "mr-8").exists() and _wt(hive, "mr-9").exists()


def test_bh_work_assign_writes_over_http_by_default_and_through_bd_under_rollback(
    hive, fakebd, composed, monkeypatch
):
    fakebd.seed("mr-1", title="t")
    fakebd.seed("mr-2", title="t")
    runner = CliRunner()
    argv = ["--to", "dev/carol", "--as", "disp/lead", "--hive", "myrepo"]

    api = runner.invoke(cli.app, ["work", "assign", "mr-1", *argv])
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "cli")
    rollback = runner.invoke(cli.app, ["work", "assign", "mr-2", *argv])

    assert api.exit_code == rollback.exit_code == 0, (api.output, rollback.output)
    assert [bead for _method, bead, _body in composed] == ["mr-1"]
    assert not fakebd.did("assign", "mr-1")
    assert fakebd.did("assign", "mr-2", "dev/carol")
    assert fakebd.beads["mr-1"]["assignee"] == fakebd.beads["mr-2"]["assignee"] == "dev/carol"


def test_bh_work_claim_refusal_keeps_its_exit_code_and_diagnostic_under_rollback(
    hive, fakebd, composed, monkeypatch
):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "cli")
    fakebd.seed("mr-1", title="t", assignee="dev/bob")

    result = CliRunner().invoke(
        cli.app, ["work", "claim", "mr-1", "--as", "dev/a", "--hive", "myrepo"]
    )

    assert result.exit_code == 1
    assert "✗ bead mr-1 assigned to dev/bob (not dev/a) — refusing to steal" in result.stderr
    assert composed == []


def test_bh_work_claim_with_a_mistyped_route_exits_2_before_any_write(
    hive, fakebd, composed, monkeypatch
):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "bogus")
    fakebd.seed("mr-1", title="t")

    result = CliRunner().invoke(
        cli.app, ["work", "claim", "mr-1", "--as", "dev/a", "--hive", "myrepo"]
    )

    assert result.exit_code == 2
    assert "✗ BH_BEADS_ROUTE='bogus' is not a Beads route" in result.stderr
    assert not fakebd.did("update", "mr-1", "--claim")
    assert fakebd.beads["mr-1"]["assignee"] == ""
    assert not _wt(hive, "mr-1").exists()


#: The migrated verbs' installed command-line contract: arguments and flags, in order.
MIGRATED_VERBS = {
    ("work", "assign"): ["bead", "--to", "--as", "--hive", "--preview", "--json"],
    ("work", "claim"): ["bead", "--as", "--group", "--collapse", "--hive", "--preview", "--json"],
    ("work", "resume"): ["bead", "--as", "--hive"],
    ("work", "abandon"): ["bead", "--hive", "--rm"],
    ("work", "start"): ["epic", "--as", "--hive"],
    ("work", "next"): ["--as", "--hive", "--json", "--epic"],
    ("work", "ready"): ["--hive"],  # every other flag forwards to `bd ready` verbatim
    ("work", "schedule"): ["epic", "--hive", "--json"],
    ("work", "submit"): [
        "bead",
        "--as",
        "--hive",
        "--group",
        "--override-validation",
        "--override-as",
    ],
    ("plan", "file"): ["spec", "--dry-run", "--save", "--hive"],
    ("plan", "verify"): ["epic", "--hive"],
    ("plan", "approve"): ["epic", "--hive"],
    ("plan", "repair"): ["epic", "--hive"],
}


@pytest.mark.parametrize("path", sorted(MIGRATED_VERBS), ids=" ".join)
def test_migrated_verbs_keep_their_command_names_and_options(path):
    root = typer.main.get_command(cli.app)
    context = click.Context(root)
    command = root.get_command(context, path[0]).get_command(context, path[1])

    assert command is not None, f"bh {' '.join(path)} is gone"
    params = [
        param.opts[0] if param.param_type_name == "option" else param.name
        for param in command.params
    ]
    assert params == MIGRATED_VERBS[path]
