"""Unit tests for the shared measurement fold and scripts/release-manifest.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import release_measurement

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "beadhive/__init__.py": b"x = 1\n",
    "beadhive/sub/mod.py": b"y = 2\n",
    "beadhive/__pycache__/mod.cpython-312.pyc": b"junk",
    "beadhive/stale.pyc": b"junk",
    "beadhive_core/other.py": b"outside\n",
    "beadhive-1.0.dist-info/METADATA": b"Name: beadhive\nVersion: 1.0\n",
}


def _script():
    spec = importlib.util.spec_from_file_location(
        "release_manifest_script", ROOT / "scripts" / "release-manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel(tmp_path: Path, files: dict[str, bytes] = FILES) -> Path:
    path = tmp_path / "beadhive-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _expected() -> str:
    digest = hashlib.sha256()
    for name in ("beadhive/__init__.py", "beadhive/sub/mod.py"):
        digest.update(name.encode() + b"\0" + hashlib.sha256(FILES[name]).digest())
    return "sha256:" + digest.hexdigest()


def test_fold_filters_and_is_order_independent() -> None:
    forward = release_measurement.fold_digest(FILES.items())
    backward = release_measurement.fold_digest(reversed(list(FILES.items())))

    assert forward == backward == (_expected(), 2)


def test_fold_empty_measures_nothing() -> None:
    assert release_measurement.fold_digest([("other/x.py", b"1")])[1] == 0


def test_installed_release_matches_fold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, data in FILES.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    dist = SimpleNamespace(
        files=[Path(n) for n in FILES],
        version="1.0",
        locate_file=lambda item: tmp_path / item,
    )
    monkeypatch.setattr(release_measurement.importlib.metadata, "distribution", lambda _: dist)

    assert release_measurement.installed_release() == {"id": "1.0", "digest": _expected()}


def test_script_manifest_matches_fold(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    out = tmp_path / "manifest.json"

    assert _script().main([str(wheel), "--commit", "c0ffee", "--output", str(out)]) == 0

    assert json.loads(out.read_text()) == {
        "version": "1.0",
        "measured_digest": _expected(),
        "algorithm": "beadhive-tree-sha256-v1",
        "file_count": 2,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "commit": "c0ffee",
    }


def test_script_prints_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _script().main([str(_wheel(tmp_path)), "--commit", "abc"])

    assert json.loads(capsys.readouterr().out)["commit"] == "abc"


def test_script_refuses_wheel_without_beadhive_files(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path, {"beadhive-1.0.dist-info/METADATA": b"Version: 1.0\n"})

    with pytest.raises(SystemExit, match="no beadhive/ files"):
        _script().main([str(wheel), "--commit", "abc"])
