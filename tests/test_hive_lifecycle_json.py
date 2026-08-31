"""Machine contracts for ``hive onboard`` and ``hive ready``.

The JSON ``text`` field is compared directly with the no-flag rendering.  That pins the legacy
human bytes while proving the machine view carries bh's wording and preserves exit semantics.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import typer
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from beadhive import config, hive, hive_ready, onboard, registry
from beadhive.cli import app

ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / "docs" / "schemas" / "wire" / "v1.1.0"
runner = CliRunner()


def _validate(schema_name: str, payload: dict) -> None:
    schema = json.loads((WIRE / schema_name).read_text())
    Draft202012Validator(schema).validate(payload)


def _exit(call) -> int:
    with pytest.raises(typer.Exit) as exc:
        call()
    return exc.value.exit_code


def _stub_ready(monkeypatch, checks: list[hive_ready.Check]) -> None:
    monkeypatch.setattr(config, "load", lambda: {})
    monkeypatch.setattr(
        hive_ready, "workspace_identity", lambda cwd=None: ("github", "acme", "repo")
    )
    monkeypatch.setattr(
        registry,
        "find_entry",
        lambda cfg, provider, org, repo: {"prefix": "ac", "kind": "org-native"},
    )
    monkeypatch.setattr(hive_ready, "_repo_root", lambda cwd=None: Path("/repo"))
    monkeypatch.setattr(hive_ready, "scan", lambda cfg, ident, entry, root: checks)


def test_hive_ready_json_validates_and_preserves_ready_human_bytes_and_exit(
    monkeypatch, capsys
) -> None:
    _stub_ready(
        monkeypatch,
        [
            hive_ready.Check("hive registered", True, "ok", "prefix=ac kind=org-native"),
            hive_ready.Check("optional adapter", False, "warn", "temporarily unavailable"),
        ],
    )

    assert _exit(lambda: hive_ready.run_check(verbose=True)) == 0
    human = capsys.readouterr()
    assert human.err == ""
    assert human.out == (
        "# Required\n"
        "  ✓ hive registered     prefix=ac kind=org-native\n"
        "\n# Optional\n"
        "  ! optional adapter    temporarily unavailable\n"
        "\n✓ hive 'ac' ready for AGF.\n"
    )

    assert _exit(lambda: hive_ready.run_check(verbose=True, as_json=True)) == 0
    machine = capsys.readouterr()
    payload = json.loads(machine.out)
    assert machine.err == ""
    _validate("bh-hive-ready-v1.schema.json", payload)
    assert payload["text"] == human.out
    assert payload["ready"] is True
    assert payload["exit_code"] == 0
    assert payload["checks"][1]["text"] == "  ! optional adapter    temporarily unavailable"


def test_hive_ready_json_preserves_not_ready_exit_and_human_text(monkeypatch, capsys) -> None:
    _stub_ready(
        monkeypatch,
        [hive_ready.Check("beads initialized", True, "missing", "missing — `bh hive init`")],
    )

    assert _exit(lambda: hive_ready.run_check()) == 1
    human = capsys.readouterr()
    assert human.out == (
        "✗ hive 'ac' not ready for AGF — 1 required check(s) failed (run -v for the breakdown)\n"
    )

    assert _exit(lambda: hive_ready.run_check(as_json=True)) == 1
    payload = json.loads(capsys.readouterr().out)
    _validate("bh-hive-ready-v1.schema.json", payload)
    assert payload["text"] == human.out
    assert payload["ready"] is False
    assert payload["exit_code"] == 1


def test_hive_ready_outside_workspace_keeps_stderr_human_contract(monkeypatch, capsys) -> None:
    monkeypatch.setattr(config, "load", lambda: {})
    monkeypatch.setattr(hive_ready, "workspace_identity", lambda cwd=None: None)

    assert _exit(lambda: hive_ready.run_check()) == 1
    human = capsys.readouterr()
    assert human.out == ""
    assert human.err == "✗ not in a git repo under $GIT_WORKSPACE — not an AGF hive.\n"

    assert _exit(lambda: hive_ready.run_check(as_json=True)) == 1
    machine = capsys.readouterr()
    payload = json.loads(machine.out)
    _validate("bh-hive-ready-v1.schema.json", payload)
    assert machine.err == ""
    assert payload["text"] == human.err
    assert payload["stream"] == "stderr"


def _stub_onboard(monkeypatch, tmp_path: Path, *, fail: bool = False) -> None:
    monkeypatch.setattr(hive, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(config, "load", lambda: {})

    def build_steps(ctx):
        check = onboard.Check(
            "identity",
            "workspace identity",
            True,
            lambda current: (not fail, "matched" if not fail else "not matched"),
        )
        return [
            onboard.Step(
                "assess",
                "assess",
                lambda current: typer.echo("• action transcript"),
                checks=[check],
            )
        ]

    monkeypatch.setattr(onboard, "build_steps", build_steps)


def test_hive_onboard_json_validates_and_carries_exact_human_transcript(
    monkeypatch, tmp_path, capsys
) -> None:
    _stub_onboard(monkeypatch, tmp_path)

    hive.onboard("github/acme/repo", dry_run=True)
    human = capsys.readouterr()
    target = tmp_path / "github" / "acme" / "repo"
    assert human.err == ""
    assert human.out == (
        "• action transcript\n"
        f"DRY-RUN onboard {target}\n"
        "  ✓ identity (workspace identity)  matched\n"
        "  • would run assess\n"
    )

    hive.onboard("github/acme/repo", dry_run=True, as_json=True)
    machine = capsys.readouterr()
    payload = json.loads(machine.out)
    assert machine.err == ""
    _validate("bh-hive-onboard-v1.schema.json", payload)
    assert payload["text"] == human.out
    assert payload["success"] is True
    assert payload["exit_code"] == 0
    assert payload["checks"][0]["text"] == "  ✓ identity (workspace identity)  matched"


def test_hive_onboard_json_preserves_preflight_failure_exit_and_text(
    monkeypatch, tmp_path, capsys
) -> None:
    _stub_onboard(monkeypatch, tmp_path, fail=True)

    assert _exit(lambda: hive.onboard("github/acme/repo", dry_run=True)) == 1
    human = capsys.readouterr()
    assert human.out == ""
    assert human.err == (
        "✗ onboarding preflight failed:\n"
        "  ✗ identity: not matched\n"
        "  override with --skip-check identity\n"
    )

    assert _exit(lambda: hive.onboard("github/acme/repo", dry_run=True, as_json=True)) == 1
    machine = capsys.readouterr()
    payload = json.loads(machine.out)
    assert machine.err == ""
    _validate("bh-hive-onboard-v1.schema.json", payload)
    assert payload["text"] == human.err
    assert payload["success"] is False
    assert payload["exit_code"] == 1
    assert payload["checks"][0]["ok"] is False


def test_hive_onboard_json_captures_inherited_subprocess_streams(
    monkeypatch, tmp_path, capfd
) -> None:
    """The bd mint seam inherits process fds; Python-only redirection would corrupt JSON."""
    monkeypatch.setattr(hive, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(config, "load", lambda: {})

    def inherited_output(current) -> None:
        os.write(1, b"bd stdout verbatim\n")
        os.write(2, b"bd stderr verbatim\n")

    monkeypatch.setattr(
        onboard,
        "build_steps",
        lambda ctx: [onboard.Step("mint", "mint", inherited_output)],
    )

    hive.onboard("github/acme/repo", dry_run=True, as_json=True)
    machine = capfd.readouterr()
    payload = json.loads(machine.out)
    _validate("bh-hive-onboard-v1.schema.json", payload)
    assert machine.err == ""
    assert "bd stdout verbatim\nbd stderr verbatim\n" in payload["text"]


def test_cli_accepts_and_forwards_both_json_flags(monkeypatch) -> None:
    onboard_args = {}
    monkeypatch.setattr(
        hive, "onboard", lambda hive_id, **kwargs: onboard_args.update(hive_id=hive_id, **kwargs)
    )
    onboard_result = runner.invoke(
        app, ["hive", "onboard", "github/acme/repo", "--dry-run", "--json"]
    )
    assert onboard_result.exit_code == 0, onboard_result.output
    assert onboard_args["as_json"] is True

    ready_args = {}
    monkeypatch.setattr(
        "beadhive.hive_ready.run_check",
        lambda verbose, *, as_json=False: ready_args.update(verbose=verbose, as_json=as_json),
    )
    ready_result = runner.invoke(app, ["hive", "ready", "--json"])
    assert ready_result.exit_code == 0, ready_result.output
    assert ready_args == {"verbose": False, "as_json": True}
