"""`bh hq init --create` (bh-aee3) — make the HQ repo private and empty when it is missing.

`_wire_remote` refuses a remote it cannot reach, which sends the operator off to create an
empty repo by hand and re-run. `--create` closes that loop, but only for the ONE failure it
can legitimately answer — a 404. The guards under test:

- a 404 + `--create`            -> `gh repo create <remote> --private`, then wiring continues
- a 404, interactive, no flag   -> offer it; "no" falls through to the existing hard failure
- a 404 under `--auto`          -> never creates unless `--create` is explicit
- auth/network failure          -> never creates, whatever the flags say
- creation                      -> no `--source`, no `--push`: the repo must land EMPTY
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import hq


class _Stdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _probe(returncode: int, stderr: str = "", stdout: str = ""):
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)


NOT_FOUND = _probe(128, stderr="ERROR: Repository not found.\nfatal: Could not read from remote")
DENIED = _probe(128, stderr="git@github.com: Permission denied (publickey).")
OFFLINE = _probe(128, stderr="ssh: Could not resolve hostname github.com")
EMPTY_OK = _probe(0)


# ---- _remote_missing: only a 404 is answerable by creating ---------------------


def test_repository_not_found_is_a_missing_remote():
    assert hq._remote_missing(NOT_FOUND)


def test_permission_denied_is_not_a_missing_remote():
    """An auth failure must keep failing loudly — creating a repo is the wrong repair."""
    assert not hq._remote_missing(DENIED)


def test_unresolvable_host_is_not_a_missing_remote():
    assert not hq._remote_missing(OFFLINE)


# ---- _should_create: the flag / prompt / --auto split -------------------------


def test_explicit_create_flag_creates_without_asking(monkeypatch):
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(hq.typer, "confirm", _boom)
    assert hq._should_create("acme/hq", auto=False, create=True) is True


def test_auto_alone_never_creates(monkeypatch):
    """A headless run that invents repositories on a typo'd name is worse than one that fails."""
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(hq.typer, "confirm", _boom)
    assert hq._should_create("acme/hq", auto=True, create=False) is False


def test_auto_with_explicit_create_still_creates(monkeypatch):
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(hq.typer, "confirm", _boom)
    assert hq._should_create("acme/hq", auto=True, create=True) is True


def test_non_tty_without_the_flag_never_creates(monkeypatch):
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=False))
    monkeypatch.setattr(hq.typer, "confirm", _boom)
    assert hq._should_create("acme/hq", auto=False, create=False) is False


def test_interactive_offer_accepted(monkeypatch):
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(hq.typer, "confirm", lambda *a, **k: True)
    assert hq._should_create("acme/hq", auto=False, create=False) is True


def test_interactive_offer_declined(monkeypatch):
    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(hq.typer, "confirm", lambda *a, **k: False)
    assert hq._should_create("acme/hq", auto=False, create=False) is False


def test_interactive_offer_defaults_to_no(monkeypatch):
    """Bare <enter> must not create a repo."""
    seen: dict = {}

    def fake_confirm(text, default=None):
        seen["default"] = default
        return default

    monkeypatch.setattr(hq.sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(hq.typer, "confirm", fake_confirm)

    assert hq._should_create("acme/hq", auto=False, create=False) is False
    assert seen["default"] is False


def _boom(*a, **k):
    raise AssertionError("prompted when it must not")


# ---- _create_repo: private, and EMPTY -----------------------------------------


@pytest.fixture
def recorded(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(hq, "run", fake_run)
    return calls


def test_create_repo_asks_for_private(recorded):
    assert hq._create_repo("acme/beadhive-hq", dry_run=False) is True
    assert recorded == [["gh", "repo", "create", "acme/beadhive-hq", "--private"]]


def test_create_repo_never_seeds_the_repo(recorded):
    """--source/--push would push `main` without refs/dolt/data, trip _wire_remote's
    already-has-content refusal, and pre-add `origin` so wiring silently no-ops."""
    hq._create_repo("acme/beadhive-hq", dry_run=False)
    flags = recorded[0]
    assert "--source" not in flags
    assert "--push" not in flags
    assert "--add-readme" not in flags


def test_create_repo_reports_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="name already exists")

    monkeypatch.setattr(hq, "run", fake_run)
    assert hq._create_repo("acme/beadhive-hq", dry_run=False) is False


def test_dry_run_creates_nothing(recorded):
    assert hq._create_repo("acme/beadhive-hq", dry_run=True) is False
    assert recorded == []
