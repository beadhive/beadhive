"""Compatibility contracts for the large modules targeted by the structural program.

These are deliberately facade tests.  They prove that callers can keep importing from the
original modules and that the collaborator names tests patch there are looked up at runtime.
An extraction may move implementations, but it must preserve these seams (or migrate them in an
equally explicit compatibility change).
"""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from beadhive import (
    config,
    work,
    work_guards,
    work_intake,
    work_metrics,
    work_reads,
    worktree,
)


def test_work_extraction_compatibility_matrix_keeps_historical_imports():
    """Every moved family remains importable from ``beadhive.work`` by its historical name."""
    matrix = {
        "metrics/events": {
            "_emit_bead_flow": work_metrics.emit_bead_flow,
            "_emit_cycle": work_metrics.emit_cycle,
            "_flow_events": work_metrics.flow_events,
            "dispatch_cause_count": work_metrics.dispatch_cause_count,
            "record_dispatch_failure": work_metrics.record_dispatch_failure,
        },
        "shared guards": {
            "_first": work_guards.first,
            "_is_epic": work_guards.is_epic,
            "_guard_seat": work_guards.guard_seat,
            "_guard_conventions": work_guards.guard_conventions,
            "_print_brief": work_guards.print_brief,
        },
        "reads/readiness": {
            "_forward_read": work_reads.forward_read,
            "_widen_narrowed_ready_args": work_reads.widen_narrowed_ready_args,
            "molecule_readiness_payload": work_reads.molecule_readiness_payload,
        },
        "intake": {"_render_disposition": work_intake.render_disposition},
    }

    for family in matrix.values():
        for historical_name, implementation in family.items():
            assert getattr(work, historical_name) is implementation

    for command_name in (
        "brief",
        "readiness",
        "ready",
        "issue",
        "list_",
        "intake_cmd",
        "accept_cmd",
        "reject_cmd",
        "reroute_cmd",
        "promote_cmd",
    ):
        assert callable(getattr(work, command_name))


def test_flow_metrics_keep_the_historical_dotted_event_stream(monkeypatch, tmp_path):
    """Metrics consume the complete dotted-id history even when events are closed and detached."""
    event = {"id": "bh-1.event", "issue_type": "event", "status": "closed"}
    calls = []
    monkeypatch.setattr(
        work_metrics.bd,
        "json",
        lambda args, cwd: calls.append((args, cwd)) or [event],
    )

    assert work_metrics.flow_events("bh-1", tmp_path) == [event]
    assert calls == [
        (
            ["list", "--parent", "bh-1", "--limit", "0", "--include-infra", "--all"],
            tmp_path,
        )
    ]


def test_work_issue_facade_executes_the_module_local_bd_patch_point(monkeypatch):
    calls = []

    def fake_bd(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='[{"id": "bh-contract", "status": "open"}]\n',
            stderr="",
        )

    monkeypatch.setattr(work.bd, "_run", fake_bd)
    monkeypatch.setattr(work.config, "load", lambda: {"sentinel": "effective-config"})
    monkeypatch.setattr(
        work.registry,
        "hive_dir_for",
        lambda cfg, hive: "/facade/hive" if cfg["sentinel"] and hive == "" else None,
    )

    result = CliRunner().invoke(work.app, ["issue", "bh-contract", "--json"])

    assert result.exit_code == 0
    assert result.stdout == '[{"id": "bh-contract", "status": "open"}]\n'
    assert calls[0][0] == ["bd", "-C", "/facade/hive", "show", "bh-contract", "--json"]
    assert calls[0][1]["capture"] is True


def test_config_load_facade_executes_layer_patch_points_and_preserves_precedence(monkeypatch):
    calls = []
    monkeypatch.setattr(
        config,
        "load_fleet",
        lambda: calls.append("fleet") or {"work": {"validate_cmd": "fleet"}},
    )
    monkeypatch.setattr(
        config,
        "load_host",
        lambda: calls.append("host") or {"otel": {"hive": "local"}},
    )
    monkeypatch.setattr(
        config,
        "_reject_fleet_overrides",
        lambda host: calls.append(("guard", dict(host))),
    )

    effective = config.load()

    assert effective == {"work": {"validate_cmd": "fleet"}, "otel": {"hive": "local"}}
    # HOST selects SQL before any local fleet access; Git still reaches the
    # historical load_fleet and override-guard facade patch points.
    assert calls == ["host", "fleet", ("guard", {"otel": {"hive": "local"}})]


def test_worktree_run_init_facade_executes_the_module_local_runner(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worktree, "run", fake_run)

    worktree.run_init(
        {"worktrees": {"init": [{"run": "tool --flag value", "verify": True}]}},
        {},
        tmp_path,
        verify_only=True,
    )

    assert calls == [(["tool", "--flag", "value"], {"cwd": str(tmp_path), "check": False})]


def test_worktree_pid_start_uses_its_documented_subprocess_patch_point(monkeypatch):
    monkeypatch.setattr(
        worktree,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("wrong patch point")),
    )
    monkeypatch.setattr(
        worktree.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout=" start-token \n"),
    )

    assert worktree._pid_start(4321) == "start-token"
