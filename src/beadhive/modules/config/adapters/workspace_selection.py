"""Local Git workspace source precedence, with all locations supplied by the caller.

This adapter stays independent of the public config and git-workspace facades so
both normal reads and an authority switch can verify the same selected bytes.
"""

from __future__ import annotations

from glob import glob
from pathlib import Path


def glob_configs(directory: Path) -> list[Path]:
    found = sorted(glob(f"{directory}/workspace*.toml"))
    return [Path(path) for path in found if not path.endswith("-lock.toml")]


def external_configs(root: Path) -> list[Path]:
    return [path for path in glob_configs(root) if not path.name.startswith("workspace-bh-")]


def selected_git_sources(cfg, *, root: Path, hq_dir: Path) -> list[Path]:
    """Select the exact local Git inputs without consulting a backend selector."""
    explicit = (cfg.get("git_workspace") or {}).get("path")
    if explicit:
        path = Path(explicit).expanduser()
        return [path] if path.exists() else []
    if own := external_configs(root):
        return own
    return glob_configs(hq_dir)
