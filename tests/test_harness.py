"""Runtime harness install (bh-pc2a.36).

The image stopped baking Claude Code because its package declares "SEE LICENSE IN README.md"
rather than an SPDX identifier — baking it would make anyone publishing the image a redistributor
of proprietary software. These tests pin the two properties that make that removal safe rather
than merely compliant: the user is TOLD what they are accepting before it installs, and a missing
harness explains itself instead of surfacing as `command not found`.
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
    assert harness_mod.HARNESSES["claude"].proprietary is True
    assert harness_mod.HARNESSES["codex"].proprietary is False
    assert harness_mod.HARNESSES["codex"].license == "Apache-2.0"


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

    assert "bh harness install codex" in hint
    assert "proprietary" not in hint


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

    assert not called, "must not shell out to npm when the harness is already installed"
    assert "already installed" in capsys.readouterr().out


def test_proprietary_install_names_the_licence_before_acting(monkeypatch, capsys):
    """The point of the verb is that this is the USER's choice, so it has to read as one. If the
    confirmation is declined, npm must never run."""
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: None)
    monkeypatch.setattr(harness_mod.shutil, "which", lambda b: "/usr/bin/npm")
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


def test_install_uses_the_images_pinned_version(monkeypatch):
    """The image still names ONE validated version for what it does not ship, so `latest` cannot
    drift silently the moment the component stops being baked."""
    monkeypatch.setenv("BH_CLAUDE_CODE_VERSION", "2.1.220")
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: None)
    monkeypatch.setattr(harness_mod.shutil, "which", lambda b: "/usr/bin/npm")
    argvs = []

    def _record(argv, **_k):
        argvs.append(argv)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(harness_mod, "run", _record)

    harness_mod.install("claude", yes=True)

    assert "@anthropic-ai/claude-code@2.1.220" in argvs[0]


def test_install_without_npm_says_so(monkeypatch):
    monkeypatch.setattr(harness_mod, "installed_path", lambda spec: None)
    monkeypatch.setattr(harness_mod.shutil, "which", lambda b: None)

    with pytest.raises(typer.Exit):
        harness_mod.install("codex")


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
