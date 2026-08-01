"""hq._confirm_remote (bh-mw97) — the interactive-by-default owner confirmation.

Wiring HQ's remote is a one-way fleet decision (it pushes `main` + `refs/dolt/data` and fixes
which HQ the fleet answers to), so a derived owner is a SUGGESTION, never a value acted on
silently. This covers the three-way split:

- `--auto`      -> take the derived value, never prompt (CI/headless)
- non-TTY       -> same, because there is nobody to ask
- interactive   -> prompt, offering the derived value as the default
"""

from __future__ import annotations

import pytest

from beadhive import hq


class _Stdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def derived(monkeypatch):
    """Pin the derivation so these tests exercise the prompt split, not the derivation."""
    monkeypatch.setattr(hq.config, "hq_remote", lambda cfg=None, cwd=None: "briancripe/beadhive-hq")


def _no_prompt(monkeypatch):
    """Make any prompt a hard failure — proves the non-interactive paths never ask."""

    def boom(*a, **k):
        raise AssertionError("prompted when it must not")

    monkeypatch.setattr(hq.typer, "prompt", boom)


def _answer(monkeypatch, reply, seen: dict):
    def fake_prompt(text, default=None):
        seen["text"], seen["default"] = text, default
        return reply

    monkeypatch.setattr(hq.typer, "prompt", fake_prompt)


# ---- non-interactive paths ----------------------------------------------------


def test_auto_takes_the_derived_value_without_prompting(derived, monkeypatch):
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))  # a TTY is available...
    _no_prompt(monkeypatch)
    assert hq._confirm_remote({}, auto=True) == "briancripe/beadhive-hq"  # ...--auto still wins


def test_non_tty_takes_the_derived_value_without_prompting(derived, monkeypatch):
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=False))
    _no_prompt(monkeypatch)
    assert hq._confirm_remote({}, auto=False) == "briancripe/beadhive-hq"


def test_auto_with_no_derivation_returns_empty_rather_than_hanging(monkeypatch):
    monkeypatch.setattr(hq.config, "hq_remote", lambda cfg=None, cwd=None: "")
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=False))
    _no_prompt(monkeypatch)
    assert hq._confirm_remote({}, auto=False) == ""


# ---- interactive path ---------------------------------------------------------


def test_interactive_offers_the_derived_value_as_the_prompt_default(derived, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    _answer(monkeypatch, "briancripe/beadhive-hq", seen)

    assert hq._confirm_remote({}, auto=False) == "briancripe/beadhive-hq"
    assert seen["default"] == "briancripe/beadhive-hq"


def test_interactive_answer_overrides_the_derived_value(derived, monkeypatch):
    """The whole point: the operator can redirect HQ away from the guess."""
    seen: dict = {}
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    _answer(monkeypatch, "someorg/other-hq", seen)

    assert hq._confirm_remote({}, auto=False) == "someorg/other-hq"


def test_interactive_strips_whitespace_from_the_answer(derived, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    _answer(monkeypatch, "  someorg/other-hq \n", seen)

    assert hq._confirm_remote({}, auto=False) == "someorg/other-hq"


def test_interactive_with_no_derivation_prompts_with_no_default(monkeypatch):
    """Nothing to suggest — ask outright rather than returning "" and failing later."""
    seen: dict = {}
    monkeypatch.setattr(hq.config, "hq_remote", lambda cfg=None, cwd=None: "")
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    _answer(monkeypatch, "someorg/other-hq", seen)

    assert hq._confirm_remote({}, auto=False) == "someorg/other-hq"
    assert seen["default"] is None
