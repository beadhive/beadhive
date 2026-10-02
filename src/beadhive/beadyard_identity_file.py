"""Durable local Git-HQ copy of the portable beadyard identity document.

Creation is exclusive and atomic. A missing document on an existing HQ is a
legacy state; only an explicit creation or guarded adoption may write it.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from .beadyard_identity import (
    DOCUMENT_PATH,
    BeadyardIdentityError,
    new_document,
    parse_document,
)

_MAX_BYTES = 4096


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_identity(hq_dir: Path) -> str | None:
    """Read an existing identity, preserving missing legacy state as ``None``."""
    path = hq_dir / DOCUMENT_PATH
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise BeadyardIdentityError("beadyard identity document is unreadable") from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_BYTES:
            raise BeadyardIdentityError("invalid beadyard identity document")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            content = stream.read(_MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(content) > _MAX_BYTES:
        raise BeadyardIdentityError("invalid beadyard identity document")
    return parse_document(content)


def create_identity(hq_dir: Path) -> str:
    """Persist exactly one ID at explicit new-HQ creation, returning a race winner.

    A completed existing document is idempotent. The hard link makes the new
    document visible only after its contents are flushed; no reader sees a
    partially written ID and no retry can overwrite a previous winner.
    """
    existing = read_identity(hq_dir)
    if existing is not None:
        _sync_directory(hq_dir)
        return existing
    content = new_document().encode("utf-8")
    identity = parse_document(content)
    descriptor, temporary = tempfile.mkstemp(prefix=".beadyard-", dir=hq_dir)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, hq_dir / DOCUMENT_PATH)
        except FileExistsError:
            winner = read_identity(hq_dir)
            if winner is None:
                raise BeadyardIdentityError(
                    "beadyard identity creation raced with removal"
                ) from None
            _sync_directory(hq_dir)
            return winner
        _sync_directory(hq_dir)
        return identity
    finally:
        os.unlink(temporary)
