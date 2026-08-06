"""`bh dep auth` — probe the credentials bh's deps need (bh-q160.3, renamed by bh-hsus.6).

The load-bearing test here is :func:`test_no_credential_value_ever_reaches_the_report`. Every
other behaviour is a convenience; leaking a token into stdout, a log or an OTEL attribute is the
one failure that cannot be walked back once it has happened. It survives the rename UNCHANGED in
substance — same planted secret, same assertion that it appears in no report field and no rendered
line — because the probe logic moved without being rewritten.

bh-hsus.6 also REVERSES one of bh-q160.3's accepted behaviours, with operator approval: the
pairwise claude-or-codex OR in :func:`credentials.unmet` becomes the selector model. See
:func:`test_a_codex_only_host_fails_the_gate`.
"""

from __future__ import annotations

import pytest

from beadhive import credentials, deps


def _gated():
    """The rows with a credential gate — DERIVED, exactly as the module derives them."""
    return deps.authenticated_deps()


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """The developer running these tests almost certainly HAS credentials. Without clearing
    them the "unauthenticated" cases silently pass for the wrong reason."""
    for dep in _gated():
        for var in dep.auth.env_vars:
            monkeypatch.delenv(var, raising=False)
    # The agent-group selector reads BH_HARNESS first, so an operator running with it exported
    # would silently change which row `unmet` requires.
    monkeypatch.delenv("BH_HARNESS", raising=False)


@pytest.fixture(autouse=True)
def _no_stored_credentials(monkeypatch, tmp_path):
    """Point every on-disk credential at an empty tmp dir. A developer's real ~/.claude would
    otherwise make the "not authenticated" cases pass for the wrong reason too."""
    for dep in _gated():
        if dep.auth.stored is not None:
            monkeypatch.setenv(dep.auth.stored.dir_env, str(tmp_path / dep.name))


def _probe(name: str) -> credentials.AuthReport:
    return credentials.probe(deps.by_name(name))


def _no_binaries(monkeypatch):
    monkeypatch.setattr(credentials.shutil, "which", lambda _: None)


# ---- the contract that matters ------------------------------------------------------------


def test_no_credential_value_ever_reaches_the_report(monkeypatch):
    """A probe reports that a variable is SET, never what it holds."""
    secret = "ghp_thismustneverappearanywhere"
    monkeypatch.setattr(credentials.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    reports = credentials.probe_all()
    rendered = "\n".join(credentials.render(reports))

    assert secret not in rendered
    for report in reports:
        assert secret not in f"{report.how}{report.detail}{report.remedy}{report.path}"
    # …and the NAME is what gets reported, because that is the useful half.
    assert "GH_TOKEN (environment)" in rendered


def test_the_keychain_query_can_never_print_the_secret(monkeypatch):
    """`security find-generic-password` WITHOUT `-w` reports existence and nothing else. With
    `-w` it prints the credential to stdout, where a log or an OTEL attribute would catch it."""
    seen = []

    class _Proc:
        returncode = 0

    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.setattr(credentials, "run", lambda cmd, **k: seen.append(cmd) or _Proc())

    credentials._keychain_credential("Claude Code-credentials")

    assert seen and "-w" not in seen[0]


def test_env_source_returns_the_name_not_the_value(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret-value")
    assert credentials._env_source(("GH_TOKEN",)) == "GH_TOKEN"


def test_blank_env_var_is_not_a_credential(monkeypatch):
    """An exported-but-empty var is the classic half-configured host; it must not read as auth."""
    monkeypatch.setenv("GH_TOKEN", "   ")
    assert credentials._env_source(("GH_TOKEN",)) is None


# ---- there is no registry ------------------------------------------------------------------


def test_the_probed_set_derives_from_the_table(monkeypatch):
    """bh-hsus.6 deleted `PROBES`. What gets probed is `[d for d in DEPS if d.auth]` and nothing
    else — so a row that grows an `auth` column is probed with no edit to this module."""
    _no_binaries(monkeypatch)
    assert [r.name for r in credentials.probe_all()] == [d.name for d in _gated()]
    assert not hasattr(credentials, "PROBES")


def test_probing_a_row_with_no_credential_is_a_caller_bug():
    """`bd` needs no credential; asking for its credential state is not a finding, it is a bug."""
    with pytest.raises(ValueError):
        credentials.probe(deps.by_name("bd"))


# ---- gh -----------------------------------------------------------------------------------


def test_gh_env_token_wins_over_stored_login(monkeypatch):
    """gh resolves GH_TOKEN before any stored login, so reporting the stored one would describe
    a credential gh is not actually using."""
    monkeypatch.setattr(credentials.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(
        credentials, "_status_login", lambda _s: pytest.fail("must not probe when env wins")
    )
    report = _probe("gh")
    assert report.authenticated and report.how == "GH_TOKEN (environment)"


def test_gh_reports_the_git_protocol(monkeypatch):
    """https-vs-ssh is what breaks HQ sync (bh-pc2a.30), so the report must name it."""
    monkeypatch.setattr(credentials.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(credentials, "_status_login", lambda _s: (True, "ssh"))
    assert "ssh" in _probe("gh").detail


def test_gh_unauthenticated_names_the_device_flow(monkeypatch):
    """The device flow is the ONE route that works with no browser on the box."""
    monkeypatch.setattr(credentials.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(credentials, "_status_login", lambda _s: (False, ""))
    report = _probe("gh")
    assert not report.authenticated
    assert "--web" in report.remedy


def test_gh_probe_survives_a_hang(monkeypatch):
    """A wedged `gh auth status` must become a finding, not a hung provision run."""

    def _timeout(*_a, **_k):
        raise credentials.subprocess.TimeoutExpired(cmd="gh", timeout=1)

    monkeypatch.setattr(credentials, "run", _timeout)
    assert credentials._status_login(deps.by_name("gh").auth.status) == (False, "")


# ---- claude / codex -----------------------------------------------------------------------


def test_claude_keychain_is_consulted_on_macos(monkeypatch):
    """The first cut probed only for a file and called a working macOS install unauthenticated —
    the exact false negative this verb exists to prevent, complete with a useless remedy."""
    monkeypatch.setattr(credentials.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(credentials, "_keychain_credential", lambda _s: True)
    report = _probe("claude")
    assert report.authenticated
    assert "Keychain" in report.how


def test_claude_keychain_never_queries_off_darwin(monkeypatch):
    """`security` is a macOS binary; a missing-command error is not a finding worth reporting."""
    monkeypatch.setattr(credentials.sys, "platform", "linux")
    monkeypatch.setattr(
        credentials, "run", lambda *a, **k: pytest.fail("must not shell out off darwin")
    )
    assert credentials._keychain_credential("Claude Code-credentials") is False


def test_the_linux_file_path_is_what_answers_off_darwin(monkeypatch, tmp_path):
    """The other half of the false negative: on Linux there is no Keychain, so the on-disk
    credential each tool writes for itself is the only stored-login evidence there is."""
    monkeypatch.setattr(credentials.sys, "platform", "linux")
    monkeypatch.setattr(credentials.shutil, "which", lambda n: f"/usr/bin/{n}")
    stored = deps.by_name("claude").auth.stored
    home = tmp_path / "claude-home"
    home.mkdir()
    (home / stored.filename).write_text("{}")
    monkeypatch.setenv(stored.dir_env, str(home))

    report = _probe("claude")

    assert report.authenticated and report.how == "stored login"
    assert "presence, not validity" in report.detail


def test_missing_binaries_report_not_installed(monkeypatch):
    _no_binaries(monkeypatch)
    for report in credentials.probe_all():
        assert not report.installed and not report.authenticated
        assert report.remedy, "an absent target must always name its remedy"


def test_an_absent_row_is_never_sent_to_a_command_that_refuses(monkeypatch):
    """bh-tccp, and the FIFTH instance of one shape: an absent row's remedy named
    `bh dep install <name>` for a row bh does not install, so the operator was routed to a
    command that exits 1 and then prints three DIFFERENT routes.

    Asserted over EVERY row rather than codex alone — naming one row is what let this recur four
    times before. A row may name the install verb only when `install.cmd` would actually run."""
    _no_binaries(monkeypatch)

    for report in credentials.probe_all():
        spec = deps.by_name(report.name)
        installable = spec.install is not None and spec.install.cmd is not None
        if not installable:
            assert f"bh dep install {report.name}" not in report.remedy, (
                f"{report.name} has no bh-driven install, so its remedy must not name the verb "
                f"that refuses — got: {report.remedy}"
            )


def test_codex_absent_remedy_carries_the_real_routes(monkeypatch):
    """The positive half: refusing the dead end is only useful if what replaces it is actionable.
    codex's three plane-specific routes (bh-hsus.1) are what the operator actually needs."""
    _no_binaries(monkeypatch)

    codex = next(r for r in credentials.probe_all() if r.name == "codex")

    assert "brew install --cask codex" in codex.remedy
    assert "github.com/openai/codex/releases" in codex.remedy
    assert "nixpkgs#codex" in codex.remedy


# ---- the host-level requirement ------------------------------------------------------------


def _report(name, *, authenticated, installed=True):
    return credentials.AuthReport(
        name=name, installed=installed, authenticated=authenticated, how="x", remedy=""
    )


def test_a_codex_only_host_fails_the_gate():
    """THE DELIBERATE REVERSAL (bh-hsus.6, operator-approved 2026-08-05).

    bh-q160.3 accepted "a codex-only host is legitimate and must not fail the gate" and built it
    as a pairwise claude-or-codex OR. That acceptance rested on a premise bh-hsus.2's spike
    verified FALSE: `bh role --harness codex` is REJECTED, so such a host passed this gate and
    then failed at the seat launch — strictly worse than failing here. The point of the reversal
    is that a codex-only host now either fails `--check` or launches a seat, never both.
    """
    reports = [
        _report("gh", authenticated=True),
        _report("claude", authenticated=False),
        _report("codex", authenticated=True),
    ]
    failures = credentials.unmet(reports, cfg={})
    assert any("no seat can run" in f for f in failures)
    assert any("claude" in f for f in failures)


def test_the_configured_harness_is_the_one_that_must_be_authenticated():
    """The selector model: `harness` in config picks the row, and THAT row is the requirement.
    There is no OR left to satisfy from the side."""
    reports = [_report("gh", authenticated=True), _report("claude", authenticated=True)]
    assert credentials.unmet(reports, cfg={}) == []


def test_an_opencode_host_is_not_asked_for_a_credential_bh_cannot_probe():
    """opencode runs a seat and bh can neither install nor authenticate it — no `auth` column, so
    nothing about it is a stage-2 requirement. Falls out of the table with no special case."""
    reports = [_report("gh", authenticated=True), _report("claude", authenticated=False)]
    assert credentials.unmet(reports, cfg={"harness": "opencode"}) == []


def test_no_harness_at_all_is_a_failure():
    reports = [
        _report("gh", authenticated=True),
        _report("claude", authenticated=False),
        _report("codex", authenticated=False),
    ]
    assert any("no seat can run" in f for f in credentials.unmet(reports, cfg={}))


def test_gh_is_required_unconditionally():
    """Without gh the host clones nothing, and a host that cannot clone cannot be onboarded."""
    reports = [
        _report("gh", authenticated=False),
        _report("claude", authenticated=True),
        _report("codex", authenticated=True),
    ]
    assert any("cannot clone" in f for f in credentials.unmet(reports, cfg={}))


def test_a_failure_names_where_the_choice_was_made():
    """ "claude must be authenticated" is a config selection, not a law — name the key that picked
    it, so the operator can change the answer rather than only the credential."""
    reports = [_report("gh", authenticated=True), _report("claude", authenticated=False)]
    assert any("`harness` selects it" in f for f in credentials.unmet(reports, cfg={}))


def test_a_row_that_is_absent_is_reported_as_missing_not_unauthenticated():
    reports = [
        _report("gh", authenticated=True),
        _report("claude", authenticated=False, installed=False),
    ]
    assert any("claude is missing" in f for f in credentials.unmet(reports, cfg={}))


# ---- driving a login flow -----------------------------------------------------------------


def test_gh_login_reprobes_rather_than_trusting_the_flow(monkeypatch):
    """A login flow that reports its own success lies when the credential did not land."""
    calls = []
    monkeypatch.setattr(credentials, "run", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(credentials, "probe", lambda dep: _report(dep.name, authenticated=True))
    result = credentials.run_login("gh")
    assert calls == [list(deps.by_name("gh").auth.login)]
    assert result.authenticated


def test_login_is_a_no_op_for_rows_with_no_headless_flow(monkeypatch):
    """`claude setup-token` must run where a browser IS; running it here would hang on a prompt,
    so the honest action is the remedy text, not a pretend login. Which flow is drivable is a
    fact about the ROW (`Auth.headless_login`), not a name this module special-cases."""
    monkeypatch.setattr(
        credentials, "run", lambda *a, **k: pytest.fail("must not shell out for claude")
    )
    monkeypatch.setattr(credentials.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(credentials, "_keychain_credential", lambda _s: False)
    report = credentials.run_login("claude")
    assert not report.authenticated
    assert "setup-token" in report.remedy


# ---- the report reads as infrastructure, not as harnesses ----------------------------------


def test_the_report_never_calls_gh_a_harness(monkeypatch):
    """gh is infrastructure that needs a credential. The old header said "Harness credentials:"
    over a block whose FIRST row was gh — the category error the whole epic exists to remove."""
    _no_binaries(monkeypatch)
    rendered = "\n".join(credentials.render(credentials.probe_all()))
    assert "harness" not in rendered.lower()

    gh = deps.by_name("gh")
    assert gh.kind == "infra"
    assert gh.runs_seats is False
    assert gh.install is None
