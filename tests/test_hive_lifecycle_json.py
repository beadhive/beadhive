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


def test_cli_renders_human_and_json_from_the_same_silent_onboard_result(monkeypatch) -> None:
    from beadhive import hive_services
    from beadhive.modules.hives import OnboardCheck, OnboardHiveResult

    class SilentService:
        def onboard(self, request):
            return OnboardHiveResult(
                request.identity,
                "/workspace/github/acme/repo",
                cloned=False,
                registered=True,
                prefix="acme-repo",
                synced=False,
                kind="org-native",
                dry_run=request.dry_run,
                checks=(OnboardCheck("identity", "workspace identity", True, "matched", True),),
                steps=("assess",),
            )

    monkeypatch.setattr(hive_services, "hive_lifecycle_service", lambda **_kwargs: SilentService())
    human = runner.invoke(app, ["hive", "onboard", "github/acme/repo", "--dry-run"])
    onboard_result = runner.invoke(
        app, ["hive", "onboard", "github/acme/repo", "--dry-run", "--json"]
    )
    assert human.exit_code == 0, human.output
    assert onboard_result.exit_code == 0, onboard_result.output
    payload = json.loads(onboard_result.stdout)
    assert human.stdout == (
        "DRY-RUN onboard /workspace/github/acme/repo\n"
        "  ✓ identity (workspace identity)  matched\n"
        "  • would run assess\n"
    )
    assert payload["text"] == human.stdout
    assert payload["success"] is True

    ready_args = {}
    monkeypatch.setattr(
        "beadhive.hive_ready.run_check",
        lambda verbose, *, as_json=False: ready_args.update(verbose=verbose, as_json=as_json),
    )
    ready_result = runner.invoke(app, ["hive", "ready", "--json"])
    assert ready_result.exit_code == 0, ready_result.output
    assert ready_args == {"verbose": False, "as_json": True}


def test_cli_onboard_json_captures_invalid_triplet_before_service_construction() -> None:
    result = runner.invoke(app, ["hive", "onboard", "acme/widget", "--json"])

    assert result.exit_code == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    _validate("bh-hive-onboard-v1.schema.json", payload)
    assert payload["success"] is False
    assert payload["exit_code"] == 1
    assert payload["hive"] == "acme/widget"
    assert payload["target"] == ""
    assert payload["text"] == "✗ expected a provider/org/repo triplet, got 'acme/widget'\n"


def test_cli_onboard_json_normalizes_executor_typer_exit(monkeypatch) -> None:
    from beadhive import hive_services

    class ExitingService:
        def onboard(self, _request):
            typer.echo("✗ executor refused", err=True)
            raise typer.Exit(7)

    monkeypatch.setattr(hive_services, "hive_lifecycle_service", lambda **_kwargs: ExitingService())

    result = runner.invoke(app, ["hive", "onboard", "github/acme/repo", "--json"])

    assert result.exit_code == 7
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    _validate("bh-hive-onboard-v1.schema.json", payload)
    assert payload["success"] is False
    assert payload["exit_code"] == 7
    assert payload["hive"] == "github/acme/repo"
    assert payload["text"] == "✗ executor refused\n"


@pytest.mark.parametrize(
    ("hive_id", "diagnostic"),
    (
        (
            "github/ac me/repo",
            "organization must be a canonical hive identity segment",
        ),
        ("github/./repo", "organization must be a canonical hive identity segment"),
        ("github/acme/..", "repository must be a canonical hive identity segment"),
    ),
)
@pytest.mark.parametrize("as_json", (False, True), ids=("human", "json"))
def test_cli_onboard_normalizes_canonical_identity_validation_only_for_json(
    hive_id, diagnostic, as_json
) -> None:
    """Human mode keeps its historical exception-only contract; JSON translates it."""

    args = ["hive", "onboard", hive_id]
    if as_json:
        args.append("--json")

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert result.stderr == ""
    if not as_json:
        assert result.stdout == ""
        assert isinstance(result.exception, ValueError)
        assert str(result.exception) == diagnostic
        return

    payload = json.loads(result.stdout)
    _validate("bh-hive-onboard-v1.schema.json", payload)
    assert payload["success"] is False
    assert payload["exit_code"] == 1
    assert payload["hive"] == hive_id
    assert payload["target"] == ""
    assert payload["text"] == f"✗ invalid hive identity: {diagnostic}\n"


@pytest.mark.parametrize(
    ("flags", "diagnostic"),
    (
        (
            ("--claude", "--skills"),
            "✗ --claude --skills conflict: in plugin mode the agf plugin already vends "
            "skills — drop --skills (or set claude.source: copy in ~/.beadhive/config.yaml to "
            "use the legacy copy path).\n",
        ),
        (
            ("--global",),
            "✗ --global needs --claude and/or --codex — it's a modifier on those grants, not "
            "a standalone flag.\n",
        ),
    ),
)
@pytest.mark.parametrize("as_json", (False, True), ids=("human", "json"))
def test_cli_onboard_guards_preserve_human_errors_and_normalize_json(
    monkeypatch, tmp_path, flags, diagnostic, as_json
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("schema_version: 1\nclaude:\n  source: plugin\n", encoding="utf-8")
    monkeypatch.setenv("BH_CONFIG", str(config_path))
    args = ["hive", "onboard", "github/acme/repo", *flags]
    if as_json:
        args.append("--json")

    result = runner.invoke(app, args)

    assert result.exit_code == 1
    if not as_json:
        assert result.stdout == ""
        assert result.stderr == diagnostic
        return

    assert result.stderr == ""
    payload = json.loads(result.stdout)
    _validate("bh-hive-onboard-v1.schema.json", payload)
    assert payload["success"] is False
    assert payload["exit_code"] == 1
    assert payload["hive"] == "github/acme/repo"
    assert payload["target"] == ""
    assert payload["text"] == diagnostic


def test_cli_onboard_captures_and_normalizes_the_entire_execution_path() -> None:
    import ast
    import inspect
    import textwrap

    from beadhive import cli, hive_services
    from beadhive.modules.hives import HiveIdentity, OnboardHiveRequest

    tree = ast.parse(textwrap.dedent(inspect.getsource(cli.hive_onboard)))
    function = tree.body[0]
    capture = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Name) and item.context_expr.id == "capture"
            for item in node.items
        )
    )

    def dotted_name(node) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{dotted_name(node.value)}.{node.attr}"
        if isinstance(node, ast.Call):
            return f"{dotted_name(node.func)}()"
        return ""

    execution_calls = {
        dotted_name(node.func): node for node in ast.walk(function) if isinstance(node, ast.Call)
    }
    captured_node_ids = {id(node) for node in ast.walk(capture)}
    required_execution = {
        "_reject_claude_skills_conflict_in_plugin_mode",
        "_reject_global_without_claude_or_codex",
        "hive_services.identity_from_legacy_triplet",
        "OnboardHiveRequest",
        "hive_services.hive_lifecycle_service().onboard",
        "_render_onboard_result",
        "typer.Exit",
    }
    handled = {
        dotted_name(exception)
        for handler in ast.walk(capture)
        if isinstance(handler, ast.ExceptHandler) and handler.type is not None
        for exception in (
            handler.type.elts if isinstance(handler.type, ast.Tuple) else (handler.type,)
        )
    }
    identity_adapter = ast.parse(
        textwrap.dedent(inspect.getsource(hive_services.identity_from_legacy_triplet))
    )
    identity_calls = {
        dotted_name(node.func) for node in ast.walk(identity_adapter) if isinstance(node, ast.Call)
    }
    validation_raises = {
        dotted_name(node.exc.func)
        for validation_type in (HiveIdentity, OnboardHiveRequest)
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(validation_type))))
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
    }

    assert required_execution <= execution_calls.keys()
    assert all(id(execution_calls[name]) in captured_node_ids for name in required_execution)
    assert {"hive._parse_triplet", "HiveIdentity"} <= identity_calls
    assert validation_raises == {"ValueError"}
    assert validation_raises <= handled
    assert handled == {"typer.Exit", "ValueError"}


@pytest.mark.parametrize(
    ("command", "scope", "action"),
    (("retire", "fleet", "retire"), ("reclaim", "host", "reclaim")),
)
def test_cli_renders_semantic_retirement_events_from_a_silent_service(
    monkeypatch, command, scope, action
) -> None:
    from beadhive import hive_services
    from beadhive.modules.hives import RetireEvent, RetireHiveResult, RetireScope

    class SilentService:
        def retire(self, request):
            assert request.scope is RetireScope(scope)
            return RetireHiveResult(
                request.hive_id,
                request.scope,
                "/workspace/github/acme/repo",
                request.dry_run,
                unregistered=False,
                events=(
                    RetireEvent("operation", {"identity": "github/acme/repo"}),
                    RetireEvent("complete"),
                ),
            )

    monkeypatch.setattr(hive_services, "hive_lifecycle_service", lambda **_kwargs: SilentService())

    result = runner.invoke(app, ["hive", command, "github/acme/repo", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f"DRY-RUN {action} github/acme/repo\n✓ dry-run complete — nothing changed\n"
    )


def test_retirement_events_do_not_carry_presentation_text() -> None:
    from beadhive.modules.hives import RetireEvent

    event = RetireEvent("archive", {"source": "/old", "destination": "/new"})

    assert event.code == "archive"
    assert event.facts == {"source": "/old", "destination": "/new"}
    assert not hasattr(event, "detail")
    assert not hasattr(event, "text")
    assert not hasattr(event, "line")
