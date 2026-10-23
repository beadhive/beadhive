"""Keep a registered Dolt remote URL out of tracked ``.beads/config.yaml`` and Git history.

Measured on bd 1.3.0 (bh-j75nl): ``bd dolt remote add <name> <url>`` writes
``sync.remote: <url>`` into ``.beads/config.yaml`` and, when that file is Git-tracked, ALSO
commits it as ``bd: update sync.remote``. bd 1.3.0 has no flag or setting that suppresses either
step (``no-git-ops`` only changes the session-close text). For a public code repository whose
internal store lives on a private object store, every registration through bd would therefore
publish the private address (an internal hostname) into code history.

Two layers, both CORRECTNESS rules rather than policy (nothing here is optional):

* :func:`guarded` wraps every bh path that registers a remote through bd. It snapshots the
  tracked config and ``HEAD`` first and, after bd returns, undoes exactly bd's side effect: a
  ``bd: update sync.remote`` commit that touched only the config file is dropped
  (``reset --soft`` + restoring the file), and an uncommitted ``sync.remote`` edit is reverted to
  the pre-call value. A call that changed nothing, an UNTRACKED config (bd's write is then local
  and harmless) and a hive outside Git are no-ops.
* :func:`leak_finding` is the standing detector for ``bh doctor`` / ``bh hive ready``: a tracked
  ``sync.remote`` holding an object-store address is a leak waiting to be pushed; a Git hint
  is not.

The undo is preferred over registering through ``CALL DOLT_REMOTE`` because it keeps bd's own
validation (the git-origin guard, URL checks) on the path and changes no registration
semantics; the registry write itself is untouched.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import bd, dolt_remote

_REL = ".beads/config.yaml"
_COMMIT_SUBJECT = "bd: update sync.remote"
_LINE = re.compile(r"^sync\.remote\s*:.*(?:\n|$)", re.MULTILINE)
_TIMEOUT = 30


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )


def _tracked(cwd: Path) -> bool:
    if not (cwd / _REL).is_file():
        return False
    return _git(cwd, "ls-files", "--error-unmatch", "--", _REL).returncode == 0


def _head(cwd: Path) -> str:
    res = _git(cwd, "rev-parse", "--verify", "-q", "HEAD")
    return res.stdout.strip() if res.returncode == 0 else ""


@dataclass
class Snapshot:
    root: Path
    head: str
    content: str


def snapshot(cwd) -> Snapshot | None:
    """The pre-call state, or None when there is nothing to guard (no tracked config)."""
    root = Path(cwd)
    try:
        if not _tracked(root):
            return None
        head = _head(root)
        return Snapshot(root, head, (root / _REL).read_text()) if head else None
    except (OSError, subprocess.SubprocessError):
        return None


_NESTED = re.compile(r"^sync:[ \t]*\n((?:[ \t]+.*(?:\n|$))+)", re.MULTILINE)
_NESTED_REMOTE = re.compile(r"^[ \t]+remote\s*:")


def _without_sync_remote(text: str) -> str:
    """*text* minus ``sync.remote`` in either spelling: bd's flat ``sync.remote:`` line, or the
    nested ``sync:`` / ``  remote:`` mapping bd 1.3.0 writes in server mode (bh-glpwp). An emptied
    ``sync:`` block goes too; its other keys stay."""

    def nested(m: re.Match) -> str:
        kept = [ln for ln in m.group(1).splitlines(keepends=True) if not _NESTED_REMOTE.match(ln)]
        return "sync:\n" + "".join(kept) if any(ln.strip() for ln in kept) else ""

    return _NESTED.sub(nested, _LINE.sub("", text))


def _sync_remote_text(text: str) -> str:
    """Just the ``sync.remote`` lines of *text*, in whichever spelling it uses ("" if none)."""
    flat = "".join(_LINE.findall(text))
    nested = "".join(
        "sync:\n"
        + "".join(ln for ln in m.group(1).splitlines(keepends=True) if _NESTED_REMOTE.match(ln))
        for m in _NESTED.finditer(text)
        if any(_NESTED_REMOTE.match(ln) for ln in m.group(1).splitlines())
    )
    return flat + nested


def scrub(snap: Snapshot | None) -> str:
    """Undo bd's ``sync.remote`` side effect since *snap*; return a one-line note ("" if none)."""
    if snap is None:
        return ""
    root = snap.root
    notes: list[str] = []
    try:
        head = _head(root)
        if head and head != snap.head:
            names = _git(root, "diff", "--name-only", snap.head, head).stdout.split()
            subjects = _git(root, "log", "--format=%s", f"{snap.head}..{head}").stdout.split("\n")
            ancestor = _git(root, "merge-base", "--is-ancestor", snap.head, head).returncode == 0
            subjects = [s for s in subjects if s]
            if ancestor and names == [_REL] and set(subjects) == {_COMMIT_SUBJECT}:
                _git(root, "reset", "--soft", snap.head)
                _git(root, "restore", "--staged", "--source", snap.head, "--", _REL)
                notes.append("dropped bd's 'update sync.remote' commit")
        path = root / _REL
        now = path.read_text() if path.is_file() else snap.content
        if now != snap.content:
            if _without_sync_remote(now).rstrip() == _without_sync_remote(snap.content).rstrip():
                # Only sync.remote moved: put the original bytes back.
                path.write_text(snap.content)
                notes.append("reverted tracked sync.remote")
            elif _sync_remote_text(now) != _sync_remote_text(snap.content):
                # bd (or a concurrent editor) also changed other lines; restore only the key.
                old = _sync_remote_text(snap.content)
                rest = _without_sync_remote(now)
                path.write_text(rest + ("" if rest.endswith("\n") or not rest else "\n") + old)
                notes.append("reverted tracked sync.remote")
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            f"could not scrub tracked sync.remote ({type(exc).__name__}); check .beads/config.yaml"
        )
    return "; ".join(notes)


@contextlib.contextmanager
def guarded(cwd) -> Iterator[None]:
    """Run a bd remote-registering call so it leaves no remote URL in tracked config or Git."""
    snap = snapshot(cwd) if cwd is not None else None
    try:
        yield
    finally:
        scrub(snap)


_STORE_FAMILIES = frozenset(
    {dolt_remote.RemoteFamily.S3, dolt_remote.RemoteFamily.AWS, dolt_remote.RemoteFamily.GS}
)


def leak_finding(entry: Mapping | None, root: Path) -> str | None:
    """A hive-ready/doctor message when a tracked ``sync.remote`` holds an object-store address,
    else None.

    Only the URL itself decides (bh-uhvf5): an object-store address (S3, an S3-compatible
    endpoint, GCS) in code history is the exposure. A Git or forge URL (``git+ssh``, ``https``,
    scp-style) names the code origin the repository already points at, so a stale hint is not a
    leak, even on a hive whose internal store moved to an HQ descriptor; flagging it failed a
    frame's conformance over harmless state."""
    if entry is None or not _tracked(root):
        return None
    url = bd.sync_remote(root)
    if not url:
        return None
    if dolt_remote.classify(url).family not in _STORE_FAMILIES:
        return None
    return (
        f"tracked {_REL} carries sync.remote={dolt_remote.redact_url(url)}, an object-store "
        "address committed to code history — `bd config unset sync.remote` and commit the removal"
    )
