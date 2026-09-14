"""Fail-closed discovery of the repository's pinned Pants launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
MISSING_LAUNCHER = (
    "Pants launcher unavailable: run `mise install scie-pants`, install the official "
    "launcher from pantsbuild.org, or set PANTS_BIN=/absolute/path/to/pants"
)


def _executable(candidate: str | None) -> str | None:
    if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def launcher(environ: dict[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    path = environment.get("PATH")
    explicit = environment.get("PANTS_BIN")
    if explicit:
        resolved = _executable(shutil.which(explicit, path=path))
        if resolved:
            return resolved
        raise RuntimeError(f"PANTS_BIN is not executable: {explicit}")

    for name in ("pants", "scie-pants"):
        resolved = _executable(shutil.which(name, path=path))
        if resolved:
            return resolved

    mise = _executable(shutil.which("mise", path=path))
    if mise:
        result = subprocess.run(
            [mise, "which", "scie-pants"],
            cwd=ROOT,
            env=dict(environment),
            capture_output=True,
            text=True,
            check=False,
        )
        lines = (result.stdout or "").splitlines()
        if result.returncode == 0 and len(lines) == 1:
            resolved = _executable(lines[0].strip())
            if resolved:
                return resolved

    raise RuntimeError(MISSING_LAUNCHER)
