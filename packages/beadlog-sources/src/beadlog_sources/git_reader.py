"""The ``git`` source reader: commits, merges, tags and releases as evidence artifacts.

A pure :class:`~beadlog_sources.readers.SourceReader` (design: ``backfill-history-pipeline-adr``,
Phase A). One run is exactly **two** ``git`` processes whatever the history size: one
``git log -z --parents --name-status`` and one ``git for-each-ref refs/tags``. Nothing is
resolved here: bead-looking tokens, shas, ``#N`` numbers, paths and tags found in a subject, body
or trailer become un-interpreted :class:`~beadlog_sources.evidence.Ref` records (a bead token is
shape only, never a prefix guess; deciding what is a bead is the later collation phase).

Artifacts (provenance ``observed``):

- ``change`` keyed ``git:commit/<sha>``, kind ``commit`` or ``merge`` (more than one parent):
  ``sha``, ``parents``, ``author`` (``Name <email>``), ``committed_at`` (canonical UTC),
  ``cc_type`` / ``cc_scope`` (hand-written Conventional Commits parse, ``None`` when the subject
  is not one), ``breaking`` (``!`` or a ``BREAKING CHANGE`` trailer), ``paths`` (sorted, renames
  contribute both sides; empty for a merge, which ``git log`` does not diff) and ``trailers``
  (key -> list of values, in order).
- ``release-marker`` keyed ``git:tag/<name>``, kind ``tag`` (lightweight) or ``release``
  (annotated): ``version`` (the tag name), ``released_at`` (the tag date, else the commit date) and
  ``commit_range`` (``<previous tag>..<tag>``, tags ordered by date then name; ``None`` for the
  first).

Shallow clones are refused (their history is truncated) unless ``allow_shallow``; the check reads
the repository's ``shallow`` file, so it costs no process.

Options: ``namespaces`` (comma-separated id namespaces anchoring bead tokens; absent: the generic
shape; ``header_options`` records ``token_mode``), ``rev`` (default ``HEAD``), ``since`` (a hex
sha: only
``since..rev``; anything else is a date for ``git log --since``), ``allow_shallow`` and ``git``
(the binary). A string
:attr:`~beadlog_sources.readers.ReadRequest.cursor` is a sha read as ``cursor..rev``
(incremental); :func:`read_git` also returns the new cursor, the newest commit read (or the old
cursor when nothing is new).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._sources import commit_time, csv, truthy
from .evidence import Artifact, Category, Ref
from .git_dates import GitReadError
from .readers import ReadRequest
from .runner import DEFAULT_TIMEOUT_SECONDS, Runner, subprocess_runner
from .tokens import bead_token_pattern, find_bead_tokens, token_mode

READER_NAME = "git"
READER_VERSION = "0.1.0"
SHALLOW_REMEDIATION = (
    "run `git fetch --unshallow` to fetch the full history "
    "(or pass option allow_shallow=true to extract from the truncated history)"
)

_RS, _US = "\x1e", "\x1f"
_LOG_FORMAT = _RS + _US.join(["%H", "%P", "%an <%ae>", "%cI", "%B"])
_TAG_FORMAT = (
    _US.join(
        [
            "%(refname:lstrip=2)",
            "%(objecttype)",
            "%(objectname)",
            "%(*objectname)",
            "%(creatordate:iso-strict)",
            "%(contents)",
        ]
    ).replace(_US, "%1f")
    + "%1e"
)
_SINCE_SHA = re.compile(r"[0-9a-fA-F]{7,64}")

# Conventional Commits: ``type(scope)!: description``.
_CC = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()\r\n]*)\))?(?P<bang>!)?:[ \t]+\S")
_TRAILER = re.compile(
    r"^(?P<key>BREAKING CHANGE|[A-Za-z][A-Za-z0-9-]*)[ \t]*:[ \t]*(?P<value>.*\S)[ \t]*$"
)
_BREAKING_KEYS = frozenset({"breaking change", "breaking-change"})

# Reference shapes (bead tokens come from the shared tokens module).
_SHA = re.compile(
    r"(?<![0-9A-Za-z_-])(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![0-9A-Za-z_-])"
)
_NUMBER = re.compile(r"(?<![0-9A-Za-z_&/])#([0-9]+)(?![0-9A-Za-z_])")
_MERGE_PR = re.compile(r"[Mm]erge pull request #([0-9]+)")
_SQUASH_PR = re.compile(r"\(#([0-9]+)\)\s*$")
_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?[ \t]+(?:[\w.-]+/[\w.-]+)?#([0-9]+)", re.I
)
_PATH = re.compile(r"(?<![0-9A-Za-z_:/.-])(?:[\w.@+-]+/)+[\w@+-]+\.[0-9A-Za-z]+(?![0-9A-Za-z_/])")
_TAG = re.compile(r"(?<![0-9A-Za-z_./-])v[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.]+)?(?![0-9A-Za-z_])")


@dataclass(frozen=True)
class ConventionalSubject:
    """A parsed Conventional Commits subject."""

    type: str
    scope: str | None
    breaking: bool


def parse_conventional(subject: str) -> ConventionalSubject | None:
    """``type(scope)!: description`` as a :class:`ConventionalSubject`, else ``None``.

    The type is lowercased; an empty scope is ``None``.
    """
    match = _CC.match(subject)
    if match is None:
        return None
    scope = (match.group("scope") or "").strip() or None
    return ConventionalSubject(match.group("type").lower(), scope, bool(match.group("bang")))


def parse_trailers(body: str) -> tuple[dict[str, list[str]], list[str]]:
    """The trailer block of a message body and the body lines before it.

    The block is the last paragraph when every non-continuation line is ``Key: value`` (a line
    starting with whitespace continues the previous value). Returns ``(trailers, body lines)``
    with ``trailers`` as key -> values in order; no block gives ``({}, all lines)``.
    """
    lines = body.rstrip().splitlines()
    start = len(lines)
    while start > 0 and lines[start - 1].strip():
        start -= 1
    block = lines[start:]
    if start == 0 or not block or not _TRAILER.match(block[0]):
        return {}, lines
    trailers: dict[str, list[str]] = {}
    last: list[str] | None = None
    for line in block:
        if line[:1] in (" ", "\t") and last is not None:
            last[-1] = f"{last[-1]} {line.strip()}"
            continue
        match = _TRAILER.match(line)
        if match is None:
            return {}, lines
        last = trailers.setdefault(match.group("key"), [])
        last.append(match.group("value"))
    while start > 0 and not lines[start - 1].strip():
        start -= 1
    return trailers, lines[:start]


def _refs(
    text: str, where: str, beads: re.Pattern[str] | None = None, *, subject: bool = False
) -> list[Ref]:
    found: list[Ref] = []

    def add(kind: str, raw: str) -> None:
        ref = Ref(kind, raw, where)
        if ref not in found:
            found.append(ref)

    for match in _MERGE_PR.finditer(text):
        add("pr", f"#{match.group(1)}")
    if subject:
        for match in _SQUASH_PR.finditer(text):
            add("pr", f"#{match.group(1)}")
    for match in _CLOSING.finditer(text):
        add("issue", f"#{match.group(1)}")
    for match in _NUMBER.finditer(text):
        add("issue", match.group(0))
    for token in find_bead_tokens(text, beads or bead_token_pattern(None)):
        add("bead-token", token)
    for pattern, kind in ((_SHA, "sha"), (_PATH, "path"), (_TAG, "tag")):
        for match in pattern.finditer(text):
            add(kind, match.group(0))
    # A "#N" already known as a pr is not also an issue.
    pulls = {ref.raw for ref in found if ref.kind == "pr"}
    return [ref for ref in found if not (ref.kind == "issue" and ref.raw in pulls)]


def _dedupe(refs: Iterable[Ref]) -> tuple[Ref, ...]:
    out: list[Ref] = []
    for ref in refs:
        if ref not in out:
            out.append(ref)
    return tuple(out)


def _paths(fields: list[str]) -> list[str]:
    """Paths of the NUL-split ``--name-status -z`` tail: status, then one path (two for R/C)."""
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index].strip()
        index += 1
        if not status:
            continue
        count = 2 if status[0] in "RC" else 1
        paths.update(item for item in fields[index : index + count] if item)
        index += count
    return sorted(paths)


def _commit(chunk: str, reader: str, version: str, beads: re.Pattern[str]) -> tuple[Artifact, str]:
    head, _, tail = chunk.partition("\0")
    parts = head.split(_US, 4)
    if len(parts) != 5:
        raise GitReadError(f"unparsable git log record: {head[:60]!r}")
    sha, parent_text, author, date, message = parts
    parents = parent_text.split()
    subject, _, rest = message.partition("\n")
    subject = subject.rstrip("\r")
    trailers, body_lines = parse_trailers(rest)
    parsed = parse_conventional(subject)
    breaking = bool(parsed and parsed.breaking) or any(
        key.lower() in _BREAKING_KEYS for key in trailers
    )
    committed = commit_time(date)
    if committed is None:
        raise GitReadError(f"commit {sha}: unparsable committer date {date!r}")
    refs = _refs(subject, "subject", beads, subject=True) + _refs(
        "\n".join(body_lines), "body", beads
    )
    for values in trailers.values():
        for value in values:
            refs += _refs(value, "trailer", beads)
    attrs = {
        "sha": sha,
        "parents": parents,
        "author": author,
        "committed_at": committed,
        "cc_type": parsed.type if parsed else None,
        "cc_scope": parsed.scope if parsed else None,
        "breaking": breaking,
        "paths": _paths([item for item in tail.lstrip("\n").split("\0")]),
        "trailers": trailers,
    }
    artifact = Artifact(
        category=Category.CHANGE,
        kind="merge" if len(parents) > 1 else "commit",
        key=f"git:commit/{sha}",
        title=subject,
        occurred_at=committed,
        attrs=attrs,
        raw_attrs={"committed_at": date, "subject": subject},
        refs=_dedupe(refs),
        reader=reader,
        reader_version=version,
    )
    return artifact, sha


def _release(
    record: str, previous: str | None, reader: str, version: str, beads: re.Pattern[str]
) -> tuple[Artifact, str] | None:
    parts = record.strip("\n").split(_US, 5)
    if len(parts) != 6 or not parts[0]:
        return None
    name, kind, obj, target, date, message = parts
    annotated = kind == "tag"
    commit = target if annotated else obj
    released = commit_time(date) if date else None
    message = message.strip("\n") if annotated else ""
    subject, _, rest = message.partition("\n")
    refs = [Ref("sha", commit, "target")] if commit else []
    refs += _refs(subject, "subject", beads) + _refs(rest, "body", beads)
    artifact = Artifact(
        category=Category.RELEASE_MARKER,
        kind="release" if annotated else "tag",
        key=f"git:tag/{name}",
        title=name,
        occurred_at=released,
        attrs={
            "version": name,
            "released_at": released,
            "commit_range": f"{previous}..{name}" if previous else None,
            "annotated": annotated,
            "target": commit or None,
            "message": message,
        },
        raw_attrs={"released_at": date} if date else {},
        refs=_dedupe(refs),
        reader=reader,
        reader_version=version,
    )
    return artifact, name


# -- history errors and the raw message log ------------------------------------------------


class CommitsSourceError(ValueError):
    """The git history cannot be read or the baseline cannot resolve bead ids (a ``ValueError``,
    so the ``beadlog`` CLI reports it as a failed operation)."""


class ShallowCloneError(CommitsSourceError):
    """The repository is a shallow clone, so its history is truncated."""


@dataclass(frozen=True)
class Commit:
    """One commit: ``message`` is subject AND body; ``date`` the committer date (ISO 8601)."""

    sha: str
    parents: int
    subject: str
    message: str
    date: str


def _git(
    root: Path | str, args: Sequence[str], git: str = "git"
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [git, "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise CommitsSourceError(f"cannot run {git}: {exc}") from exc


def _git_out(root: Path | str, args: Sequence[str], git: str = "git") -> str:
    result = _git(root, args, git)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise CommitsSourceError(
            f"git {' '.join(args[:2])} failed in {root} ({result.returncode}): "
            f"{detail[-1] if detail else ''}"
        )
    return result.stdout


def is_shallow(root: Path | str, git: str = "git") -> bool:
    """Whether the repository at ``root`` is a shallow clone (asks git; see also
    :func:`is_shallow_repo`, which reads the ``shallow`` file without a process)."""
    return _git_out(root, ["rev-parse", "--is-shallow-repository"], git).strip() == "true"


def require_full_history(root: Path | str, git: str = "git") -> None:
    """Refuse a shallow clone with the remediation text."""
    if is_shallow(root, git):
        raise ShallowCloneError(
            f"{root} is a shallow clone: its history is truncated, so beads whose commits "
            f"predate the cut would be missed; {SHALLOW_REMEDIATION}"
        )


def resolve_commit(root: Path | str, rev: str, git: str = "git") -> str | None:
    """The full sha ``rev`` names when it is a commit, else ``None``."""
    probe = _git(root, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], git)
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def read_commits(
    root: Path | str,
    rev: str = "HEAD",
    *,
    since: str | None = None,
    limit: int | None = None,
    git: str = "git",
) -> list[Commit]:
    """The raw message log: walk ``rev`` backwards, newest first, keeping each full message.

    For consumers that need a commit's message text as written (coverage hashes it, the
    correlation scripts re-scan it); the artifacts of :func:`read_git` keep the parsed subject,
    trailers and references instead. ``since`` narrows the walk: a commit (anything ``git``
    resolves to one) means only the commits after it, ``since..rev``; any other value is a date
    for ``git log --since``.
    """
    fmt = _US.join(["%H", "%P", "%cI", "%s", "%B"]) + _RS
    args = ["log", f"--pretty=format:{fmt}", "--no-color"]
    if since:
        if resolve_commit(root, since, git) is not None:
            args.append(f"{since}..{rev}")
        else:
            args += [f"--since={since}", rev]
    else:
        args.append(rev)
    if limit:
        args.append(f"-n{limit}")
    raw = _git_out(root, args, git)
    commits: list[Commit] = []
    for chunk in raw.split(_RS):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split(_US)
        if len(parts) < 5:
            continue
        sha, parents, date, subject, body = parts[:5]
        commits.append(
            Commit(
                sha=sha,
                parents=len(parents.split()),
                subject=subject,
                # %B already holds subject + body; both stay explicit so "subject AND body"
                # is visible rather than implied.
                message=f"{subject}\n{body}",
                date=date,
            )
        )
    return commits


def _git_dir(root: Path) -> Path | None:
    dot = root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():  # a linked worktree or submodule: ``gitdir: <path>``
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            path = Path(text.partition(":")[2].strip())
            return path if path.is_absolute() else (root / path).resolve()
    return root if (root / "HEAD").is_file() else None  # a bare repository


def is_shallow_repo(root: Path | str) -> bool:
    """Whether ``root`` is a shallow clone, read from the ``shallow`` file (no process)."""
    git_dir = _git_dir(Path(root))
    if git_dir is None:
        return False
    common = git_dir / "commondir"
    if common.is_file():
        git_dir = (git_dir / common.read_text(encoding="utf-8").strip()).resolve()
    return (git_dir / "shallow").is_file()


@dataclass(frozen=True)
class GitRead:
    """One read: the ``artifacts`` and the ``cursor`` for the next incremental read."""

    artifacts: tuple[Artifact, ...]
    cursor: str | None


def _argv(binary: str, root: Path, *args: str) -> list[str]:
    return [binary, "-C", str(root), "-c", "core.quotepath=off", *args]


def read_git(request: ReadRequest, runner: Runner | None = None) -> GitRead:
    """Read the repository of ``request`` (see the module docstring); two ``runner`` calls."""
    options = request.options
    binary = str(options.get("git") or "git")
    rev = str(options.get("rev") or "HEAD")
    since = str(options.get("since") or "")
    root = Path(request.root)
    if not truthy(options.get("allow_shallow", False)) and is_shallow_repo(root):
        raise ShallowCloneError(
            f"{root} is a shallow clone: its history is truncated; {SHALLOW_REMEDIATION}"
        )
    run = runner or subprocess_runner(DEFAULT_TIMEOUT_SECONDS, GitReadError, "git")
    cursor = request.cursor if isinstance(request.cursor, str) and request.cursor else None
    log = ["log", "-z", "--parents", "-M", "--name-status", "--no-color", "--no-ext-diff"]
    log.append(f"--format={_LOG_FORMAT}")
    base = cursor or (since if _SINCE_SHA.fullmatch(since) else "")
    if base:
        log.append(f"{base}..{rev}")
    else:
        if since:
            log.append(f"--since={since}")
        log.append(rev)
    log.append("--")
    text = run(_argv(binary, root, *log))
    refs = run(
        _argv(
            binary,
            root,
            "for-each-ref",
            "--sort=refname",
            "--sort=creatordate",
            f"--format={_TAG_FORMAT}",
            "refs/tags",
        )
    )
    beads = bead_token_pattern(csv(options.get("namespaces", "")))
    artifacts: list[Artifact] = []
    head: str | None = None
    for chunk in text.split(_RS):
        if not chunk.strip("\0\n"):
            continue
        artifact, sha = _commit(chunk, READER_NAME, READER_VERSION, beads)
        head = head or sha
        artifacts.append(artifact)
    previous: str | None = None
    for record in refs.split(_RS):
        made = _release(record, previous, READER_NAME, READER_VERSION, beads)
        if made is not None:
            artifacts.append(made[0])
            previous = made[1]
    return GitRead(tuple(artifacts), head or cursor)


class GitReader:
    """The ``git`` :class:`~beadlog_sources.readers.SourceReader`."""

    name = READER_NAME
    version = READER_VERSION
    categories: tuple[Category, ...] = (Category.CHANGE, Category.RELEASE_MARKER)

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner

    @staticmethod
    def header_options(options: Mapping[str, Any]) -> dict[str, Any]:
        """The options recorded in the run's ReaderHeader: the given ones plus ``token_mode``."""
        return {**options, "token_mode": token_mode(csv(options.get("namespaces", "")))}

    def read(self, request: ReadRequest) -> Iterable[Artifact]:
        """The commits and tags of the repository at ``request.root``."""
        return read_git(request, self._runner).artifacts


__all__ = [
    "READER_NAME",
    "READER_VERSION",
    "SHALLOW_REMEDIATION",
    "Commit",
    "CommitsSourceError",
    "ConventionalSubject",
    "GitRead",
    "GitReader",
    "ShallowCloneError",
    "is_shallow",
    "is_shallow_repo",
    "parse_conventional",
    "parse_trailers",
    "read_commits",
    "read_git",
    "require_full_history",
    "resolve_commit",
]
