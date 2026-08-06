"""`run.child_env` — the ONE launcher that CONSTRUCTS the environment bh hands a child (bh-9qor).

The measured failure, on beadhive-factory 2026-08-05: `host_provision` ran
`git workspace update` with no `env=`, `GIT_WORKSPACE` was unset in the invoking shell, and one
variable bh already resolves (`identity.workspace_root`) skipped three of ten provisioning steps
— then `bead sync` had no clones and `adopt` died on a missing directory. The same shape, one
variable over: every provider block in HQ's workspace.toml declares `env_var = "GITHUB_TOKEN"`,
that was unset too, and `bh harness auth --check` had just reported gh authenticated.

What the tests below pin, in the order the acceptance criteria state them:
  * ONE launcher, and `role.harness_env` is a CALLER of it, not a second implementation;
  * the beadhive-factory sequence: GIT_WORKSPACE unset in the shell, resolved in the child;
  * a token derived fresh from `gh auth token`, with nothing written to disk;
  * PRECEDENCE, per variable — an operator-set value WINS and is never rewritten;
  * gh present-but-unauthenticated degrades with a named remedy, no crash and no empty token;
  * SECRET HYGIENE — a planted token reaches no log, no error message, no captured output.
"""

from __future__ import annotations

import os

import pytest

from beadhive import credentials, deps, role
from beadhive import run as run_mod

PLANTED = "ghp_PLANTED_SECRET_VALUE_do_not_leak"


class _Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def gh_authenticated(monkeypatch):
    """A host where gh is installed and authenticated and `gh auth token` answers — the
    beadhive-factory state (`bh harness auth --check` reported `✓ gh authenticated`)."""
    monkeypatch.setattr(deps, "present", lambda _dep: True)
    monkeypatch.setattr(
        credentials,
        "probe",
        lambda dep: credentials.AuthReport(
            name=dep.name,
            installed=True,
            authenticated=True,
            how="stored login (`gh auth login`)",
            remedy="",
            path="/usr/bin/gh",
        ),
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return _Res(0, PLANTED + "\n")

    monkeypatch.setattr(run_mod, "run", fake_run)
    return calls


# ---- GIT_WORKSPACE: the variable that skipped three steps -----------------------------


def test_git_workspace_is_resolved_for_the_child_when_the_shell_has_none(monkeypatch, tmp_path):
    """THE beadhive-factory failure. `GIT_WORKSPACE` unset in the invoking shell; bh resolves it
    via workspace_root() and the child sees it."""
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    monkeypatch.setattr(run_mod, "_fill_github_token", lambda _env: None)
    monkeypatch.setenv("HOME", str(tmp_path))

    env = run_mod.child_env()

    assert env["GIT_WORKSPACE"] == str((tmp_path / "workspace").resolve())


def test_an_operator_set_git_workspace_wins(monkeypatch, tmp_path):
    """PRECEDENCE. The launcher fills gaps; it never overrides intent."""
    mine = tmp_path / "somewhere-else"
    mine.mkdir()
    monkeypatch.setenv("GIT_WORKSPACE", str(mine))

    env = run_mod.child_env()

    assert env["GIT_WORKSPACE"] == str(mine)


def test_a_blank_git_workspace_is_treated_as_unset(monkeypatch, tmp_path):
    """`GIT_WORKSPACE=` is an empty shell variable, not an operator asking for the empty path."""
    monkeypatch.setenv("GIT_WORKSPACE", "")
    monkeypatch.setenv("HOME", str(tmp_path))

    env = run_mod.child_env()

    assert env["GIT_WORKSPACE"] == str((tmp_path / "workspace").resolve())


def test_an_explicit_base_env_is_gap_filled_not_bypassed(monkeypatch, tmp_path):
    """An explicit `env=` is the BASE the launcher fills, not a way around it — otherwise every
    caller that already builds an env (hub._bd_ni_env) silently opts out of the guarantee."""
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    env = run_mod.child_env({"BD_NON_INTERACTIVE": "1"})

    assert env["BD_NON_INTERACTIVE"] == "1"
    assert env["GIT_WORKSPACE"] == str((tmp_path / "workspace").resolve())


def test_child_env_reads_the_environment_fresh_on_every_call(monkeypatch, tmp_path):
    """Never a module-level snapshot — the hazard `hub._bd_ni_env` documents."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    monkeypatch.setenv("GIT_WORKSPACE", str(first))
    assert run_mod.child_env()["GIT_WORKSPACE"] == str(first)

    monkeypatch.setenv("GIT_WORKSPACE", str(second))

    assert run_mod.child_env()["GIT_WORKSPACE"] == str(second)


# ---- GITHUB_TOKEN: derived fresh, never persisted -------------------------------------


def test_github_token_is_derived_from_gh_when_no_token_is_set(monkeypatch, gh_authenticated):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    env = run_mod.child_env(github_token=True)

    assert env["GITHUB_TOKEN"] == PLANTED
    assert ["gh", "auth", "token"] in gh_authenticated


def test_github_token_is_not_derived_unless_asked(monkeypatch, gh_authenticated):
    """A secret belongs in the environment of the call that needs it, not of every `git`/`bd`
    subprocess bh makes."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    env = run_mod.child_env()

    assert "GITHUB_TOKEN" not in env
    assert gh_authenticated == []


def test_an_operator_set_github_token_wins(monkeypatch, gh_authenticated):
    """PRECEDENCE. Deriving over a deliberately-set token would make bh silently ignore it."""
    monkeypatch.setenv("GITHUB_TOKEN", "operator-set")

    env = run_mod.child_env(github_token=True)

    assert env["GITHUB_TOKEN"] == "operator-set"
    assert gh_authenticated == []  # gh was never consulted


def test_an_operator_set_gh_token_wins_and_is_mirrored(monkeypatch, gh_authenticated):
    """gh's canonical variable is GH_TOKEN; git-workspace's providers declare GITHUB_TOKEN. The
    operator's value is mirrored under the name the child reads — never replaced by a derived
    one."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "operator-set")

    env = run_mod.child_env(github_token=True)

    assert env["GITHUB_TOKEN"] == "operator-set"
    assert gh_authenticated == []


def test_gh_present_but_unauthenticated_degrades_with_a_remedy(monkeypatch, capsys):
    """No crash, and no empty token producing a confusing 404 downstream."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(deps, "present", lambda _dep: True)
    monkeypatch.setattr(
        credentials,
        "probe",
        lambda dep: credentials.AuthReport(
            name=dep.name,
            installed=True,
            authenticated=False,
            how="—",
            remedy=dep.auth.remedy,
            path="/usr/bin/gh",
        ),
    )
    monkeypatch.setattr(
        run_mod, "run", lambda *a, **k: pytest.fail("must not run `gh auth token` unauthenticated")
    )

    env = run_mod.child_env(github_token=True)

    assert "GITHUB_TOKEN" not in env  # absent, not empty
    assert "gh auth login" in capsys.readouterr().err  # the remedy is NAMED, not swallowed


def test_gh_absent_degrades_without_deriving(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(deps, "present", lambda _dep: False)
    monkeypatch.setattr(
        credentials,
        "probe",
        lambda dep: credentials.AuthReport(
            name=dep.name,
            installed=False,
            authenticated=False,
            how="—",
            remedy=dep.auth.absent_remedy,
        ),
    )

    env = run_mod.child_env(github_token=True)

    assert "GITHUB_TOKEN" not in env


def test_a_failing_gh_auth_token_never_yields_an_empty_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(deps, "present", lambda _dep: True)
    monkeypatch.setattr(
        credentials,
        "probe",
        lambda dep: credentials.AuthReport(
            name=dep.name, installed=True, authenticated=True, how="stored login", remedy=""
        ),
    )
    monkeypatch.setattr(run_mod, "run", lambda *a, **k: _Res(1, "", "boom"))

    env = run_mod.child_env(github_token=True)

    assert "GITHUB_TOKEN" not in env


# ---- secret hygiene (mirrors tests/test_credentials.py's planted-token bar) ------------


def test_the_derived_token_reaches_no_log_and_no_captured_output(
    monkeypatch, gh_authenticated, capsys
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    env = run_mod.child_env(github_token=True)

    assert env["GITHUB_TOKEN"] == PLANTED  # it reached the child…
    out = capsys.readouterr()  # …and nowhere else: `log` renders to stderr
    assert PLANTED not in out.out
    assert PLANTED not in out.err


def test_no_token_is_cached_across_calls(monkeypatch, gh_authenticated):
    """Fresh per call is what makes rotation and expiry free — a cached token is a
    presence-is-not-validity bug waiting to happen."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    run_mod.child_env(github_token=True)
    run_mod.child_env(github_token=True)

    assert gh_authenticated.count(["gh", "auth", "token"]) == 2
    assert "GITHUB_TOKEN" not in os.environ  # never leaks back into bh's own process


# ---- ONE launcher: harness_env is a caller, not a parallel implementation --------------


def test_harness_env_is_a_caller_of_the_launcher(monkeypatch, tmp_path):
    """bh-og0q.2's docstring argued the GENERAL case and applied it to one call site. It is now
    the same constructed environment every other subprocess gets, plus BH_ROLE."""
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    env = role.harness_env("developer")

    assert env["BH_ROLE"] == "developer"
    assert env["GIT_WORKSPACE"] == str((tmp_path / "workspace").resolve())


def test_harness_env_carries_no_derived_token(monkeypatch, gh_authenticated):
    """The harness authenticates itself; handing it a bh-derived token would widen the secret's
    blast radius for no measured need."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    env = role.harness_env("developer")

    assert "GITHUB_TOKEN" not in env
