from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from beadhive import bd, engine


def test_invoke_owns_hive_actor_capture_env_and_default_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bd,
        "_run",
        lambda cmd, **kwargs: (
            calls.append((cmd, kwargs)) or subprocess.CompletedProcess(cmd, 0, "ok", "")
        ),
    )

    result = engine.BdEngine().invoke(
        ["show", "bh-1"],
        cwd="/hive",
        actor="dev/a",
        capture=True,
        env={"X": "1"},
        pin_process_cwd=True,
    )

    assert result.returncode == 0
    assert calls == [
        (
            ["bd", "-C", "/hive", "--actor", "dev/a", "show", "bh-1"],
            {
                "check": False,
                "capture": True,
                "timeout": engine.STATE_TIMEOUT,
                "cwd": "/hive",
                "env": {"X": "1"},
            },
        )
    ]


def test_invoke_streams_ambient_bd_and_turns_timeout_into_exit_124(monkeypatch):
    def expire(cmd, **kwargs):
        assert cmd == ["bd", "--version"]
        assert kwargs["capture"] is False
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(bd, "_run", expire)

    result = engine.BdEngine().invoke(["--version"], hive_aware=False)

    assert result.returncode == 124
    assert "timed out after 120s" in result.stderr


def test_invoke_promotes_exit_zero_failure_report(monkeypatch):
    monkeypatch.setattr(
        bd,
        "_run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, "failed to import from /hive-a", ""
        ),
    )

    result = engine.BdEngine().invoke(
        ["repo", "sync"],
        cwd="/hub",
        capture=True,
        no_work_markers=("failed to import from /hive-a",),
    )

    assert result.returncode == 1
    assert result.bd_no_work is True
    assert "Error: bd exited 0 but reported failure" in result.stderr


def test_source_has_no_unjustified_raw_bd_invocations():
    root = Path(__file__).parents[1] / "src" / "beadhive"
    found: list[tuple[str, str]] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
            doc = ast.get_docstring(function) or ""
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                name = (
                    call.func.id
                    if isinstance(call.func, ast.Name)
                    else call.func.attr
                    if isinstance(call.func, ast.Attribute)
                    else ""
                )
                if name not in {"run", "_run", "run_bounded"}:
                    continue
                if not call.args or not isinstance(call.args[0], (ast.List, ast.Tuple)):
                    continue
                items = call.args[0].elts
                if not items or not isinstance(items[0], ast.Constant) or items[0].value != "bd":
                    continue
                found.append((path.name, function.name))
                assert "bd-seam-justified" in doc

    assert sorted(found) == [
        ("dolt_health.py", "_read_local_bd_version_string"),
        ("dolt_health.py", "_scratch_probe_local_version"),
        ("dolt_health.py", "probe_server_schema_version"),
        ("fleet.py", "sql"),
        ("hub.py", "bounded_bd"),
        ("registry.py", "report"),
        ("safety.py", "_bd_dolt_status_payload"),
        ("safety.py", "_bd_has_dolt_remote"),
        ("validate.py", "_issues_and_problems"),
    ]
