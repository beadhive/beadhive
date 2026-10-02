"""Current config generation shares canonical release normalization and inventory."""

import runpy
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("check", [True, False])
def test_generator_uses_current_canonical_bundle_and_preserves_archive(
    tmp_path, monkeypatch, check
):
    script = runpy.run_path(str(Path(__file__).parents[1] / "scripts/generate_config_schema.py"))
    main = script["main"]
    current = tmp_path / "v2.0.0"
    archive = tmp_path / "v1.0.0" / "artifacts/config-v1.schema.json"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"historical version one bytes")
    canonical = {
        Path("artifacts/config-v1.schema.json"): b'{"version":2}\n',
        Path("artifacts/plugin-config-test-v1.schema.json"): b'{"version":2}\n',
        Path("inventory.json"): b"inventory remains writer-owned",
    }
    for relative, data in canonical.items():
        path = current / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    calls = []
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(main.__globals__, "ARTIFACT_ROOT", current / "artifacts")
    monkeypatch.setitem(main.__globals__, "render_release", lambda: canonical)
    monkeypatch.setitem(main.__globals__, "write_release", calls.append)
    monkeypatch.setattr(sys, "argv", ["generate_config_schema.py", *(["--check"] if check else [])])
    assert main() == 0
    assert calls == ([] if check else [current])
    assert archive.read_bytes() == b"historical version one bytes"
    assert script["ARTIFACT_ROOT"].parent.name == "v" + script["RELEASE_VERSION"]
