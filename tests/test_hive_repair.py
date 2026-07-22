"""`bh hive repair --prefix <p>` — reconcile registry prefix vs beads-DB issue_prefix.

Real git hive under $GIT_WORKSPACE (identity/registry resolve for real); `bd config get
issue_prefix` and `bd rename-prefix` faked by monkeypatching the bd.json/bd.run seam directly
(no swarm/gate state to track here, so no need for test_plan_repair.py's stateful FakeBd).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import typer

from beadhive import bd as bd_mod
from beadhive import config, hive_repair, registry
from beadhive.run import run as real_run

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

CONFIG_YAML = """\
providers: [github]
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
  - {provider: github, org: myorg, repo: other, prefix: ot, kind: personal}
"""


def _git(*args, cwd):
    return real_run(["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV)


@pytest.fixture
def hive(tmp_path, monkeypatch):
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    main.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "human@example.com", cwd=main)
    _git("config", "user.name", "human", cwd=main)
    _git("commit", "--allow-empty", "-m", "init", cwd=main)
    (main / ".beads").mkdir()

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.delenv("WS_CREW", raising=False)
    monkeypatch.delenv("BH_DEV", raising=False)
    monkeypatch.chdir(main)
    return SimpleNamespace(main=main, tmp=tmp_path)


class FakeBd:
    """Stateful fake for the two bd calls repair makes: `config get issue_prefix` (read) and
    `rename-prefix <p>-` (write, mutates the served db_prefix so a second detect() converges)."""

    def __init__(self, db_prefix):
        self.db_prefix = db_prefix
        self.rename_calls = []

    def fake_json(self, args, cwd):
        assert args == ["config", "get", "issue_prefix"]
        return {"key": "issue_prefix", "schema_version": 1, "value": self.db_prefix}

    def fake_run(self, args, cwd, actor="", capture=False, text_input=None):
        assert args[0] == "rename-prefix"
        self.rename_calls.append(args[1])
        self.db_prefix = args[1].rstrip("-")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _patch_bd(monkeypatch, db_prefix):
    fake = FakeBd(db_prefix)
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)
    return fake


# ---- normalize_prefix -------------------------------------------------------


def test_normalize_prefix_strips_trailing_hyphen():
    assert hive_repair.normalize_prefix("mr-") == "mr"


def test_normalize_prefix_rejects_empty():
    with pytest.raises(hive_repair.RepairError, match="cannot be empty"):
        hive_repair.normalize_prefix("")


def test_normalize_prefix_rejects_bad_chars():
    with pytest.raises(hive_repair.RepairError, match="invalid prefix"):
        hive_repair.normalize_prefix("1abc")


def test_normalize_prefix_rejects_too_long():
    with pytest.raises(hive_repair.RepairError, match="max is 8"):
        hive_repair.normalize_prefix("abcdefgh")


# ---- detect / repair flow ---------------------------------------------------


def test_repair_detects_mismatch_and_dry_run_makes_no_changes(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "mr")
    hive_repair.repair(hive="", prefix="newpre", yes=False, dry_run=True)
    out = capsys.readouterr().out
    assert "Registry prefix: mr -> newpre" in out
    assert "dry-run" in out
    assert fake.rename_calls == []
    cfg = config.load()
    entry = registry.find_entry(cfg, "github", "myorg", "myrepo")
    assert entry is not None
    assert entry["prefix"] == "mr"


def test_repair_refuses_without_yes(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "mr")
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="", prefix="newpre", yes=False, dry_run=False)
    assert exc.value.exit_code == 1
    assert "refusing" in capsys.readouterr().err
    assert fake.rename_calls == []


def test_repair_applies_with_yes(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "mr")
    hive_repair.repair(hive="", prefix="newpre", yes=True, dry_run=False)
    out = capsys.readouterr().out
    assert "Database migrated" in out
    assert "Registry updated" in out
    assert "Prefixes consistent" in out
    assert fake.rename_calls == ["newpre-"]
    cfg = config.load()
    entry = registry.find_entry(cfg, "github", "myorg", "myrepo")
    assert entry is not None
    assert entry["prefix"] == "newpre"


def test_repair_second_run_is_idempotent_noop(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "mr")
    hive_repair.repair(hive="", prefix="newpre", yes=True, dry_run=False)
    fake.rename_calls.clear()
    hive_repair.repair(hive="", prefix="newpre", yes=True, dry_run=False)
    out = capsys.readouterr().out
    assert "nothing to repair" in out
    assert fake.rename_calls == []


def test_repair_only_updates_registry_when_db_already_matches_target(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "newpre")
    hive_repair.repair(hive="", prefix="newpre", yes=True, dry_run=False)
    out = capsys.readouterr().out
    assert "Registry updated" in out
    assert "Database migrated" not in out
    assert fake.rename_calls == []


def test_repair_refuses_unregistered_hive(hive, monkeypatch):
    _patch_bd(monkeypatch, "mr")
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="github/nope/nope", prefix="newpre", yes=True, dry_run=False)
    assert exc.value.exit_code == 1


def test_repair_refuses_missing_beads_dir(hive, monkeypatch):
    _patch_bd(monkeypatch, "ot")
    other = hive.tmp / "ws" / "github" / "myorg" / "other"
    other.mkdir(parents=True)  # no .beads/ under it
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="github/myorg/other", prefix="newpre", yes=True, dry_run=False)
    assert exc.value.exit_code == 1


def test_repair_refuses_prefix_collision_with_another_hive(hive, monkeypatch):
    _patch_bd(monkeypatch, "mr")
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="", prefix="ot", yes=True, dry_run=False)
    assert exc.value.exit_code == 1
