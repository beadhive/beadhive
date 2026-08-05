"""Runtime harness install (bh-pc2a.36, bh-hsus.1).

The image stopped baking Claude Code because its package declares "SEE LICENSE IN README.md"
rather than an SPDX identifier — baking it would make anyone publishing the image a redistributor
of proprietary software. These tests pin the two properties that make that removal safe rather
than merely compliant: the user is TOLD what they are accepting before it installs, and a missing
harness explains itself instead of surfacing as `command not found`.

bh-hsus.1: neither harness is actually installed via `npm install -g` on a real machine, and doing
so anyway built a second, PATH-shadowing copy alongside an already-present native install. These
tests also pin the fix: claude bootstraps via Anthropic's own native installer (never npm), codex
gets a remedy note instead of an attempted install (never npm), and idempotence no longer has a
`--version` escape hatch that can build that second copy.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from beadhive import harness as harness_mod
from beadhive import role as role_mod

runner = CliRunner()


def test_claude_is_marked_proprietary_and_codex_is_not():
    """The distinction the whole bead turns on. codex is Apache-2.0 and stays baked; stretching
    the rule to cover it would cost users a working default for no licence benefit."""
    assert harness_mod.HARNESSES["claude"].install.proprietary is True
    assert harness_mod.HARNESSES["codex"].install.proprietary is False
    assert harness_mod.HARNESSES["codex"].license == "Apache-2.0"


def test_claude_has_no_npm_package_anywhere_in_its_route():
    """bh-hsus.1's core claim: npm was never how a real machine gets this binary."""
    cmd = harness_mod.HARNESSES["claude"].install.cmd
    assert cmd is not None
    assert "npm" not in cmd
    assert "claude.ai/install.sh" in " ".join(cmd)


def test_codex_has_no_bh_driven_install_and_its_note_names_the_real_routes():
    """codex has no single cross-platform command bh can run — brew is macOS-only, the Linux and
    Nix routes differ, and this module must not build a plane resolver to pick between them."""
    route = harness_mod.HARNESSES["codex"].install
    assert route.cmd is None
    assert "brew" in route.note
    assert "release" in route.note or "github.com/openai/codex" in route.note
    assert "nix" in route.note.lower()


def test_missing_hint_names_the_install_verb():
    """A bare `claude: command not found` is true and points nowhere — the bh-pc2a.33 failure."""
    hint = harness_mod.missing_hint("claude")

    assert "bh harness install claude" in hint
    assert "proprietary" in hint
    # True on a host as well as in the image — on a host it is simply not installed, and
    # asserting the image does not ship it would be a confident falsehood there.
    assert "not installed" in hint


def test_missing_hint_for_a_non_proprietary_harness_omits_the_licence_warning():
    hint = harness_mod.missing_hint("codex")

    assert "proprietary" not in hint


def test_missing_hint_for_a_harness_bh_does_not_install_never_names_a_refusing_command():
    """bh-hsus.1 review: codex's ``install.cmd`` is None, so ``bh harness install codex`` always
    exits 1 (see ``test_install_of_codex_names_the_remedy_and_never_npm_installs`` below). Routing
    the missing-harness hint at that command sends the operator straight into the refusal it just
    described — the bh-pc2a.33 failure mode reproduced one hop later, this time BY the function
    that exists to prevent it. The hint must surface the real remedy (``install.note``) instead."""
    assert harness_mod.HARNESSES["codex"].install.cmd is None  # the precondition this pins

    hint = harness_mod.missing_hint("codex")

    assert "bh harness install codex" not in hint
    assert "brew" in hint
    assert "nix" in hint.lower()


def test_missing_hint_rejects_an_unknown_name():
    assert "unknown harness" in harness_mod.missing_hint("nope")


def test_install_refuses_an_unknown_harness():
    with pytest.raises(typer.Exit):
        harness_mod.install("nope")


def test_install_is_idempotent_when_already_present(monkeypatch, capsys):
    """Re-installing would silently move a pinned version, so a present harness is left alone."""
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: "/usr/local/bin/codex")
    called = []
    monkeypatch.setattr(harness_mod, "run", lambda *a, **k: called.append(a))

    harness_mod.install("codex")

    assert not called, "must not shell out when the harness is already installed"
    assert "already installed" in capsys.readouterr().out


def test_install_is_idempotent_even_with_an_explicit_version(monkeypatch, capsys):
    """bh-hsus.1's actual bug: the old `--version` branch SKIPPED the idempotence guard, so
    passing --version against an already-present native install quietly built a second,
    PATH-shadowing copy. Idempotence must now hold unconditionally."""
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: "/home/x/.local/bin/claude")
    called = []
    monkeypatch.setattr(harness_mod, "run", lambda *a, **k: called.append(a))
    monkeypatch.setattr(harness_mod.typer, "confirm", lambda *a, **k: True)

    harness_mod.install("claude", version="2.1.220", yes=True)

    assert not called, "an explicit --version must not bypass idempotence and shadow-install"
    assert "already installed" in capsys.readouterr().out


def test_proprietary_install_names_the_licence_before_acting(monkeypatch, capsys):
    """The point of the verb is that this is the USER's choice, so it has to read as one. If the
    confirmation is declined, nothing must run."""
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: None)
    called = []
    monkeypatch.setattr(harness_mod, "run", lambda *a, **k: called.append(a))

    def _decline(*_a, **_k):
        raise typer.Abort()

    monkeypatch.setattr(harness_mod.typer, "confirm", _decline)

    with pytest.raises(typer.Abort):
        harness_mod.install("claude")

    out = capsys.readouterr().out
    assert "PROPRIETARY" in out
    assert "SEE LICENSE IN README.md" in out
    assert not called, "declining the licence prompt must not install anything"


def test_install_bootstraps_claude_via_the_native_installer_not_npm(monkeypatch):
    """Acceptance: `bh harness install claude` uses the native bootstrap, not `npm install -g`."""
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: None)
    argvs = []

    def _record(argv, **_k):
        argvs.append(argv)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(harness_mod, "run", _record)

    harness_mod.install("claude", yes=True)

    assert len(argvs) == 1
    joined = " ".join(argvs[0])
    assert "claude.ai/install.sh" in joined
    assert "npm" not in joined


def test_install_forwards_an_explicit_version_to_the_bootstrap(monkeypatch):
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: None)
    argvs = []

    def _record(argv, **_k):
        argvs.append(argv)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(harness_mod, "run", _record)

    harness_mod.install("claude", version="2.1.220", yes=True)

    assert argvs[0][-1] == "2.1.220"


def test_install_uses_bh_claude_code_version_only_on_a_genuine_bootstrap(monkeypatch):
    """BH_CLAUDE_CODE_VERSION is an explicit, opt-in bootstrap-target override (bh-hsus.1) — it is
    consulted ONLY when there is no claude on PATH yet, never to move an existing install."""
    monkeypatch.setenv("BH_CLAUDE_CODE_VERSION", "2.1.220")
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: None)
    argvs = []

    def _record(argv, **_k):
        argvs.append(argv)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(harness_mod, "run", _record)

    harness_mod.install("claude", yes=True)

    assert argvs[0][-1] == "2.1.220"


def test_install_of_codex_names_the_remedy_and_never_npm_installs(monkeypatch, capsys):
    """Acceptance: `bh harness install codex` names brew/release/nix and does not npm-install."""
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: None)
    called = []
    monkeypatch.setattr(harness_mod, "run", lambda *a, **k: called.append(a))

    with pytest.raises(typer.Exit):
        harness_mod.install("codex")

    assert not called, "bh must never shell out to install codex"
    err = capsys.readouterr().err
    assert "brew" in err
    assert "nix" in err.lower()


def test_role_launch_refuses_a_missing_harness_with_the_install_hint(monkeypatch, capsys):
    """The seam that matters most: `bh role launch` is how a user actually meets the absence."""
    monkeypatch.setattr(role_mod, "_known_seats", lambda: ["developer"])
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: None)
    ran = []
    monkeypatch.setattr(role_mod, "run", lambda *a, **k: ran.append(a))

    with pytest.raises(SystemExit) as exc:
        role_mod.launch("developer", harness="claude")

    assert exc.value.code == 1
    assert not ran, "must not exec a harness that is not installed"
    assert "bh harness install claude" in capsys.readouterr().err
