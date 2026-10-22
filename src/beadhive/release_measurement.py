"""Measured executing package release, shared by heartbeat reports and emergency admission.

A leaf on purpose: ``frame_emergency`` sits under the HQ lease/control-plane modules, so
reaching back into ``heartbeat_report`` (CLI + config + hive readiness) would close an
import cycle through the whole legacy core.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
from collections.abc import Iterable

ALGORITHM = "beadhive-tree-sha256-v1"


def measured_member(name: str) -> bool:
    """True when ``name`` is part of the v1 measurement (``beadhive/`` sources, no bytecode)."""
    return name.startswith("beadhive/") and "__pycache__" not in name and not name.endswith(".pyc")


def fold_digest(entries: Iterable[tuple[str, bytes]]) -> tuple[str, int]:
    """Fold ``(name, bytes)`` pairs into ``("sha256:<hex>", file_count)``.

    Pure: filters with ``measured_member``, sorts by name and hashes
    ``name NUL sha256(content)`` per file. Shared by ``installed_release`` and
    ``scripts/release-manifest.py`` so the two cannot drift.
    """
    digest = hashlib.sha256()
    count = 0
    for name, data in sorted(
        ((n, d) for n, d in entries if measured_member(n)), key=lambda e: e[0]
    ):
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
        count += 1
    return "sha256:" + digest.hexdigest(), count


def installed_release() -> dict[str, str]:
    """Hash installed package files using the original enrollment measurement."""
    from .hq_framelease_contracts import HeartbeatError  # deferred: keeps this module stdlib-only

    distribution = importlib.metadata.distribution("beadhive")
    entries = []
    for item in distribution.files or []:
        name = str(item)
        path = distribution.locate_file(item)
        if measured_member(name) and path.is_file():
            entries.append((name, path.read_bytes()))
    digest, count = fold_digest(entries)
    if not count:
        raise HeartbeatError("installed package files unavailable; editable installs cannot attest")
    return {"id": distribution.version, "digest": digest}
