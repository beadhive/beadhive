"""``hq.path`` host config key (bh-b1yh7): precedence and HQ-adjacent path relocation."""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive import backup, config, hq_authority_enforce, hq_beadyard


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for var in ("BH_HQ", "WS_HQ", "WS_HOME", "WS_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.setenv("BH_CONFIG", str(home / "config.yaml"))
    return home


def _set_hq_path(home: Path, target: Path) -> None:
    (home / "config.yaml").write_text(f"hq:\n  path: {target}\n")


def test_default_is_home_hq(env):
    assert config.hq_dir() == env / "hq"
    assert config.hq_home() == env


def test_config_path_relocates_hq(env, tmp_path):
    target = tmp_path / "gc" / "hq"
    _set_hq_path(env, target)
    assert config.hq_dir() == target
    assert config.hq_home() == target.parent


def test_env_wins_over_config(env, tmp_path, monkeypatch):
    _set_hq_path(env, tmp_path / "gc" / "hq")
    monkeypatch.setenv("BH_HQ", str(tmp_path / "envhq"))
    assert config.hq_dir() == tmp_path / "envhq"


def test_malformed_host_falls_back_to_default(env):
    (env / "config.yaml").write_text("hq: [not, a, mapping\n")
    assert config.hq_dir() == env / "hq"


def test_adjacent_paths_follow_hq_path_not_bh_home(env, tmp_path):
    target = tmp_path / "gc" / "hq"
    _set_hq_path(env, target)
    base = target.parent
    assert backup.hq_root() == base / "backups" / "hq"
    assert backup.legacy_hq_root() == base / "hq-backups"

    class _Snap:
        backend_identity = "b"
        generation = "g"
        commit_revision = "r"

    adoption = hq_beadyard._pending_sql_directory(_Snap())
    assert base in adoption.parents
    state = hq_authority_enforce._fleet_state_path()
    assert state is not None and Path(state).parent == base / "state"
    assert not (env / "hq").exists()
    assert not (env / "backups").exists()
    assert not (env / "state").exists()


def test_bh_hq_leaves_adjacent_paths_under_bh_home(env, tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HQ", str(tmp_path / "envhq" / "hq"))
    assert config.hq_dir() == tmp_path / "envhq" / "hq"
    assert config.hq_home() == env
    assert backup.hq_root() == env / "backups" / "hq"
    assert backup.legacy_hq_root() == env / "hq-backups"
    state = hq_authority_enforce._fleet_state_path()
    assert state is not None and Path(state).parent == env / "state"


def test_bh_hq_wins_over_hq_path_for_adjacent_state(env, tmp_path, monkeypatch):
    _set_hq_path(env, tmp_path / "gc" / "hq")
    monkeypatch.setenv("BH_HQ", str(tmp_path / "envhq" / "hq"))
    assert config.hq_home() == env
