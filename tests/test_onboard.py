"""Unit tests for ws.onboard — the tiny Check/Step/OnboardPlan core + run_onboard.

Exercises the ENGINE only, with fake steps/checks (no real git/bd). Asserts on the returned
``OnboardPlan`` object, never on stdout (the retire pattern).
"""

from __future__ import annotations

import pytest
import typer

from beadhive.onboard import (
    Check,
    Ctx,
    OnboardPlan,
    Step,
    _topo_order,
    run_onboard,
)

# ---------------------------------------------------------------------------
# Helpers — fake checks/steps with mutation counters
# ---------------------------------------------------------------------------


def _ok_check(cid: str, *, overridable: bool = True, applies=lambda c: True) -> Check:
    return Check(cid, cid, overridable, lambda c: (True, "ok"), applies)


def _fail_check(cid: str, *, overridable: bool = True, applies=lambda c: True) -> Check:
    return Check(cid, cid, overridable, lambda c: (False, f"{cid} failed"), applies)


def _rec_step(cid: str, log: list[str], *, requires=None, mutates=False, checks=None,
              enabled=lambda c: True, preflight=False) -> Step:
    """A step whose action appends its id to ``log`` when it runs."""
    return Step(
        id=cid,
        label=cid,
        action=lambda c, _cid=cid: log.append(_cid),
        requires=list(requires or []),
        mutates=mutates,
        checks=list(checks or []),
        enabled=enabled,
        preflight=preflight,
    )


def _ctx(steps: list[Step]) -> Ctx:
    return Ctx(hive="p/o/r", target="/tmp/p/o/r", steps=steps)


# ---------------------------------------------------------------------------
# (e) topological execution order
# ---------------------------------------------------------------------------


def test_topo_order_respects_requires() -> None:
    log: list[str] = []
    steps = [
        _rec_step("c", log, requires=["b"]),
        _rec_step("a", log),
        _rec_step("b", log, requires=["a"]),
    ]
    plan = run_onboard(_ctx(steps))
    assert plan.steps_run == ["a", "b", "c"]
    assert log == ["a", "b", "c"]


def test_topo_order_ignores_missing_requires_from_disabled_steps() -> None:
    """A require pointing at a disabled/absent step must not deadlock the sort."""
    log: list[str] = []
    steps = [
        _rec_step("run-me", log, requires=["disabled"], enabled=lambda c: True),
        _rec_step("disabled", log, enabled=lambda c: False),
    ]
    plan = run_onboard(_ctx(steps))
    assert plan.steps_run == ["run-me"]
    assert log == ["run-me"]


def test_topo_order_raises_on_cycle() -> None:
    a = _rec_step("a", [], requires=["b"])
    b = _rec_step("b", [], requires=["a"])
    with pytest.raises(ValueError, match="cycle"):
        _topo_order([a, b])


# ---------------------------------------------------------------------------
# (a) batch preflight fast-fail — report ALL failures, mutate nothing
# ---------------------------------------------------------------------------


def test_preflight_reports_every_failure_before_any_mutation() -> None:
    log: list[str] = []
    steps = [
        _rec_step("assess", log, checks=[_fail_check("chk-1"), _fail_check("chk-2")]),
        _rec_step("mutate", log, requires=["assess"], mutates=True),
    ]
    with pytest.raises(typer.Exit) as exc:
        run_onboard(_ctx(steps))
    assert exc.value.exit_code == 1
    # No action ran — the gate fired before Phase B.
    assert log == []


def test_preflight_pass_runs_all_steps() -> None:
    log: list[str] = []
    steps = [
        _rec_step("assess", log, checks=[_ok_check("chk-1")]),
        _rec_step("mutate", log, requires=["assess"], mutates=True),
    ]
    plan = run_onboard(_ctx(steps))
    assert log == ["assess", "mutate"]
    assert [r.id for r in plan.checks] == ["chk-1"]
    assert all(r.ok for r in plan.checks)


# ---------------------------------------------------------------------------
# (b) skip_checks downgrades overridable failures to warnings
# ---------------------------------------------------------------------------


def test_skip_check_downgrades_overridable_failure_to_warning() -> None:
    log: list[str] = []
    steps = [
        _rec_step("assess", log, checks=[_fail_check("dirty-tree", overridable=True)]),
        _rec_step("mutate", log, requires=["assess"], mutates=True),
    ]
    plan = run_onboard(_ctx(steps), skip_checks=["dirty-tree"])
    # Downgraded → proceeds; recorded as skipped, not a hard failure.
    assert plan.skipped_checks == ["dirty-tree"]
    assert log == ["assess", "mutate"]
    res = next(r for r in plan.checks if r.id == "dirty-tree")
    assert res.skipped is True
    assert res.ok is False
    assert res.glyph == "⚠"


# ---------------------------------------------------------------------------
# (c) non-overridable failure is NEVER bypassed
# ---------------------------------------------------------------------------


def test_non_overridable_failure_never_bypassed_even_if_skipped() -> None:
    log: list[str] = []
    steps = [
        _rec_step("assess", log, checks=[_fail_check("prefix-policy", overridable=False)]),
        _rec_step("mutate", log, requires=["assess"], mutates=True),
    ]
    with pytest.raises(typer.Exit):
        run_onboard(_ctx(steps), skip_checks=["prefix-policy"])
    assert log == []


# ---------------------------------------------------------------------------
# (d) dry_run runs zero mutating actions, still returns a populated plan
# ---------------------------------------------------------------------------


def test_dry_run_skips_mutations_runs_assessment_and_populates_plan() -> None:
    mutated: list[str] = []
    assessed: list[str] = []
    steps = [
        _rec_step("assess", assessed, checks=[_ok_check("chk-1")]),
        _rec_step("mutate", mutated, requires=["assess"], mutates=True),
    ]
    plan = run_onboard(_ctx(steps), dry_run=True)
    # Mutating action did NOT run; read-only assessment action DID run.
    assert mutated == []
    assert assessed == ["assess"]
    # Plan is fully populated (both steps in the previewed order).
    assert plan.dry_run is True
    assert plan.steps_run == ["assess", "mutate"]
    assert [r.id for r in plan.checks] == ["chk-1"]


# ---------------------------------------------------------------------------
# clone / preflight carve-out: two batches split by the acquire step
# ---------------------------------------------------------------------------


def test_preflight_step_action_runs_mid_preflight_and_sets_cloned() -> None:
    log: list[str] = []
    steps = [
        _rec_step("resolve", log, checks=[_ok_check("valid-triplet")]),
        _rec_step("clone", log, requires=["resolve"], mutates=True, preflight=True,
                  checks=[_ok_check("url-present")]),
        _rec_step("bd-init", log, requires=["clone"], mutates=True),
    ]
    plan = run_onboard(_ctx(steps))
    assert plan.cloned is True
    # The clone (acquire) action runs during Phase A; every other action runs in Phase B in
    # topo order. So the acquire fires first, then the remaining steps.
    assert log == ["clone", "resolve", "bd-init"]
    # steps_run still records the full plan in topological order.
    assert plan.steps_run == ["resolve", "clone", "bd-init"]


def test_repo_level_check_applies_gated_on_cloned() -> None:
    """A dirty-tree-style check that applies only when we did NOT just clone is skipped
    (never evaluated) once the acquire step has cloned."""
    evaluated: list[str] = []

    def dirty_fn(c):
        evaluated.append("dirty")
        return (False, "dirty")

    dirty = Check("dirty-tree", "dirty-tree", True, dirty_fn, applies=lambda c: not c.cloned)
    steps = [
        _rec_step("clone", [], mutates=True, preflight=True),
        _rec_step("worktree-clean", [], requires=["clone"], checks=[dirty]),
    ]
    plan = run_onboard(_ctx(steps))
    # Fresh clone → dirty-tree N/A → never evaluated, so no hard failure.
    assert evaluated == []
    assert plan.cloned is True
    assert all(r.id != "dirty-tree" for r in plan.checks)


def test_existing_folder_dirty_check_fires_before_bd_init() -> None:
    """No clone step enabled → single batch; a dirty-tree failure gates before bd-init."""
    log: list[str] = []
    dirty = Check("dirty-tree", "dirty-tree", True, lambda c: (False, "dirty"),
                  applies=lambda c: not c.cloned)
    steps = [
        _rec_step("worktree-clean", log, checks=[dirty]),
        _rec_step("bd-init", log, requires=["worktree-clean"], mutates=True),
    ]
    with pytest.raises(typer.Exit):
        run_onboard(_ctx(steps))
    assert log == []  # bd-init never reached


# ---------------------------------------------------------------------------
# plan shape
# ---------------------------------------------------------------------------


def test_onboard_plan_is_dataclass_with_expected_defaults() -> None:
    plan = OnboardPlan(hive="p/o/r", target="/t", dry_run=False)
    assert plan.cloned is False
    assert plan.checks == []
    assert plan.skipped_checks == []
    assert plan.steps_run == []
    assert plan.registered is False
    assert plan.installers_run == []
    assert plan.hub_synced is False


def test_disabled_step_action_never_runs() -> None:
    log: list[str] = []
    steps = [
        _rec_step("on", log, enabled=lambda c: True),
        _rec_step("off", log, enabled=lambda c: False),
    ]
    plan = run_onboard(_ctx(steps))
    assert log == ["on"]
    assert plan.steps_run == ["on"]


# ---------------------------------------------------------------------------
# bh-dhl6 — never configure a beads remote we cannot push to
# ---------------------------------------------------------------------------


def _capture_bd_calls(monkeypatch):
    """Patch onboard's rig.run seam; return the list of commands it receives."""
    from beadhive import hive

    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):  # noqa: ARG001
        calls.append(list(cmd))
        return _Ok()

    monkeypatch.setattr(hive, "run", _fake_run)
    return calls


class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""


def test_bd_init_unsets_remote_without_push_access(monkeypatch):
    """A repo we lack push access to (viewerPermission=READ / gh absent) leaves sync.remote
    unset — beads live on our fork or nowhere (bh-dhl6)."""
    from beadhive import onboard, registry

    calls = _capture_bd_calls(monkeypatch)
    monkeypatch.setattr(registry, "has_push_access", lambda *a: False)
    ctx = Ctx(hive="github/stablyai/orca", target="/t", provider="github", org="stablyai",
              repo="orca", prefix="orca", cwd="/t")
    onboard._guard_beads_remote(ctx)
    assert ["bd", "config", "unset", "sync.remote"] in calls


def test_bd_init_keeps_remote_with_push_access(monkeypatch):
    """A repo we own (ADMIN/WRITE/MAINTAIN) keeps bd's derived sync.remote — no unset."""
    from beadhive import onboard, registry

    calls = _capture_bd_calls(monkeypatch)
    monkeypatch.setattr(registry, "has_push_access", lambda *a: True)
    ctx = Ctx(hive="github/briancripe/orca", target="/t", provider="github", org="briancripe",
              repo="orca", prefix="orca", cwd="/t")
    onboard._guard_beads_remote(ctx)
    assert ["bd", "config", "unset", "sync.remote"] not in calls


def test_has_push_access_fail_closed_when_gh_absent(monkeypatch):
    from beadhive import registry

    monkeypatch.setattr(registry.shutil, "which", lambda _n: None)
    assert registry.has_push_access("github", "stablyai", "orca") is False


def test_has_push_access_reads_only_is_no_access(monkeypatch):
    from beadhive import registry

    monkeypatch.setattr(registry.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(
        registry, "run",
        lambda *a, **k: _Ok_json('{"viewerPermission": "READ"}'),
    )
    assert registry.has_push_access("github", "stablyai", "orca") is False


def test_has_push_access_write_permission_is_access(monkeypatch):
    from beadhive import registry

    monkeypatch.setattr(registry.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(
        registry, "run",
        lambda *a, **k: _Ok_json('{"viewerPermission": "WRITE"}'),
    )
    assert registry.has_push_access("github", "briancripe", "orca") is True


def _Ok_json(payload: str):
    class _R:
        returncode = 0
        stdout = payload
        stderr = ""

    return _R()
