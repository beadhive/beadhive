"""Commit-date lookup: many shas, one ``git`` call, unknown shas reported not fatal.

The runner is a fake returning ``git log`` output for shas recorded from this repository's own
history (dates carry the committer's offset, e.g. ``-07:00``, which must normalize to UTC).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

import pytest

from beadlog_sources import CommitDates, GitReadError, commit_dates
from beadlog_sources.git_dates import LOG_FORMAT, dates_argv, parse_dates

OFFSET = "6e4513e082603cd0501ad7ba44aa8b754361635d"  # chore(merge): bead bh-f62lu.1
UTC = "b590eb143b195b3c449d1a735f4f096a196fe087"  # chore(merge): molecule bh-ebopn
LOG = (
    f"{UTC}\t2026-10-31T13:12:07Z\t2026-10-31T13:12:07Z\n"
    f"{OFFSET}\t2026-09-15T22:57:50-07:00\t2026-09-15T22:57:50-07:00\n"
)


class FakeGit:
    """Returns the log lines of the shas on the command line it knows; counts its calls."""

    def __init__(self, known: dict[str, str] | None = None) -> None:
        self.known = (
            known if known is not None else {UTC: LOG.splitlines()[0], OFFSET: LOG.splitlines()[1]}
        )
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(list(argv))
        out = [
            line
            for sha, line in self.known.items()
            if any(
                sha.startswith(arg)
                for arg in argv[argv.index("--ignore-missing") + 1 :]
                if arg != "--"
            )
        ]
        return "".join(f"{line}\n" for line in out)


def test_author_and_committer_dates_normalize_to_utc() -> None:
    result = commit_dates([UTC, OFFSET], root="/repo", runner=FakeGit())
    assert result.dates[OFFSET].author_date == "2026-09-16T05:57:50Z"
    assert result.dates[OFFSET].committer_date == "2026-09-16T05:57:50Z"
    assert result.dates[UTC].committer_date == "2026-10-31T13:12:07Z"
    assert result.missing == () and result.invalid == ()


def test_author_and_committer_can_differ() -> None:
    line = f"{UTC}\t2026-10-30T10:00:00+02:00\t2026-10-31T13:12:07Z"
    result = commit_dates([UTC], runner=FakeGit({UTC: line}))
    date = result.dates[UTC]
    assert (date.author_date, date.committer_date) == (
        "2026-10-30T08:00:00Z",
        "2026-10-31T13:12:07Z",
    )


def test_many_shas_are_one_git_call() -> None:
    git = FakeGit()
    shas = [f"{n:040x}" for n in range(1, 1500)] + [UTC, OFFSET, UTC]  # duplicates collapse
    result = commit_dates(shas, root="/repo", runner=git)
    assert len(git.calls) == 1 and result.calls == 1
    argv = git.calls[0]
    assert argv[:3] == ["git", "-C", "/repo"]
    assert argv[3:5] == ["log", "--ignore-missing"] and "--no-walk=unsorted" in argv
    assert argv[-1] == "--" and argv.count(UTC) == 1
    assert set(result.dates) == {UTC, OFFSET}
    assert len(result.missing) == 1499


def test_unknown_shas_are_reported_not_fatal() -> None:
    ghost = "deadbeef" * 5
    result = commit_dates([UTC, ghost], runner=FakeGit())
    assert set(result.dates) == {UTC}
    assert result.missing == (ghost,)


def test_abbreviated_shas_resolve_by_prefix() -> None:
    result = commit_dates([UTC[:10]], runner=FakeGit())
    assert result.dates[UTC[:10]].sha == UTC


def test_invalid_shas_never_reach_the_command_line() -> None:
    git = FakeGit()
    result = commit_dates(["--all", "HEAD", "", UTC], runner=git)
    assert result.invalid == ("--all", "HEAD", "")
    assert "--all" not in git.calls[0] and "HEAD" not in git.calls[0]


def test_nothing_to_look_up_makes_no_call() -> None:
    git = FakeGit()
    assert commit_dates([], runner=git) == CommitDates()
    assert commit_dates(["HEAD"], runner=git).invalid == ("HEAD",)
    assert git.calls == []  # a bare `git log` would walk HEAD


def test_chunking_bounds_argv_length_only() -> None:
    git = FakeGit()
    shas = [f"{n:040x}" for n in range(1, 11)]
    assert commit_dates(shas, chunk=4, runner=git).calls == 3
    assert len(git.calls) == 3
    with pytest.raises(ValueError, match="chunk"):
        commit_dates(shas, chunk=0, runner=git)


def test_malformed_output_lines_are_counted() -> None:
    dates, bad = parse_dates(f"{LOG}garbage\n{UTC}\tnope\t2026-10-31T13:12:07Z\n\n")
    assert set(dates) == {UTC, OFFSET} and bad == 2


def test_argv_uses_the_documented_format() -> None:
    assert f"--format={LOG_FORMAT}" in dates_argv([UTC])
    assert dates_argv([UTC])[0] == "git"


def test_git_failure_is_a_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(argv: Sequence[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository\n")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(GitReadError, match="not a git repository"):
        commit_dates([UTC], root="/nowhere")


def test_real_git_reports_unknown_shas_and_normalizes_offsets(tmp_path: Any) -> None:
    """The default subprocess runner against a real repository (dates pinned, no clock)."""
    env = {
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@example.com",
        "GIT_COMMITTER_NAME": "c",
        "GIT_COMMITTER_EMAIL": "c@example.com",
        "GIT_AUTHOR_DATE": "2026-09-15T22:57:50-07:00",
        "GIT_COMMITTER_DATE": "2026-09-16T01:00:00+00:00",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": __import__("os").environ["PATH"],
    }

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(tmp_path), *args], env=env, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    git("init", "-q")
    git("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "first")
    sha = git("rev-parse", "HEAD")
    ghost = "0123456789abcdef" * 2 + "01234567"
    result = commit_dates([sha, ghost, "zz"], root=tmp_path)
    assert result.dates[sha].author_date == "2026-09-16T05:57:50Z"
    assert result.dates[sha].committer_date == "2026-09-16T01:00:00Z"
    assert result.missing == (ghost,) and result.invalid == ("zz",)
