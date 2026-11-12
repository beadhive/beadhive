"""The ``git`` reader: a tiny real repository, golden artifacts, two git calls, shallow refusal."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from beadlog_sources import (
    GitReader,
    ReadRequest,
    SourceReader,
    build_corpus,
    dumps_evidence,
    parse_conventional,
    parse_trailers,
    read_git,
)
from beadlog_sources.commits import ShallowCloneError
from beadlog_sources.evidence import Category, ReaderHeader
from beadlog_sources.git_dates import GitReadError
from beadlog_sources.runner import Runner, subprocess_runner

GOLDEN = Path(__file__).parent / "golden" / "git_reader.golden"


def git(root: Path, *args: str, when: str = "2026-03-01T10:00:00+02:00") -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_AUTHOR_EMAIL": "ada@example.invalid",
        "GIT_COMMITTER_NAME": "Ada",
        "GIT_COMMITTER_EMAIL": "ada@example.invalid",
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout.strip()


def commit(root: Path, message: str, day: int, **files: str) -> str:
    for name, text in files.items():
        path = root / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message, when=f"2026-03-{day:02d}T10:00:00+02:00")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def full_repo(tmp_path: Path) -> Path:
    root = tmp_path / "full"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    commit(root, "chore: start", 1, README="hello\n" * 30)
    git(root, "tag", "v0.1.0")
    commit(
        root,
        "feat(parser)!: drop the old syntax (#12)\n\nSee docs/design/x.md and bh-4o07n.\n\n"
        "Closes: bh-abc12\nRefs: #7\nBREAKING CHANGE: syntax gone",
        2,
        src__a_py="print(1)\n",
    )
    git(root, "checkout", "-q", "-b", "side")
    commit(root, "fix: side fix, reverts a1b2c3d", 3, side="s\n")
    git(root, "checkout", "-q", "main")
    git(root, "mv", "README", "README.md")
    commit(root, "docs: rename readme", 4)
    git(
        root,
        "merge",
        "-q",
        "--no-ff",
        "side",
        "-m",
        "Merge pull request #5 from x/side",
        when="2026-03-05T10:00:00+02:00",
    )
    git(
        root,
        "tag",
        "-a",
        "v0.2.0",
        "-m",
        "Release 0.2.0\n\nShips bh-4o07n",
        when="2026-03-06T10:00:00+02:00",
    )
    return root


class Counting:
    def __init__(self) -> None:
        self.calls: list[Sequence[str]] = []
        self._run: Runner = subprocess_runner(60, GitReadError, "git")

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(list(argv))
        return self._run(argv)


def lines(root: Path, **options: object) -> list[str]:
    reader = GitReader()
    artifacts = list(reader.read(ReadRequest(root=root, options=options)))
    corpus = build_corpus(artifacts, [ReaderHeader(reader.name, reader.version, dict(options))])
    return dumps_evidence(corpus).decode().splitlines()


def test_is_a_reader() -> None:
    assert isinstance(GitReader(), SourceReader)
    assert GitReader.categories == (Category.CHANGE, Category.RELEASE_MARKER)


def test_matches_golden(full_repo: Path) -> None:
    # Normalize the shas (they depend on the machine-independent but long dates) away by golden
    # comparison on structure: replace each sha by its commit subject index.
    out = "\n".join(lines(full_repo)) + "\n"
    shas = git(full_repo, "log", "--format=%H", "--reverse").splitlines()
    # artifact ids hash the shas, so compare the shape instead of raw ids
    records = [json.loads(line) for line in out.splitlines()]
    shape = []
    for record in records:
        text = json.dumps({k: v for k, v in record.items() if k not in ("id",)}, sort_keys=True)
        for index, sha in enumerate(shas):
            text = text.replace(sha, f"<c{index}>")
        shape.append(text)
    expected = GOLDEN.read_text().splitlines()
    assert shape == expected


def test_commit_attrs_and_refs(full_repo: Path) -> None:
    artifacts = {a.key: a for a in GitReader().read(ReadRequest(root=full_repo))}
    by_title = {a.title: a for a in artifacts.values() if a.key.startswith("git:commit/")}
    feat = by_title["feat(parser)!: drop the old syntax (#12)"]
    assert feat.kind == "commit" and feat.provenance == "observed"
    assert feat.attrs["cc_type"] == "feat" and feat.attrs["cc_scope"] == "parser"
    assert feat.attrs["breaking"] is True
    assert feat.attrs["author"] == "Ada <ada@example.invalid>"
    assert feat.attrs["committed_at"] == "2026-03-02T08:00:00Z"
    assert list(feat.attrs["paths"]) == ["src/a_py"]
    assert {k: list(v) for k, v in feat.attrs["trailers"].items()} == {
        "Closes": ["bh-abc12"],
        "Refs": ["#7"],
        "BREAKING CHANGE": ["syntax gone"],
    }
    got = {(r.kind, r.raw, r.where) for r in feat.refs}
    assert ("pr", "#12", "subject") in got
    assert ("bead-token", "bh-4o07n", "body") in got
    assert ("path", "docs/design/x.md", "body") in got
    assert ("bead-token", "bh-abc12", "trailer") in got
    assert ("issue", "#7", "trailer") in got
    rename = by_title["docs: rename readme"]
    assert list(rename.attrs["paths"]) == ["README", "README.md"]
    assert rename.attrs["cc_type"] == "docs" and rename.attrs["cc_scope"] is None
    merge = by_title["Merge pull request #5 from x/side"]
    assert merge.kind == "merge" and len(merge.attrs["parents"]) == 2
    assert merge.attrs["cc_type"] is None
    assert ("pr", "#5", "subject") in {(r.kind, r.raw, r.where) for r in merge.refs}
    side = by_title["fix: side fix, reverts a1b2c3d"]
    assert ("sha", "a1b2c3d", "subject") in {(r.kind, r.raw, r.where) for r in side.refs}


def test_tags(full_repo: Path) -> None:
    artifacts = {a.key: a for a in GitReader().read(ReadRequest(root=full_repo))}
    light, annotated = artifacts["git:tag/v0.1.0"], artifacts["git:tag/v0.2.0"]
    assert light.category is Category.RELEASE_MARKER and light.kind == "tag"
    assert light.attrs["commit_range"] is None and light.attrs["version"] == "v0.1.0"
    assert annotated.kind == "release"
    assert annotated.attrs["commit_range"] == "v0.1.0..v0.2.0"
    assert annotated.attrs["message"].startswith("Release 0.2.0")
    assert ("bead-token", "bh-4o07n", "body") in {(r.kind, r.raw, r.where) for r in annotated.refs}
    assert annotated.occurred_at == "2026-03-06T08:00:00Z"
    assert annotated.occurred_at is not None and annotated.occurred_at.endswith("Z")


def test_exactly_two_git_calls(full_repo: Path) -> None:
    counting = Counting()
    list(GitReader(counting).read(ReadRequest(root=full_repo)))
    assert len(counting.calls) == 2
    assert [argv[5] for argv in counting.calls] == ["log", "for-each-ref"]
    for index in range(40):  # history size does not change the call count
        commit(full_repo, f"chore: bulk {index}", 6, bulk=str(index))
    more = Counting()
    artifacts = list(GitReader(more).read(ReadRequest(root=full_repo)))
    assert len(more.calls) == 2 and len(artifacts) > 40


def test_reread_is_byte_identical(full_repo: Path) -> None:
    assert lines(full_repo) == lines(full_repo)


def test_cursor_and_since(full_repo: Path) -> None:
    first = read_git(ReadRequest(root=full_repo))
    assert first.cursor == git(full_repo, "rev-parse", "HEAD")
    nothing = read_git(ReadRequest(root=full_repo, cursor=first.cursor))
    assert [a for a in nothing.artifacts if a.category is Category.CHANGE] == []
    assert nothing.cursor == first.cursor
    new = commit(full_repo, "feat: later", 9, later="1")
    more = read_git(ReadRequest(root=full_repo, cursor=first.cursor))
    assert [a.attrs["sha"] for a in more.artifacts if a.category is Category.CHANGE] == [new]
    assert more.cursor == new
    start = git(full_repo, "rev-list", "--max-parents=0", "HEAD")
    since = read_git(ReadRequest(root=full_repo, options={"since": start}))
    assert all(a.attrs.get("sha") != start for a in since.artifacts)
    dated = read_git(ReadRequest(root=full_repo, options={"since": "2026-03-08"}))
    assert [a.title for a in dated.artifacts if a.category is Category.CHANGE] == ["feat: later"]


def test_shallow_clone_refused(full_repo: Path, tmp_path: Path) -> None:
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{full_repo}", str(clone)], check=True
    )
    with pytest.raises(ShallowCloneError, match="shallow clone"):
        GitReader().read(ReadRequest(root=clone))
    allowed = GitReader().read(ReadRequest(root=clone, options={"allow_shallow": "true"}))
    assert any(a.category is Category.CHANGE for a in allowed)
    assert not (full_repo / ".git" / "shallow").exists()


def test_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(GitReadError):
        GitReader().read(ReadRequest(root=tmp_path))


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("feat: x", ("feat", None, False)),
        ("Fix(Core/IO)!: y", ("fix", "Core/IO", True)),
        ("feat(): z", ("feat", None, False)),
        ("Merge branch main", None),
        ("feat:no space", None),
        ("just words: here", None),
    ],
)
def test_parse_conventional(subject: str, expected: tuple[str, str | None, bool] | None) -> None:
    parsed = parse_conventional(subject)
    assert (None if parsed is None else (parsed.type, parsed.scope, parsed.breaking)) == expected


def test_parse_trailers() -> None:
    trailers, body = parse_trailers("text\n\nA: 1\nA: 2\nB-c: long\n  more\n")
    assert trailers == {"A": ["1", "2"], "B-c": ["long more"]} and body == ["text"]
    assert parse_trailers("only a paragraph: with colon\n")[0] == {}
    assert parse_trailers("text\n\nnot: ok\nplain\n")[0] == {}


def test_namespaces_anchor_bead_tokens(full_repo: Path) -> None:
    assert GitReader.header_options({})["token_mode"] == "generic"
    assert GitReader.header_options({"namespaces": "bh, x"})["token_mode"] == "namespaces"
    got = read_git(ReadRequest(root=full_repo, options={"namespaces": "bh"}))
    tokens = {r.raw for a in got.artifacts for r in a.refs if r.kind == "bead-token"}
    assert tokens == {"bh-4o07n", "bh-abc12"}
    other = read_git(ReadRequest(root=full_repo, options={"namespaces": "zz"}))
    assert not {r for a in other.artifacts for r in a.refs if r.kind == "bead-token"}
