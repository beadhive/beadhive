"""`bh harness auth` — probe and guide harness credentials (bh-q160.3).

The load-bearing test here is :func:`test_no_credential_value_ever_reaches_the_report`. Every
other behaviour is a convenience; leaking a token into stdout, a log or an OTEL attribute is the
one failure that cannot be walked back once it has happened.
"""

from __future__ import annotations

import pytest

from beadhive import harness_auth


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """The developer running these tests almost certainly HAS credentials. Without clearing
    them the "unauthenticated" cases silently pass for the wrong reason."""
    for var in (
        *harness_auth._GH_TOKEN_VARS,
        *harness_auth._CLAUDE_TOKEN_VARS,
        *harness_auth._CODEX_TOKEN_VARS,
    ):
        monkeypatch.delenv(var, raising=False)


def _no_binaries(monkeypatch):
    monkeypatch.setattr(harness_auth.shutil, "which", lambda _: None)


# ---- the contract that matters ------------------------------------------------------------


def test_no_credential_value_ever_reaches_the_report(monkeypatch):
    """A probe reports that a variable is SET, never what it holds."""
    secret = "ghp_thismustneverappearanywhere"
    monkeypatch.setattr(harness_auth.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    reports = harness_auth.probe_all()
    rendered = "\n".join(harness_auth.render(reports))

    assert secret not in rendered
    for report in reports:
        assert secret not in f"{report.how}{report.detail}{report.remedy}{report.path}"
    # …and the NAME is what gets reported, because that is the useful half.
    assert "GH_TOKEN (environment)" in rendered


def test_env_source_returns_the_name_not_the_value(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret-value")
    assert harness_auth._env_source(("GH_TOKEN",)) == "GH_TOKEN"


def test_blank_env_var_is_not_a_credential(monkeypatch):
    """An exported-but-empty var is the classic half-configured host; it must not read as auth."""
    monkeypatch.setenv("GH_TOKEN", "   ")
    assert harness_auth._env_source(("GH_TOKEN",)) is None


# ---- gh -----------------------------------------------------------------------------------


def test_gh_env_token_wins_over_stored_login(monkeypatch):
    """gh resolves GH_TOKEN before any stored login, so reporting the stored one would describe
    a credential gh is not actually using."""
    monkeypatch.setattr(harness_auth.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(
        harness_auth, "_gh_auth_status", lambda: pytest.fail("must not probe when env wins")
    )
    report = harness_auth.probe_gh()
    assert report.authenticated and report.how == "GH_TOKEN (environment)"


def test_gh_reports_the_git_protocol(monkeypatch):
    """https-vs-ssh is what breaks HQ sync (bh-pc2a.30), so the report must name it."""
    monkeypatch.setattr(harness_auth.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(harness_auth, "_gh_auth_status", lambda: (True, "ssh"))
    assert "ssh" in harness_auth.probe_gh().detail


def test_gh_unauthenticated_names_the_device_flow(monkeypatch):
    """The device flow is the ONE route that works with no browser on the box."""
    monkeypatch.setattr(harness_auth.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(harness_auth, "_gh_auth_status", lambda: (False, ""))
    report = harness_auth.probe_gh()
    assert not report.authenticated
    assert "--web" in report.remedy


def test_gh_probe_survives_a_hang(monkeypatch):
    """A wedged `gh auth status` must become a finding, not a hung provision run."""

    def _timeout(*_a, **_k):
        raise harness_auth.subprocess.TimeoutExpired(cmd="gh", timeout=1)

    monkeypatch.setattr(harness_auth, "run", _timeout)
    assert harness_auth._gh_auth_status() == (False, "")


# ---- claude / codex -----------------------------------------------------------------------


def test_claude_keychain_is_consulted_on_macos(monkeypatch):
    """The first cut probed only for a file and called a working macOS install unauthenticated."""
    monkeypatch.setattr(harness_auth.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(harness_auth, "_claude_keychain_credential", lambda: True)
    report = harness_auth.probe_claude()
    assert report.authenticated
    assert "Keychain" in report.how


def test_claude_keychain_never_queries_off_darwin(monkeypatch):
    """`security` is a macOS binary; a missing-command error is not a finding worth reporting."""
    monkeypatch.setattr(harness_auth.sys, "platform", "linux")
    monkeypatch.setattr(
        harness_auth, "run", lambda *a, **k: pytest.fail("must not shell out off darwin")
    )
    assert harness_auth._claude_keychain_credential() is False


def test_missing_binaries_report_not_installed(monkeypatch):
    _no_binaries(monkeypatch)
    for report in harness_auth.probe_all():
        assert not report.installed and not report.authenticated
        assert report.remedy, "an absent target must always name its remedy"


# ---- the host-level requirement ------------------------------------------------------------


def _report(name, *, authenticated):
    return harness_auth.AuthReport(
        name=name, installed=True, authenticated=authenticated, how="x", remedy=""
    )


def test_one_harness_is_enough():
    """A seat needs ONE agent, not both — requiring both would fail a codex-only host that works."""
    reports = [
        _report("gh", authenticated=True),
        _report("claude", authenticated=False),
        _report("codex", authenticated=True),
    ]
    assert harness_auth.unmet(reports) == []


def test_no_harness_at_all_is_a_failure():
    reports = [
        _report("gh", authenticated=True),
        _report("claude", authenticated=False),
        _report("codex", authenticated=False),
    ]
    assert any("no seat can run" in f for f in harness_auth.unmet(reports))


def test_gh_is_required_unconditionally():
    """Without gh the host clones nothing, and a host that cannot clone cannot be onboarded."""
    reports = [
        _report("gh", authenticated=False),
        _report("claude", authenticated=True),
        _report("codex", authenticated=True),
    ]
    assert any("cannot clone" in f for f in harness_auth.unmet(reports))


# ---- driving a login flow -----------------------------------------------------------------


def test_gh_login_reprobes_rather_than_trusting_the_flow(monkeypatch):
    """A login flow that reports its own success lies when the credential did not land."""
    calls = []
    monkeypatch.setattr(harness_auth, "run", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(harness_auth, "probe_gh", lambda: _report("gh", authenticated=True))
    result = harness_auth.run_login("gh")
    assert calls == [harness_auth.GH_DEVICE_FLOW]
    assert result.authenticated


def test_login_is_a_no_op_for_harnesses_with_no_headless_flow(monkeypatch):
    """`claude setup-token` must run where a browser IS; running it here would hang on a prompt,
    so the honest action is the remedy text, not a pretend login."""
    monkeypatch.setattr(
        harness_auth, "run", lambda *a, **k: pytest.fail("must not shell out for claude")
    )
    monkeypatch.setattr(harness_auth.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(harness_auth, "_claude_keychain_credential", lambda: False)
    report = harness_auth.run_login("claude")
    assert not report.authenticated
    assert "setup-token" in report.remedy
