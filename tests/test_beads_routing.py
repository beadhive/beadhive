"""The beadhive-core cutover's one composition decision, its route config and its bounded rollback
(bh-sy36q.6, bh-m36pc).

Shell-level compatibility across the cutover: every migrated cohort seam opens its Beads session
through :mod:`beadhive.beads_routing`; the hive's ``work.beads.route`` decides whether a seam may
select its CLI-compatibility route when no capable session opens — ``api`` (the default) fails
closed in every cohort with one actionable diagnostic, ``api+cli-fallback`` reproduces the
bh-sy36q.6 automatic selection, ``cli`` reproduces the rollback; ``BH_BEADS_ROUTE`` still wins
over the key with its old meaning (``cli`` selects the CLI-compatibility route for all of them
before any Beads operation; a mistyped value fails the command with one diagnostic and is never
mistaken for "service unavailable"); and the installed ``bh`` command names, options, exit codes
and output of the migrated verbs are the same on both routes.

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
    config_schema,
    dispatch_state,
    host_beads,
    plan_filing,
    work_lifecycle,
    work_queue,
)
from beadhive_beads_client import CapabilityMissing, IncompatibleService
from beadhive_beads_client.service import ServiceUnavailable
from test_work import CONFIG_YAML, _wt, fakebd, hive
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


# ---- work.beads.route: the per-hive route config (bh-m36pc) ----------------------------------

#: The line every FakeBd-backed ``test_work`` config carries to opt into the bd route.
OPT_IN = '  beads: {route: "api+cli-fallback"}  # FakeBd-backed: opt into the bd route (bh-m36pc)\n'
FALLBACK = beads_routing.ROUTE_API_CLI_FALLBACK


def _entry(route: str | None) -> dict:
    """A registry entry, optionally carrying a per-hive ``work.beads.route``."""
    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    return entry if route is None else {**entry, "work": {"beads": {"route": route}}}


DEFAULT = _entry(None)
HIVE = "github/myorg/myrepo"


def _no_session(error: BaseException):
    def factory(_main, _entry):
        raise error

    return factory


#: Every "no capable session" error a seam used to map, unconditionally, onto its ``bd`` route.
UNAVAILABLE = [
    pytest.param(
        ServiceUnavailable("no service", state="absent", start_command="bh host beads start"),
        id="service-unavailable",
    ),
    pytest.param(core.SessionUnavailable("myrepo uses embedded Dolt"), id="session-unavailable"),
    pytest.param(IncompatibleService("wire contract differs"), id="incompatible"),
    pytest.param(CapabilityMissing("work.claim-next"), id="capability-missing"),
]


@pytest.fixture
def no_bd(monkeypatch):
    """Fail the test on any ``bd`` spawn: proof a seam did not select its CLI route."""
    from beadhive import bd

    def refuse(*args, **_kw):
        raise AssertionError(f"the bd CLI route was selected: {args!r}")

    monkeypatch.setattr(bd, "_run", refuse)
    monkeypatch.setattr(bd, "run", refuse)


def _assert_fails_closed(refused, capsys, detail: str) -> None:
    message = str(refused.value)
    assert refused.value.exit_code == 1
    assert f"no capable Beads session for {HIVE}: {detail}" in message
    assert f"`bh host beads start --hive {HIVE}`" in message
    assert "work.beads.route is 'api'" in message
    assert "set work.beads.route to 'api+cli-fallback' (or 'cli') for this hive" in message
    assert capsys.readouterr().err == f"✗ {message}\n"


def test_the_route_key_is_a_validated_literal_defaulting_to_api():
    assert config_schema.literal_choices("work.beads.route") == ("api", "api+cli-fallback", "cli")
    assert config_schema.field_default("work.beads.route") == "api"
    assert config_schema.WorkConfig().beads.route == "api"
    with pytest.raises(ValueError, match="work|beads|route|literal"):
        config_schema.WorkConfig.model_validate({"beads": {"route": "bd"}})


def test_an_absent_route_resolves_to_api():
    assert beads_routing.configured_route(DEFAULT, {}) == "api"
    assert beads_routing.route(DEFAULT, {}) == "api"
    assert beads_routing.route({"prefix": "x"}, {"work": {"beads": {}}}) == "api"
    assert beads_routing.route(DEFAULT) == "api"  # the sandboxed global config sets no route


@pytest.mark.parametrize("value", ["api", FALLBACK, "cli"])
def test_the_route_resolves_per_hive_over_global(value):
    cfg = {"work": {"beads": {"route": "cli"}}}
    assert beads_routing.route(_entry(value), cfg) == value
    assert beads_routing.route(DEFAULT, cfg) == "cli"  # the global value when the hive is silent


@pytest.mark.parametrize("value", ["API", "bd", "api+cli", "", None, 1])
def test_a_mistyped_route_key_is_refused_with_one_diagnostic(capsys, value):
    with pytest.raises(beads_routing.BeadsRouteConfigInvalid) as refused:
        beads_routing.route({**DEFAULT, "work": {"beads": {"route": value}}})

    message = (
        f"work.beads.route={value!r} is not a Beads route; use one of 'api', "
        "'api+cli-fallback', 'cli'"
    )
    assert refused.value.exit_code == 2
    assert str(refused.value) == message
    assert capsys.readouterr().err == f"✗ {message}\n"
    assert not isinstance(refused.value, (*beads_routing.unavailable_errors(), OSError, ValueError))


@pytest.mark.parametrize(
    ("env", "effective"), [("api", FALLBACK), (" API ", FALLBACK), ("cli", "cli")]
)
@pytest.mark.parametrize("key", ["api", FALLBACK, "cli", "bogus"])
def test_bh_beads_route_still_wins_over_the_key_with_its_old_meaning(
    monkeypatch, env, effective, key
):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, env)
    assert beads_routing.route(_entry(key)) == effective


def test_a_mistyped_bh_beads_route_is_still_refused_whatever_the_key_says(monkeypatch):
    monkeypatch.setenv(beads_routing.ROUTE_ENV, "bd")
    with pytest.raises(beads_routing.BeadsRouteInvalid):
        beads_routing.route(_entry(FALLBACK))


@pytest.mark.parametrize(("seam", "capabilities"), SEAMS)
def test_a_cli_route_key_refuses_every_cohort_session_before_resolving_the_service(
    resolved, seam, capabilities
):
    with pytest.raises(core.SessionUnavailable, match="work.beads.route=cli"):
        seam.hive_session(MAIN, _entry("cli"))

    assert resolved == []


@pytest.mark.parametrize("key", ["api", FALLBACK])
@pytest.mark.parametrize(("seam", "capabilities"), SEAMS)
def test_an_api_route_key_resolves_the_service(resolved, seam, capabilities, key):
    assert seam.hive_session(MAIN, _entry(key)) == "session"
    assert resolved == [(MAIN, getattr(core, capabilities), _entry(key))]


# ---- default ``api``: every cohort fails closed instead of selecting bd ----------------------


@pytest.mark.parametrize("error", UNAVAILABLE)
def test_api_fails_closed_for_ready_schedule_and_claim_next(monkeypatch, no_bd, capsys, error):
    monkeypatch.setattr(work_queue, "session_factory", _no_session(error))

    for call in (
        lambda: work_queue.open_ready(MAIN, DEFAULT, limit=0),
        lambda: work_queue.open_children(MAIN, DEFAULT, "mr-epic"),
        lambda: work_queue.claim_next(MAIN, DEFAULT, "dev/alice"),
    ):
        with pytest.raises(beads_routing.BeadsServiceRequired) as refused:
            call()
        assert refused.value.__cause__ is error
        _assert_fails_closed(refused, capsys, str(error))


@pytest.mark.parametrize("error", UNAVAILABLE)
def test_api_fails_closed_for_every_dispatch_read(monkeypatch, no_bd, capsys, error):
    monkeypatch.setattr(dispatch_state, "session_factory", _no_session(error))

    for call in (
        lambda: dispatch_state.open_molecule_progress(MAIN, DEFAULT, "mr-epic"),
        lambda: dispatch_state.open_local_loop_state(MAIN, DEFAULT, "mr-1"),
        lambda: dispatch_state.open_swarm_members(MAIN, DEFAULT, "mr-epic"),
        lambda: dispatch_state.open_event_rows(MAIN, DEFAULT, "mr-1"),
        lambda: dispatch_state.open_poll_ready(MAIN, DEFAULT),
    ):
        with pytest.raises(beads_routing.BeadsServiceRequired) as refused:
            call()
        assert refused.value.__cause__ is error
        _assert_fails_closed(refused, capsys, str(error))


@pytest.mark.parametrize("error", UNAVAILABLE)
def test_api_fails_closed_for_molecule_filing(monkeypatch, no_bd, capsys, error):
    monkeypatch.setattr(plan_filing, "session_factory", _no_session(error))

    with pytest.raises(beads_routing.BeadsServiceRequired) as refused:
        with plan_filing._filer(MAIN, DEFAULT):
            pytest.fail("no filer may be yielded under the api route")
    assert refused.value.__cause__ is error
    _assert_fails_closed(refused, capsys, str(error))


@pytest.mark.parametrize("error", UNAVAILABLE)
def test_api_fails_closed_for_the_lifecycle_issues_port(monkeypatch, no_bd, capsys, error):
    monkeypatch.setattr(work_lifecycle, "session_factory", _no_session(error))

    with ExitStack() as stack:
        issues = work_lifecycle.SelectedIssues(MAIN, DEFAULT, stack)
        with pytest.raises(beads_routing.BeadsServiceRequired) as refused:
            issues.get("mr-1")
    assert refused.value.__cause__ is error
    _assert_fails_closed(refused, capsys, str(error))


def test_an_unaddressable_entry_still_fails_closed_naming_a_placeholder_hive(
    monkeypatch, no_bd, capsys
):
    monkeypatch.setattr(host_beads, "resolve_session", lambda *_a, **_k: {}["repo"])

    with pytest.raises(beads_routing.BeadsServiceRequired) as refused:
        dispatch_state.open_poll_ready(MAIN, {"prefix": "x"})

    assert "`bh host beads start --hive <hive>`" in str(refused.value)
    assert "cannot address hive from entry" in str(refused.value)
    capsys.readouterr()


# ---- api+cli-fallback / cli: bit-for-bit the bh-sy36q.6 selection -----------------------------


@pytest.mark.parametrize("key", [FALLBACK, "cli"])
@pytest.mark.parametrize("error", UNAVAILABLE)
def test_an_opted_in_hive_selects_the_cli_route_for_ready_schedule_and_claim_next(
    monkeypatch, capsys, key, error
):
    monkeypatch.setattr(work_queue, "session_factory", _no_session(error))
    entry = _entry(key)

    assert work_queue.open_ready(MAIN, entry, limit=0) is None
    assert work_queue.open_children(MAIN, entry, "mr-epic") is None
    assert work_queue.claim_next(MAIN, entry, "dev/alice") is None
    assert "✗" not in capsys.readouterr().err  # the selection is logged, never refused


@pytest.mark.parametrize("key", [FALLBACK, "cli"])
@pytest.mark.parametrize("error", UNAVAILABLE)
def test_an_opted_in_hive_selects_the_cli_route_for_every_dispatch_read(
    monkeypatch, capsys, key, error
):
    monkeypatch.setattr(dispatch_state, "session_factory", _no_session(error))
    entry = _entry(key)

    assert dispatch_state.open_molecule_progress(MAIN, entry, "mr-epic") is None
    assert dispatch_state.open_local_loop_state(MAIN, entry, "mr-1") is None
    assert dispatch_state.open_swarm_members(MAIN, entry, "mr-epic") is None
    assert dispatch_state.open_event_rows(MAIN, entry, "mr-1") is None
    assert dispatch_state.open_poll_ready(MAIN, entry) is None
    assert "✗" not in capsys.readouterr().err  # the selection is logged, never refused


@pytest.mark.parametrize("key", [FALLBACK, "cli"])
@pytest.mark.parametrize("error", UNAVAILABLE)
def test_an_opted_in_hive_selects_the_cli_molecule_filer_and_issues_port(monkeypatch, key, error):
    monkeypatch.setattr(plan_filing, "session_factory", _no_session(error))
    monkeypatch.setattr(work_lifecycle, "session_factory", _no_session(error))

    with plan_filing._filer(MAIN, _entry(key)) as filer:
        assert isinstance(filer, plan_filing.CliMoleculeFiler)
    with ExitStack() as stack:
        issues = work_lifecycle.SelectedIssues(MAIN, _entry(key), stack)
        assert issues.route == "cli-compatibility"


def test_a_cli_route_key_selects_every_cohort_cli_route_without_resolving(resolved):
    entry = _entry("cli")

    assert work_queue.open_ready(MAIN, entry, limit=0) is None
    assert work_queue.open_children(MAIN, entry, "mr-epic") is None
    assert work_queue.claim_next(MAIN, entry, "dev/alice") is None
    assert dispatch_state.open_poll_ready(MAIN, entry) is None
    with plan_filing._filer(MAIN, entry) as filer:
        assert isinstance(filer, plan_filing.CliMoleculeFiler)
    with ExitStack() as stack:
        assert work_lifecycle.SelectedIssues(MAIN, entry, stack).route == "cli-compatibility"
    assert resolved == []


# ---- the installed CLI under the default route ------------------------------------------------


@pytest.fixture
def api_default(hive):
    """The ``test_work`` hive with NO route key: the default ``api`` route, and no service."""
    assert OPT_IN in CONFIG_YAML
    hive.cfg_path.write_text(CONFIG_YAML.replace(OPT_IN, ""))
    return hive


def _refusal_line(result) -> str:
    return next(line for line in result.stderr.splitlines() if line.startswith("✗ no capable"))


def test_bh_work_claim_fails_closed_under_the_default_route(api_default, fakebd):
    fakebd.seed("mr-1", title="t", description="the brief")

    result = CliRunner().invoke(
        cli.app, ["work", "claim", "mr-1", "--as", "dev/a", "--hive", "myrepo"]
    )

    assert result.exit_code == 1, result.output
    line = _refusal_line(result)
    assert "no capable Beads session for github/myorg/myrepo" in line
    assert "`bh host beads start --hive github/myorg/myrepo`" in line
    assert "work.beads.route" in line
    # Only the state-sync refresh (`bd dolt pull`, outside the route) ran; no bead read or write.
    bead_calls = [args for _actor, args in fakebd.calls if args[:1] != ["dolt"]]
    assert bead_calls == [], "the bd route must not be selected"
    assert fakebd.beads["mr-1"]["assignee"] == ""
    assert not _wt(api_default, "mr-1").exists()


def test_bh_work_ready_json_and_next_fail_closed_under_the_default_route(api_default, fakebd):
    fakebd.seed("mr-1", title="t")
    runner = CliRunner()

    ready = runner.invoke(cli.app, ["work", "ready", "--hive", "myrepo", "--json", "--limit", "0"])
    nxt = runner.invoke(cli.app, ["work", "next", "--as", "dev/a", "--hive", "myrepo"])

    for result in (ready, nxt):
        assert result.exit_code == 1, result.output
        assert "work.beads.route" in _refusal_line(result)
    assert not fakebd.did("ready")
    assert not fakebd.did("update", "--claim")


def test_bh_work_claim_selects_bd_when_the_hive_opts_in(hive, fakebd):
    """The same composition as above, with the ``test_work`` hive's opt-in line: bd serves it."""
    fakebd.seed("mr-1", title="t", description="the brief")

    result = CliRunner().invoke(
        cli.app, ["work", "claim", "mr-1", "--as", "dev/a", "--hive", "myrepo"]
    )

    assert result.exit_code == 0, result.output
    assert fakebd.did("update", "mr-1", "--claim")
    assert _wt(hive, "mr-1").exists()


def test_approve_and_bounce_stay_outside_the_route_key(monkeypatch):
    """``work_review`` never consults ``work.beads.route``: even ``cli`` resolves the service."""
    from beadhive import work_review

    calls = []
    monkeypatch.setattr(
        host_beads,
        "resolve_session",
        lambda main, caps, *, entry: calls.append((main, caps, entry)) or "session",
    )

    assert work_review.hive_session(MAIN, _entry("cli")) == "session"
    assert calls == [(MAIN, core.REVIEW_CAPABILITIES, _entry("cli"))]
