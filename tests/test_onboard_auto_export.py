"""bd auto-export defaults (bh-ug5u) — `onboard._configure_auto_export` +
`hive._ensure_export_exclude`.

Two guarantees, and the second is the one with teeth: the snapshot is always FRESH (bd exports
after write commands) and it is NEVER git-tracked. A test that only asserts the config keys were
written would pass against a hive that happily commits the file on every onboard.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from beadhive import hive, onboard


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A real git repo — `_ensure_export_exclude` probes with `git check-ignore`, so a fake
    directory would not exercise the branch that matters.

    `core.excludesFile` is neutralized deliberately. A developer whose personal
    `~/.config/git/ignore` already carries `.beads/*` (ours does) would otherwise see these
    tests pass against their dotfiles rather than against bh — and would ship a hive that leaks
    the snapshot on every OTHER machine. Same class of bug as bh-myp0."""
    _git("init", "-q", ".", cwd=tmp_path)
    _git("config", "core.excludesFile", "/dev/null", cwd=tmp_path)
    (tmp_path / ".beads").mkdir()
    return tmp_path


def _ctx(base, *, furnish: bool):
    return types.SimpleNamespace(base=base, cwd=str(base), furnish=furnish)


# ---- config keys --------------------------------------------------------------


def test_turns_auto_export_on_and_pins_git_add_off(repo, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(hive, "run", fake_run)
    onboard._configure_auto_export(_ctx(repo, furnish=False))

    written = {c[3]: c[4] for c in calls if c[:3] == ["bd", "config", "set"]}
    assert written == {"export.auto": "true", "export.git-add": "false"}


def test_interval_is_left_at_bds_default(repo, monkeypatch):
    """A full export measured ~2.6s on a 1.5k-issue hive, so a shorter window would spend a
    large fraction of wall-clock re-dumping. Writing no interval is the deliberate choice —
    assert it, so nobody 'helpfully' pins 5s later without revisiting the cost."""
    calls = []
    monkeypatch.setattr(
        hive, "run", lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0))[1]
    )
    onboard._configure_auto_export(_ctx(repo, furnish=False))

    assert not any("export.interval" in c for c in calls)


def test_a_bd_that_rejects_the_key_does_not_fail_onboarding(repo, monkeypatch):
    """Auto-export is an interop nicety. An older bd must still produce a working hive."""
    monkeypatch.setattr(hive, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "x"))

    onboard._configure_auto_export(_ctx(repo, furnish=True))  # must not raise


# ---- never tracked ------------------------------------------------------------


def test_furnished_hive_excludes_the_snapshot_from_git(repo):
    assert hive._ensure_export_exclude(repo) is True

    probe = subprocess.run(
        ["git", "check-ignore", "-q", ".beads/issues.jsonl"], cwd=str(repo), check=False
    )
    assert probe.returncode == 0  # git now ignores it


def test_exclude_is_idempotent(repo):
    hive._ensure_export_exclude(repo)
    before = (repo / ".git/info/exclude").read_text()

    assert hive._ensure_export_exclude(repo) is False  # already ignored — no second write
    assert (repo / ".git/info/exclude").read_text() == before


def test_exclude_preserves_existing_entries(repo):
    exclude = repo / ".git/info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# mine\nscratch/\n")

    hive._ensure_export_exclude(repo)

    assert "scratch/" in exclude.read_text()


def test_the_exported_file_cannot_be_staged_after_exclusion(repo):
    """The guarantee operators actually care about: `git add` must not pick it up. Asserting on
    .git/info/exclude's TEXT would pass even if the pattern were wrong."""
    hive._ensure_export_exclude(repo)
    (repo / ".beads" / "issues.jsonl").write_text('{"id": "x-1"}\n')

    _git("add", "-A", cwd=repo)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=str(repo), capture_output=True, text=True
    ).stdout

    assert "issues.jsonl" not in staged


def test_zero_footprint_hive_needs_no_separate_exclude(repo, monkeypatch):
    """Zero-footprint already excludes all of `.beads/`, so `_configure_auto_export` must not
    write a redundant entry — the furnished branch is the only one that needs it."""
    monkeypatch.setattr(hive, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    called = []
    monkeypatch.setattr(hive, "_ensure_export_exclude", lambda base: called.append(base))

    onboard._configure_auto_export(_ctx(repo, furnish=False))

    assert called == []
