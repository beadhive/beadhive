from __future__ import annotations

import importlib.util
import os
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_cache_locality.py"
SPEC = importlib.util.spec_from_file_location("benchmark_cache_locality", SCRIPT)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_hardlink_observation_reports_target_file_identity(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    original = tmp_path / "package-file"
    original.write_text("fixture")
    os.link(original, target / "package-file")

    observed = benchmark._hardlink_observation(target, target.stat().st_dev)

    info = (target / "package-file").stat()
    assert observed["files"] == 1
    assert observed["files_with_multiple_links"] == 1
    assert observed["hardlink_identities"] == [
        {
            "path": "package-file",
            "device": info.st_dev,
            "inode": info.st_ino,
            "link_count": 2,
        }
    ]


def test_hardlink_observation_ignores_linked_files_on_another_device(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    original = tmp_path / "package-file"
    original.write_text("fixture")
    os.link(original, target / "package-file")

    observed = benchmark._hardlink_observation(target, target.stat().st_dev + 1)

    assert observed["files"] == 1
    assert observed["files_with_multiple_links"] == 0
    assert observed["hardlink_identities"] == []
