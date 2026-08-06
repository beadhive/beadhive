"""`bh dep list|show|install|auth` — one surface over the dep table (bh-hsus.6).

Three properties are load-bearing here and the rest is presentation:

1. `bh harness list|install|auth` still work, and are the SAME code path as `bh dep …` — they
   are aliases, not a second implementation that can drift.
2. `bh dep install` reads `deps.installable()` (what bh will run), never `has_install_route()`
   (what bh knows about). Conflating those is the bug this epic has already produced three times.
3. `bh plugin` is untouched and keeps exactly its three optional integrations. An empty
   `bh plugin gh` namespace is the wrapper `credentials`' docstring forbids.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import dep_cli, deps
from beadhive.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_probing(monkeypatch):
    """Neither stage may shell out from these tests: stage 1 runs a version command per row and
    stage 2 reaches the network for gh."""
    monkeypatch.setattr(deps, "present", lambda dep: dep.name != "podman")
    from beadhive import credentials

    monkeypatch.setattr(
        credentials,
        "probe",
        lambda dep: credentials.AuthReport(
            name=dep.name, installed=True, authenticated=True, how="stubbed", remedy=""
        ),
    )


# ---- list ----------------------------------------------------------------------------------


def test_list_shows_every_row_with_the_columns_the_bead_names():
    result = runner.invoke(app, ["dep", "list"])
    assert result.exit_code == 0
    for column in ("NAME", "KIND", "REQUIRED", "PRESENT", "AUTH", "INSTALL"):
        assert column in result.output
    for dep in deps.DEPS:
        assert dep.name in result.output


def test_list_names_the_group_and_whether_config_selects_it():
    """`group:agent` on its own does not answer the question an operator is asking, which is
    "is THIS the one my config picked"."""
    result = runner.invoke(app, ["dep", "list", "--kind", "harness"])
    assert "agent (selected)" in result.output  # claude, the default selector value
    assert "never" in result.output  # codex, which no config can select


def test_list_filters_by_kind_and_rejects_a_kind_that_is_not_in_the_table():
    harnesses = runner.invoke(app, ["dep", "list", "--kind", "harness"])
    assert "claude" in harnesses.output and "bd" not in harnesses.output

    bogus = runner.invoke(app, ["dep", "list", "--kind", "nope"])
    assert bogus.exit_code == 2
    assert "unknown kind" in bogus.output


def test_list_missing_shows_only_what_this_host_does_not_satisfy():
    result = runner.invoke(app, ["dep", "list", "--missing"])
    assert "podman" in result.output
    assert "claude" not in result.output


def test_list_still_surfaces_the_proprietary_licence():
    """bh-pc2a.36's point survives the move off `bh harness list`: the user is told what they
    are accepting, and told the image deliberately does not ship it."""
    result = runner.invoke(app, ["dep", "list", "--kind", "harness"])
    assert "SEE LICENSE IN README.md" in result.output
    assert "Proprietary tools are NOT shipped" in result.output


# ---- show ----------------------------------------------------------------------------------


def test_show_reports_one_row_including_its_selector():
    result = runner.invoke(app, ["dep", "show", "claude"])
    assert result.exit_code == 0
    assert "runs seats yes" in result.output
    assert "selector: harness" in result.output


def test_show_says_gh_is_infrastructure_and_names_no_harness():
    """gh is the row the whole epic turns on: it needs a credential, runs no seat, and bh has
    no install route for it. Nothing about it may read as a harness."""
    result = runner.invoke(app, ["dep", "show", "gh"])
    assert "kind       infra" in result.output
    assert "runs seats no" in result.output
    assert "harness" not in result.output.lower()


def test_show_rejects_an_unknown_name_with_the_known_list():
    result = runner.invoke(app, ["dep", "show", "nope"])
    assert result.exit_code == 2
    assert "unknown dep" in result.output
    assert "git-workspace" in result.output


# ---- install: the narrower predicate --------------------------------------------------------


def test_install_promises_a_command_only_for_rows_bh_will_actually_run():
    """`has_install_route()` is {claude, codex}; `installable()` is {claude}. The INSTALL column
    must read the narrower one or it offers codex a command that exits 1."""
    assert {d.name for d in deps.has_install_route()} == {"claude", "codex"}
    assert {d.name for d in deps.installable()} == {"claude"}

    result = runner.invoke(app, ["dep", "list", "--kind", "harness"])
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "bh dep install" in lines["claude"]
    assert "bh dep install" not in lines["codex"]


def test_install_of_a_row_with_no_route_at_all_says_so():
    """gh has no install route. Answering "unknown harness" — `harness.install`'s error — would
    be a lie about a row that is right there in the table."""
    result = runner.invoke(app, ["dep", "install", "gh"])
    assert result.exit_code == 1
    assert "no known install route" in result.output


def test_install_of_an_unknown_name_names_deps_not_harnesses():
    result = runner.invoke(app, ["dep", "install", "nope"])
    assert result.exit_code == 2
    assert "unknown dep" in result.output


def test_install_delegates_to_the_one_installer(monkeypatch):
    """One implementation of "place a binary", reused — not a second copy that can drift from
    the idempotence guard and the licence confirmation."""
    from beadhive import harness as harness_mod

    calls = []
    monkeypatch.setattr(harness_mod, "install", lambda name, **kw: calls.append((name, kw)))
    result = runner.invoke(app, ["dep", "install", "claude", "--yes"])
    assert result.exit_code == 0
    assert calls == [("claude", {"version": "", "yes": True})]


# ---- auth ----------------------------------------------------------------------------------


def test_auth_probes_a_named_row(monkeypatch):
    result = runner.invoke(app, ["dep", "auth", "gh"])
    assert result.exit_code == 0
    assert "Credentials:" in result.output
    assert "gh" in result.output


def test_auth_refuses_a_row_that_has_no_credential_to_probe():
    """`bd` is in the table but has no `auth` column, so there is nothing to report about it."""
    result = runner.invoke(app, ["dep", "auth", "bd"])
    assert result.exit_code == 2
    assert "no credential bh can probe" in result.output


def test_auth_check_passes_when_the_configured_agent_and_gh_are_authenticated():
    result = runner.invoke(app, ["dep", "auth", "--check"])
    assert result.exit_code == 0
    assert "host has the credentials it needs" in result.output


def test_auth_check_fails_a_codex_only_host(monkeypatch):
    """End to end through the CLI: THE reversal. A host with codex authenticated and claude not
    must FAIL `--check`, because `bh role --harness codex` will refuse to launch a seat on it."""
    from beadhive import credentials

    monkeypatch.setattr(
        credentials,
        "probe",
        lambda dep: credentials.AuthReport(
            name=dep.name,
            installed=True,
            authenticated=dep.name in ("gh", "codex"),
            how="stubbed",
            remedy="do the thing",
        ),
    )
    result = runner.invoke(app, ["dep", "auth", "--check"])
    assert result.exit_code == 1
    assert "no seat can run" in result.output


# ---- the aliases ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("list", dep_cli.ls), ("auth", dep_cli.auth), ("install", dep_cli.install)],
)
def test_bh_harness_verbs_still_exist(alias, canonical):
    """bh-q160.3's acceptance and the documented adoption sequences name `bh harness …`."""
    result = runner.invoke(app, ["harness", alias, "--help"])
    assert result.exit_code == 0
    assert "alias" in result.output


def test_the_harness_alias_calls_the_canonical_verb(monkeypatch):
    """A THIN alias: one call, no second implementation to drift from `bh dep`."""
    seen = []
    monkeypatch.setattr(dep_cli, "ls", lambda **kw: seen.append(kw))
    runner.invoke(app, ["harness", "list"])
    assert seen == [{"kind": "harness", "missing": False}]


def test_harness_auth_is_the_same_report_as_dep_auth():
    harness_out = runner.invoke(app, ["harness", "auth"]).output
    dep_out = runner.invoke(app, ["dep", "auth"]).output
    assert harness_out == dep_out


# ---- bh plugin is untouched -----------------------------------------------------------------


def test_bh_plugin_keeps_exactly_its_optional_integrations():
    """`bh plugin <name>` is a MOUNT POINT for a tool's own sub-app, not a namespace every dep
    gets. `bh plugin gh` would be empty, and an empty namespace invites the wrapper
    `credentials`' own docstring forbids."""
    from beadhive import plugins

    assert sorted(p.name for p in plugins.registry()) == ["hitch", "observaloop", "orca"]

    result = runner.invoke(app, ["plugin", "--help"])
    assert "gh" not in result.output.split("Commands")[-1]


def test_setup_check_is_still_gated_on_presence_alone():
    """`bh setup check` is UNCHANGED by this bead: stage 1 only, no auth probe folded in. The
    in-image manifest path is contractually zero-subprocess (`test_setup_manifest.py`) and every
    auth probe shells out, so the two gates stay separate even though the TABLE is one."""
    from beadhive import setup as setup_mod

    source = Path(setup_mod.__file__).read_text()
    assert "credentials" not in source
    assert "authenticated" not in source
