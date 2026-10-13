"""Per-worker snapshots of real Git universes, restored onto one fixed live root (bh-gb56v).

The real-git work tests rebuild the same repository shapes in every test: a hive with a bare
remote, or a molecule landed through real worktrees, claims, submits and merges. Git topology is
what those tests prove, so Git stays real; only the repeated setup is shared.

A ``TreeTemplates`` store keeps one snapshot directory per key. ``capture`` copies the live root
into a new snapshot and records its digest; ``restore`` checks that the snapshot still has that
digest, replaces the live root with a fresh copy and checks the copy has the same digest. A test
therefore always starts from a byte-identical tree and never sees another test's writes, and a
test that writes into a snapshot fails the next restore instead of leaking into later tests.

The live root is fixed for the store's lifetime (one store per xdist worker), so the absolute
paths Git records — ``.git/worktrees/*/gitdir``, each worktree's ``.git`` file, remote URLs —
stay valid after a restore, the same pattern as ``_PreparedHq`` in
``tests/test_hq_authority_backend.py``. Payloads (fake tracker state, captured output) are
deep-copied out on every restore so tests cannot share mutable objects either.
"""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import stat
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


class SnapshotMutated(AssertionError):
    """A snapshot changed after capture, or a restore did not reproduce it byte for byte."""


def tree_digest(
    root: Path,
    *,
    exclude: Iterable[str] = (),
    normalize: Mapping[bytes, bytes] | None = None,
) -> str:
    """Digest every entry under ``root``: relative path, kind, permission bits and content.

    Symlinks are recorded by target, never followed; special files by kind. ``exclude`` names
    relative paths to leave out; ``normalize`` rewrites byte strings in file contents and link
    targets before hashing, so two trees that differ only in where they live (an absolute root
    path written into a Git config) can be compared.
    """
    root = Path(root)
    skipped = set(exclude)
    replacements = sorted((normalize or {}).items(), key=lambda item: -len(item[0]))

    def norm(data: bytes) -> bytes:
        for old, new in replacements:
            data = data.replace(old, new)
        return data

    digest = hashlib.sha256()
    for current, dirs, files in os.walk(root):
        dirs.sort()
        here = Path(current)
        for name in [*dirs, *sorted(files)]:
            path = here / name
            rel = path.relative_to(root).as_posix()
            if rel in skipped:
                continue
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                entry = b"L" + norm(os.readlink(path).encode())
            elif stat.S_ISDIR(info.st_mode):
                entry = b"D"
            elif stat.S_ISREG(info.st_mode):
                entry = b"F" + hashlib.sha256(norm(path.read_bytes())).digest()
            else:  # a FIFO, socket or device node: its kind, never a blocking read
                entry = b"S" + str(stat.S_IFMT(info.st_mode)).encode()
            digest.update(f"{rel}\0{mode:o}\0".encode() + entry + b"\0")
        dirs[:] = [d for d in dirs if (here / d).relative_to(root).as_posix() not in skipped]
    return digest.hexdigest()


def copy_tree(source: Path, target: Path) -> None:
    """Copy ``source`` (a directory, file or symlink) to a new ``target`` keeping symlinks,
    permission bits and mtimes."""
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, symlinks=True)
    elif target.exists() or target.is_symlink():
        raise FileExistsError(target)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    digest: str
    payload: object


class TreeTemplates:
    """One worker's snapshots, all restored onto the same fixed ``live`` directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.live = self.root / "live"
        self._snapshots: dict[Hashable, _Snapshot] = {}

    def __contains__(self, key: Hashable) -> bool:
        return key in self._snapshots

    def reset_live(self) -> Path:
        """Empty the live root, ready for a build."""
        shutil.rmtree(self.live, ignore_errors=True)
        self.live.mkdir(parents=True)
        return self.live

    def capture(self, key: Hashable, payload: object = None) -> None:
        """Snapshot the current live root (and a private copy of ``payload``) under ``key``."""
        if key in self._snapshots:
            raise KeyError(f"template already captured: {key!r}")
        path = self.root / f"snapshot-{len(self._snapshots)}"
        copy_tree(self.live, path)
        self._snapshots[key] = _Snapshot(path, tree_digest(path), copy.deepcopy(payload))

    def restore(self, key: Hashable, into: Path | None = None) -> object:
        """Replace the live root with a byte-identical copy of ``key``; return its payload.

        With ``into``, copy the snapshot's top-level entries into that existing directory
        instead (for a template with no absolute paths of its own) and leave the live root alone.
        """
        snapshot = self._snapshots[key]
        if tree_digest(snapshot.path) != snapshot.digest:
            raise SnapshotMutated(
                f"template snapshot {snapshot.path} changed after capture; a test wrote into it"
            )
        if into is not None:
            for entry in sorted(snapshot.path.iterdir()):
                copy_tree(entry, Path(into) / entry.name)
            return copy.deepcopy(snapshot.payload)
        shutil.rmtree(self.live, ignore_errors=True)
        copy_tree(snapshot.path, self.live)
        if tree_digest(self.live) != snapshot.digest:
            raise SnapshotMutated(f"restored {self.live} differs from snapshot {snapshot.path}")
        return copy.deepcopy(snapshot.payload)


_WORKER_TEMPLATES: dict[tuple[Path, str], TreeTemplates] = {}


def worker_templates(tmp_path_factory, name: str) -> TreeTemplates:
    """The ``name`` store of this pytest session's base directory, i.e. of this xdist worker."""
    key = (tmp_path_factory.getbasetemp(), name)
    if key not in _WORKER_TEMPLATES:
        _WORKER_TEMPLATES[key] = TreeTemplates(tmp_path_factory.mktemp(f"{name}-templates"))
    return _WORKER_TEMPLATES[key]
