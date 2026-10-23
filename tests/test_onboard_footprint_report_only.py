"""bh-h7o9c — re-onboarding a furnished, already-registered hive is report-only by default.

Real git in a tmp dir; bd is faked. The regression: `hive onboard --no-hub-sync` on a
furnish:full hive flipped `export.auto` in tracked .beads/config.yaml and committed
"chore(agf): hive scaffolding repair" on the live checkout.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from beadhive import onboard


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(autouse=True)
def _no_opt_in(monkeypatch):
    monkeypatch.delenv(onboard.REPAIR_FOOTPRINT_ENV, raising=False)


@pytest.fixture
def repo(tmp_path):
    _git("init", "-q", ".", cwd=tmp_path)
    _git("config", "core.excludesFile", "/dev/null", cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=tmp_path)
    _git("config", "user.name", "t", cwd=tmp_path)
    _git("config", "commit.gpgsign", "false", cwd=tmp_path)
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads/config.yaml").write_text("export.auto: false\n")
    (tmp_path / "CLAUDE.md").write_text("hint\n")
    _git("add", ".", cwd=tmp_path)
    _git("commit", "-q", "-m", "chore(agf): hive scaffolding (beads + agent config)", cwd=tmp_path)
    return tmp_path


def _ctx(base, **kw):
    fields = dict(
        base=base,
        cwd=str(base),
        furnish=True,
        existing={"prefix": "x"},
        furnish_explicit=False,
        _derived=True,
        plan=types.SimpleNamespace(warnings=[]),
    )
    fields.update(kw)
    return types.SimpleNamespace(**fields)


def _fake_bd(monkeypatch, current="false"):
    sets = []

    def fake_run(args, _cwd, **kw):
        cmd = ["bd", *args]
        if args[:2] == ["config", "set"]:
            sets.append(args[2:])
            if args[2] == "export.auto":  # mimic bd writing the tracked yaml
                (_cwd / ".beads/config.yaml").write_text("export.auto: true\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(onboard.bd_mod, "run", fake_run)
    monkeypatch.setattr(
        onboard.bd_mod, "json", lambda args, _cwd, **kw: {"value": current}, raising=False
    )
    return sets


def _head(repo):
    return _git("rev-parse", "HEAD", cwd=repo).strip()


def _assert_repaired(repo):
    """The drifted config is committed (the unpushed scaffold commit is amended in place, as
    before) and the tree is clean."""
    assert _git("show", "HEAD:.beads/config.yaml", cwd=repo) == "export.auto: true\n"
    assert _git("status", "--porcelain", cwd=repo) == ""


def test_reonboard_reports_drift_and_commits_nothing(repo, monkeypatch, capsys):
    sets = _fake_bd(monkeypatch)
    ctx = _ctx(repo)
    before = _head(repo)

    onboard._configure_auto_export(ctx)
    (repo / "CLAUDE.md").write_text("hint edited\n")  # drift in tracked scaffolding
    onboard._act_footprint(ctx)

    assert sets == []  # no bd config set -> tracked config.yaml untouched
    assert (repo / ".beads/config.yaml").read_text() == "export.auto: false\n"
    assert _head(repo) == before
    assert _git("log", "--oneline", cwd=repo).count("\n") == 1
    err = capsys.readouterr().err
    assert "export.auto=true" in err and "CLAUDE.md" in err and "BH_ONBOARD_REPAIR_FOOTPRINT" in err
    assert len(ctx.plan.warnings) == 2  # warnings, so the exit code stays 0


def test_reonboard_with_no_drift_is_quiet(repo, monkeypatch, capsys):
    _fake_bd(monkeypatch, current="true")
    ctx = _ctx(repo)
    onboard._footprint_report_only(ctx)
    onboard._act_footprint(ctx)
    assert ctx.plan.warnings == []
    assert "no tracked scaffolding drift" in capsys.readouterr().out


def test_opt_in_repairs_and_commits_as_before(repo, monkeypatch):
    monkeypatch.setenv(onboard.REPAIR_FOOTPRINT_ENV, "1")
    sets = _fake_bd(monkeypatch)
    ctx = _ctx(repo)

    onboard._configure_auto_export(ctx)
    onboard._act_footprint(ctx)

    assert ["export.auto", "true"] in sets
    _assert_repaired(repo)


def test_explicit_furnish_declaration_still_applies(repo, monkeypatch):
    _fake_bd(monkeypatch)
    ctx = _ctx(repo, furnish_explicit=True)
    onboard._configure_auto_export(ctx)
    onboard._act_footprint(ctx)
    _assert_repaired(repo)


def test_fresh_onboarding_unchanged(repo, monkeypatch):
    sets = _fake_bd(monkeypatch)
    ctx = _ctx(repo, existing=None)  # not yet registered
    assert onboard._footprint_report_only(ctx) is False
    onboard._configure_auto_export(ctx)
    onboard._act_footprint(ctx)
    assert ["export.auto", "true"] in sets
    _assert_repaired(repo)
