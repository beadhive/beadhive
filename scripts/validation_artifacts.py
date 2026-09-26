"""Paths for generated validation evidence kept outside the tracked checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def evidence_directory(root: Path) -> Path:
    configured = os.environ.get("BH_VALIDATION_EVIDENCE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    completed = subprocess.run(
        ("git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"),
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(completed.stdout.strip()) / "bh" / "validation" / "evidence"


def evidence_path(root: Path, name: str) -> Path:
    return evidence_directory(root) / name
