"""Regression coverage for the hermetic installed/current parity probe."""

from __future__ import annotations

import importlib.util
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo_live_ingress_matrix.py"
SPEC = importlib.util.spec_from_file_location("demo_live_ingress_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
demo_live_ingress_matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo_live_ingress_matrix)


def test_parity_does_not_require_venv_or_ensurepip(tmp_path) -> None:
    real_python = sys.executable
    python_without_venv = tmp_path / "python-without-venv"
    python_without_venv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && '
        '{ [ "$2" = "venv" ] || [ "$2" = "ensurepip" ]; }; then\n'
        '  echo "venv/ensurepip unavailable" >&2\n'
        "  exit 86\n"
        "fi\n"
        f'exec {shlex.quote(real_python)} "$@"\n'
    )
    python_without_venv.chmod(0o755)
    for module in ("venv", "ensurepip"):
        unavailable = subprocess.run(
            [python_without_venv, "-m", module, tmp_path / "must-not-exist"],
            capture_output=True,
            text=True,
        )
        assert unavailable.returncode == 86
        assert "venv/ensurepip unavailable" in unavailable.stderr

    parity_root = tmp_path / "parity"
    parity_root.mkdir()
    result = demo_live_ingress_matrix.parity(
        parity_root, python_executable=str(python_without_venv)
    )

    assert result["commit_sha"]
    assert result["feature_probes"] == [
        "work-loop-baml-required",
        "role-baml-required",
    ]
    assert result["install_method"].startswith("manual isolated bin/site")
    assert not (tmp_path / "must-not-exist").exists()
