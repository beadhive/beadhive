"""``GitRepoFacts``: the intake ``RepoFacts`` port over a real, temporary git repository (bh-xhww6).

Pinned: ancestry is ``YES`` / ``NO`` from exit 0 / 1 only and ``UNKNOWN`` otherwise; the first tag
containing a commit is version-sorted (``v0.9.0`` before ``v0.10.0``, where a plain sort says the
opposite); changed paths are what the branch changed since it diverged, with both names of a
rename; and a value that is not a plain sha or ref never reaches git. Nothing is written to the
repository by the adapter. No network, no clock.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from beadhive.adapters.intake import GitRepoFacts
from beadhive.modules.intake.contracts import RepoFacts, Verdict
from beadhive.testing.intake import FakeRepoFacts

_IDENTITY = (
    "-c",
    "user.name=Repo Facts",
    "-c",
    "user.email=facts@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *_IDENTITY, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> dict[str, str | Path]:
    """``main``: c1 (tag v0.9.0) -> c2 (tag v0.10.0) -> c3 (untagged, renames a doc); a side branch
    ``feature`` off c1 with s1."""
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    c1 = _commit(repo, "c1", {"src/app.py": "one\n", "docs/guide.md": "guide\n"})
    _git(repo, "tag", "v0.9.0")
    c2 = _commit(repo, "c2", {"src/app.py": "two\n"})
    _git(repo, "tag", "v0.10.0")
    _git(repo, "mv", "docs/guide.md", "docs/manual.md")
    c3 = _commit(repo, "c3", {"src/lib/util.py": "util\n"})
    _git(repo, "checkout", "-q", "-b", "feature", c1)
    s1 = _commit(repo, "s1", {"src/side.py": "side\n"})
    _git(repo, "checkout", "-q", "main")
    return {"repo": repo, "c1": c1, "c2": c2, "c3": c3, "s1": s1}


def _recording(repo: Path) -> tuple[GitRepoFacts, list[list[str]]]:
    asked: list[list[str]] = []

    def run(args: Sequence[str]) -> tuple[int, str]:
        asked.append(list(args))
        done = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
        return done.returncode, done.stdout

    return GitRepoFacts(repo, run=run), asked


def test_it_satisfies_the_port_as_does_the_kit_fake(world) -> None:
    assert isinstance(GitRepoFacts(world["repo"]), RepoFacts)
    assert isinstance(FakeRepoFacts(), RepoFacts)


def test_ancestry_is_yes_no_or_unknown(world) -> None:
    facts = GitRepoFacts(world["repo"])
    assert facts.is_ancestor(world["c1"], "main") is Verdict.YES
    assert facts.is_ancestor(world["c3"][:12], "main") is Verdict.YES  # an abbreviated sha
    assert facts.is_ancestor(world["s1"], "main") is Verdict.NO
    assert facts.is_ancestor(world["c1"], "feature") is Verdict.YES
    assert facts.is_ancestor("0123456789abcdef", "main") is Verdict.UNKNOWN  # no such object
    assert facts.is_ancestor(world["c1"], "no-such-branch") is Verdict.UNKNOWN


def test_the_first_tag_containing_a_commit_is_version_sorted(world) -> None:
    facts = GitRepoFacts(world["repo"])
    assert sorted(["v0.9.0", "v0.10.0"]) == ["v0.10.0", "v0.9.0"]  # a plain sort is wrong
    assert facts.first_tag_containing(world["c1"]) == "v0.9.0"
    assert facts.first_tag_containing(world["c2"]) == "v0.10.0"
    assert facts.first_tag_containing(world["c3"]) is None  # merged, not released
    assert facts.first_tag_containing(world["s1"]) is None
    assert facts.first_tag_containing("0123456789abcdef") is None  # git refuses: no answer


def test_a_tag_pattern_narrows_the_tags(world) -> None:
    facts = GitRepoFacts(world["repo"], tag_pattern="v0.1*")
    assert facts.first_tag_containing(world["c1"]) == "v0.10.0"
    with pytest.raises(ValueError, match="tag pattern"):
        GitRepoFacts(world["repo"], tag_pattern="--points-at")


def test_changed_paths_since_a_commit_include_both_names_of_a_rename(world) -> None:
    facts = GitRepoFacts(world["repo"])
    assert facts.changed_paths(world["c1"], "main") == (
        "docs/guide.md",
        "docs/manual.md",
        "src/app.py",
        "src/lib/util.py",
    )
    assert facts.changed_paths(world["c2"], "main") == (
        "docs/guide.md",
        "docs/manual.md",
        "src/lib/util.py",
    )
    assert facts.changed_paths(world["c3"], "main") == ()
    # since the side branch diverged, main changed what c2 and c3 changed, not what s1 did
    assert "src/side.py" not in (facts.changed_paths(world["s1"], "main") or ())
    assert facts.changed_paths("0123456789abcdef", "main") is None


@pytest.mark.parametrize(
    ("question", "args"),
    [
        ("is_ancestor", ("--all", "main")),
        ("is_ancestor", ("c1", "main..feature")),
        ("is_ancestor", ("c1", "-main")),
        ("first_tag_containing", ("--points-at=HEAD",)),
        ("changed_paths", ("--output=/tmp/x", "main")),
        ("changed_paths", ("c1", "main...feature")),
        ("changed_paths", ("c1", "refs/heads/main.lock")),
    ],
)
def test_a_value_that_is_not_a_plain_sha_or_ref_never_reaches_git(world, question, args) -> None:
    facts, asked = _recording(world["repo"])
    args = tuple(world.get(a, a) for a in args)
    answer = getattr(facts, question)(*args)
    assert answer in (Verdict.UNKNOWN, None) and asked == []


def test_only_exit_zero_and_one_are_meaningful_for_ancestry() -> None:
    def scripted(code: int):
        return GitRepoFacts("/nowhere", run=lambda args: (code, ""))

    assert scripted(0).is_ancestor("abcdef1", "main") is Verdict.YES
    assert scripted(1).is_ancestor("abcdef1", "main") is Verdict.NO
    for code in (128, 129, -9):
        assert scripted(code).is_ancestor("abcdef1", "main") is Verdict.UNKNOWN
        assert scripted(code).first_tag_containing("abcdef1") is None
        assert scripted(code).changed_paths("abcdef1", "main") is None


def test_the_adapter_writes_nothing(world) -> None:
    repo = world["repo"]
    before = (_git(repo, "show-ref"), _git(repo, "status", "--porcelain"))
    facts = GitRepoFacts(repo)
    facts.is_ancestor(world["c1"], "main")
    facts.first_tag_containing(world["c1"])
    facts.changed_paths(world["c1"], "main")
    assert (_git(repo, "show-ref"), _git(repo, "status", "--porcelain")) == before
