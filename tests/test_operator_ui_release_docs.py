"""Pin the cross-repo UI release handoff without importing the UI toolchain.

The real browser proof belongs to beadhive-ui and is manual. These fast tests guard the local
delegate, installed launch commands, evidence provenance, and narrow phase-one exception where
they can drift in this repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "justfile"
DOC = ROOT / "docs" / "OPERATOR-UI.md"
ADR = ROOT / "docs" / "design" / "unified-host-daemon-adr.md"
PROOF = ROOT / "docs" / "proof" / "operator-loopback-ui-release-2026-08-25.md"

CORE_COMMAND = (
    "BH_OPERATOR_UI_ORIGIN=http://127.0.0.1:3000 bh host daemon serve --host 127.0.0.1 --port 8420"
)
UI_COMMAND = (
    "BH_OPERATOR_UI_ORIGIN=http://127.0.0.1:3000 beadhive-operator-ui "
    "--host 127.0.0.1 --port 3000 --daemon-url http://127.0.0.1:8420"
)


def _recipe_header(text: str, name: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"{name}:"))


def test_installed_launch_commands_and_phase_one_boundary_are_pinned():
    text = DOC.read_text()

    assert CORE_COMMAND in text
    assert UI_COMMAND in text
    for boundary in (
        "loopback-only, unauthenticated, read-only",
        "MCP over HTTP",
        "activity `POST`",
        "terminal WebSockets",
        "non-loopback listener",
        "bh-xw03t",
        "Node relay",
    ):
        assert boundary in text


def test_adr_records_a_narrow_exception_without_erasing_phase_two_auth():
    text = ADR.read_text()
    amendment = text.split("## Amendment — 2026-08-25: phase-one local read-only UI exception", 1)[
        1
    ]

    assert "literal `127.0.0.1`" in amendment
    assert "one exact configured literal-loopback browser origin" in amendment
    assert "Authentication and authorization remain mandatory phase-two prerequisites" in amendment
    assert "bh-xw03t" in amendment
    assert "does not authorize" in amendment


def test_proof_pins_exact_cross_repo_sources_result_and_cleanup():
    text = PROOF.read_text()

    for value in (
        "038df72459140330624bffc637d32bfcdc8005c4",
        "9dccd355eff8488649bbe81f62e395137c685ea0",
        "aee7df29afd2291d01d24296ea8b724207788c4c",
        "0f450299dfc4a97f35b0e46fb82b9dfc27082ec5",
        "pass 1",
        "fail 0",
        "asserted no\ndescendant remained",
    ):
        assert value in text


def test_cross_repo_proof_stays_out_of_routine_gates():
    text = JUSTFILE.read_text()

    assert "check-operator-release" not in _recipe_header(text, "check")
    assert "check-operator-release" not in _recipe_header(text, "check-all")
    assert "check-operator-release ui_repo=" in text
    assert '--working-directory "$ui_repo"' in text


needs_just_and_git = pytest.mark.skipif(
    shutil.which("just") is None or shutil.which("git") is None,
    reason="needs just and git",
)


def _make_clean_ui_repo(tmp_path: Path) -> Path:
    ui = tmp_path / "beadhive-ui"
    ui.mkdir()
    (ui / "justfile").write_text("default:\n    @true\n")
    subprocess.run(["git", "init", "-q", str(ui)], check=True)
    subprocess.run(["git", "-C", str(ui), "add", "justfile"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(ui),
            "-c",
            "user.name=Release Proof",
            "-c",
            "user.email=proof@example.invalid",
            "commit",
            "-qm",
            "test: seed ui",
        ],
        check=True,
    )
    return ui


def _run_delegate(tmp_path: Path, ui: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    real_just = shutil.which("just")
    assert real_just is not None
    capture = tmp_path / "inner-just-argv"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "just"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{capture}"\n')
    stub.chmod(0o755)
    result = subprocess.run(
        [real_just, "-f", str(JUSTFILE), "check-operator-release", str(ui)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"},
        check=False,
    )
    return result, capture


@needs_just_and_git
def test_manual_recipe_delegates_to_the_clean_ui_checkout(tmp_path):
    ui = _make_clean_ui_repo(tmp_path)

    result, capture = _run_delegate(tmp_path, ui)

    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines() == [
        "--justfile",
        str(ui / "justfile"),
        "--working-directory",
        str(ui),
        "check-operator-release",
        str(ROOT),
    ]


@needs_just_and_git
def test_manual_recipe_refuses_a_dirty_ui_checkout_before_delegating(tmp_path):
    ui = _make_clean_ui_repo(tmp_path)
    (ui / "dirty.txt").write_text("uncommitted\n")

    result, capture = _run_delegate(tmp_path, ui)

    assert result.returncode == 2
    assert "must be clean" in result.stderr
    assert not capture.exists()
