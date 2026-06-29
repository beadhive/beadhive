"""Unit self-checks for the Typer-free core functions the CLI verbs delegate to.

These cover the service-layer extraction: the same plain, typed
functions the Typer commands call and a future MCP entrypoint will reuse. No Typer, no
real bd — each function is exercised directly with its subprocess seam faked.
"""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest

from ws import bd, plan, work

Completed = namedtuple("Completed", "returncode stdout stderr")


# ---- bd: triplet labels + create ------------------------------------------


def test_triplet_label_args_inside_managed_repo(monkeypatch):
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: ("github", "myorg", "myrepo"))
    assert bd.triplet_label_args(Path(".")) == ["-l", "provider:github,org:myorg,repo:myrepo"]


def test_triplet_label_args_outside_managed_repo(monkeypatch):
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: None)
    assert bd.triplet_label_args(Path(".")) == []


def test_create_blocks_on_violations_and_runs_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: True)
    monkeypatch.setattr(bd, "run", lambda *a, **k: calls.append(a) or Completed(0, "", ""))
    code, error = bd.create(["title"], Path("."))
    assert code == 1
    assert "label violations" in error
    assert calls == []  # nothing created while the rig is dirty


def test_create_appends_triplet_and_returns_bd_code(monkeypatch):
    seen = {}
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: False)
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: ("github", "myorg", "myrepo"))

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return Completed(0, "", "")

    monkeypatch.setattr(bd, "run", fake_run)
    code, error = bd.create(["My title"], Path("."))
    assert (code, error) == (0, "")
    assert seen["cmd"] == [
        "bd",
        "create",
        "My title",
        "-l",
        "provider:github,org:myorg,repo:myrepo",
    ]


# ---- plan: check_spec ------------------------------------------------------


def test_check_spec_returns_problems_for_missing_acceptance(tmp_path):
    spec = tmp_path / "bad.yaml"
    spec.write_text("epic: {title: E}\nissues:\n  - {handle: a, title: t}\n")
    problems = plan.check_spec(str(spec), {})
    assert any("acceptance" in p for p in problems)


def test_check_spec_valid_spec_returns_empty(tmp_path):
    spec = tmp_path / "ok.yaml"
    spec.write_text("epic: {title: E}\nissues:\n  - {handle: a, title: t, acceptance: works}\n")
    assert plan.check_spec(str(spec), {}) == []


def test_check_spec_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        plan.check_spec(str(tmp_path / "nope.yaml"), {})


# ---- plan: file_molecule raises a Typer-free PlanError ---------------------


def test_create_one_raises_plan_error_on_bd_failure(monkeypatch):
    monkeypatch.setattr(plan, "_bd", lambda *a, **k: Completed(1, "", "boom"))
    with pytest.raises(plan.PlanError):
        plan._create_one(["title"], Path("."), actor="")


# ---- work: refine_branch raises a Typer-free WorkError ---------------------


def test_refine_branch_requires_exactly_one_mode(monkeypatch):
    monkeypatch.setattr(
        work.worktree, "locate", lambda cfg, rig, bead: (None, Path("."), Path("."), "wt/bead/x")
    )
    with pytest.raises(work.WorkError) as ei:
        work.refine_branch({}, rig="", bead="x")  # zero modes selected
    assert "exactly one" in ei.value.messages[0]


def test_refine_branch_missing_worktree(monkeypatch, tmp_path):
    missing = tmp_path / "absent"
    monkeypatch.setattr(
        work.worktree, "locate", lambda cfg, rig, bead: (None, tmp_path, missing, "wt/bead/x")
    )
    with pytest.raises(work.WorkError) as ei:
        work.refine_branch({}, rig="", bead="x", autosquash=True)
    assert "no worktree" in ei.value.messages[0]
    assert ei.value.backup == ""  # no backup created on an early guard
