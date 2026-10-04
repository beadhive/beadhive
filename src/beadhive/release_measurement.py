"""Measured executing package release, shared by heartbeat reports and emergency admission.

A leaf on purpose: ``frame_emergency`` sits under the HQ lease/control-plane modules, so
reaching back into ``heartbeat_report`` (CLI + config + hive readiness) would close an
import cycle through the whole legacy core.
"""

from __future__ import annotations

import hashlib
import importlib.metadata

from .hq_framelease_contracts import HeartbeatError


def installed_release() -> dict[str, str]:
    """Hash installed package files using the original enrollment measurement."""
    distribution = importlib.metadata.distribution("beadhive")
    digest = hashlib.sha256()
    count = 0
    for item in sorted(distribution.files or [], key=str):
        name = str(item)
        path = distribution.locate_file(item)
        if (
            name.startswith("beadhive/")
            and "__pycache__" not in name
            and not name.endswith(".pyc")
            and path.is_file()
        ):
            digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
            count += 1
    if not count:
        raise HeartbeatError("installed package files unavailable; editable installs cannot attest")
    return {"id": distribution.version, "digest": "sha256:" + digest.hexdigest()}
