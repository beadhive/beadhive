"""Paths for generated validation evidence kept outside the tracked checkout.

bh-vi4ob.1: every worktree in this repo shares one physical ``.git`` (``git-common-dir``), so a
flat filename here used to be ONE file shared by every worktree on the host. A key-verdict reuse
or carry that skips re-running a key's command left a worktree trusting whichever tree last wrote
that shared file -- not necessarily its own. Every generated evidence file is now partitioned by
:func:`checkout_tree_scope`, so two worktrees validating different content can never read or
clobber one another's evidence: reuse becomes safe by construction rather than by timing.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

# The operational report (bh-ck1t6) is generated FROM this evidence and lives in the tracked
# checkout; excluding it here (mirroring
# ``test_closure_certification.CHECKOUT_IDENTITY_EXCLUDES``) keeps a refresh from being able to
# relocate its own evidence directory mid-run.
_SELF_REFERENTIAL_EXCLUDES = frozenset({"docs/SELECTIVE-CI-OPERATIONS.md"})


def _common_dir(root: Path) -> Path:
    completed = subprocess.run(
        ("git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"),
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(completed.stdout.strip())


def checkout_tree_scope(root: Path) -> str:
    """A content-addressed key for the exact checkout currently under validation at ``root``.

    This hashes real on-disk bytes for every tracked path -- never the Git index's blob ids, so
    an uncommitted edit is never mistaken for its last-staged content -- purely to partition the
    shared, git-private evidence directory below. Two worktrees on the very same commit but with
    different uncommitted edits (the common case right after ``bh work claim``, before either has
    made its first commit) must still land in different directories, which a cheaper key such as
    ``git rev-parse HEAD^{tree}`` cannot guarantee.

    This intentionally mirrors ``test_closure_certification.checkout_input_identity``'s tree
    digest without importing it, to avoid a circular import between this low-level path helper
    and the script that is its main caller.
    """
    completed = subprocess.run(
        ("git", "-C", str(root), "ls-files", "--stage", "-z"),
        check=True,
        capture_output=True,
    )
    digest = hashlib.sha256()
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        header, encoded_path = raw.split(b"\t", 1)
        mode, _object_id, stage = header.decode().split()
        relative = encoded_path.decode()
        if stage != "0" or relative in _SELF_REFERENTIAL_EXCLUDES:
            continue
        path = root / relative
        content = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        blob = hashlib.sha256(content).hexdigest()
        digest.update(f"{mode}\0{relative}\0sha256:{blob}\n".encode())
    return digest.hexdigest()


def evidence_directory(root: Path) -> Path:
    configured = os.environ.get("BH_VALIDATION_EVIDENCE_DIR")
    base = (
        Path(configured).expanduser().resolve()
        if configured
        else _common_dir(root) / "bh" / "validation" / "evidence"
    )
    return base / checkout_tree_scope(root)


def evidence_path(root: Path, name: str) -> Path:
    return evidence_directory(root) / name
