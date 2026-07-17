"""config._Env / migrate_home_if_needed — the BH_* env-var fallback and the one-time
~/.ws -> ~/.beadhive home-dir migration (,).

The migration MUST NEVER fire except from a deliberate `bh <command>` invocation
(cli._root) — never as a side effect of a plain config read. These tests monkeypatch
config._DEFAULT_HOME_OLD/_NEW directly so they can never touch a real path on the
machine running the suite, on top of conftest's autouse BH_HOME sandbox.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from beadhive import config, home_migration
from beadhive.run import run

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args, cwd):
    run(["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV)


def test_env_new_name_wins_over_old(monkeypatch):
    monkeypatch.setenv("BH_ROLE", "new")
    monkeypatch.setenv("WS_ROLE", "old")
    assert config._env("role") == "new"


def test_env_falls_back_to_deprecated_old_name(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    monkeypatch.setenv("WS_ROLE", "old")
    assert config._env("role") == "old"


def test_env_empty_new_name_falls_back_to_old(monkeypatch):
    """An empty (but set) BH_* var is treated as unset, matching the old _env_flag behavior."""
    monkeypatch.setenv("BH_ROLE", "")
    monkeypatch.setenv("WS_ROLE", "old")
    assert config._env("role") == "old"


def test_env_none_when_neither_set(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    monkeypatch.delenv("WS_ROLE", raising=False)
    assert config._env("role") is None


def test_home_reads_env_without_any_migration_side_effect(tmp_path, monkeypatch):
    """A plain home() read must never touch disk, even when the legacy dir 'exists'."""
    old = tmp_path / "old-ws"
    old.mkdir()
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    assert config.home() == config._DEFAULT_HOME_NEW
    assert old.is_dir()  # untouched — home() never migrates


def test_migrate_home_if_needed_moves_old_to_new(tmp_path, monkeypatch):
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    (old / "config.yaml").write_text("providers: [github]\n")
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    home_migration.migrate_home_if_needed()

    assert new.is_dir()
    assert not old.exists()
    assert (new / "config.yaml").read_text() == "providers: [github]\n"


def test_migrate_home_if_needed_is_idempotent(tmp_path, monkeypatch):
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    home_migration.migrate_home_if_needed()
    home_migration.migrate_home_if_needed()  # second call: new exists — must no-op, not raise

    assert new.is_dir()


def test_migrate_home_if_needed_noop_when_destination_already_migrated(tmp_path, monkeypatch):
    """bh-2gd1.1: an already-migrated destination (has config.yaml) must be a safe no-op —
    no `FileExistsError` from `shutil.move`, and old is never nested into new. Distinct from
    the idempotent-second-call case above: here `old` is *still present* alongside a
    genuinely-migrated `new`, the scenario that made ``shutil.move`` raise/nest."""
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    (old / "sentinel-old").write_text("old")
    new.mkdir()
    (new / "config.yaml").write_text("providers: [github]\n")
    (new / "sentinel-new").write_text("new")
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    home_migration.migrate_home_if_needed()  # must not raise FileExistsError

    assert (new / "config.yaml").read_text() == "providers: [github]\n"
    assert (new / "sentinel-new").exists()
    assert not (new / "old-ws").exists()  # old never nested into new
    assert old.is_dir()  # left untouched — already migrated, so the move never fires


def test_migrate_home_if_needed_treats_winner_finished_race_as_noop(tmp_path, monkeypatch):
    """bh-2gd1.1: the likely race shape — os.rename is atomic and near-instant, so the
    common outcome of two `bh` processes racing to migrate the same real ``~/.ws`` is that
    the *other* one finishes first. Its `os.rename(old, new)` lands in the gap between our
    guards and our own `shutil.move` call: `old` vanishes out from under `shutil.move`'s
    internal `os.stat(old)`, which is the real `FileNotFoundError` shutil.move raises on a
    missing source (reproduced here by actually removing `old` and delegating to the real
    `shutil.move`, not a generic mocked exception). Losing that race must be a clean no-op."""
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    real_move = home_migration.shutil.move

    def _winner_finishes_first(src, dst):
        shutil.rmtree(src)  # the other bh's os.rename already completed: old is gone
        Path(dst).mkdir()
        (Path(dst) / "config.yaml").write_text("providers: [github]\n")
        return real_move(src, dst)  # src now missing -> raises the real FileNotFoundError

    monkeypatch.setattr(home_migration.shutil, "move", _winner_finishes_first)

    home_migration.migrate_home_if_needed()  # must not raise

    assert not old.exists()
    assert (new / "config.yaml").read_text() == "providers: [github]\n"


def test_migrate_home_if_needed_treats_file_exists_race_against_genuine_winner_as_noop(
    tmp_path, monkeypatch
):
    """bh-2gd1.1: shutil.move can also lose the race via `FileExistsError`/`shutil.Error`
    rather than `FileNotFoundError` (e.g. a cross-filesystem move's copytree fallback
    finding the destination already created). This must only be treated as a lost race —
    not a real failure — once the destination has become a *genuine* migrated home
    (config.yaml present); that's what distinguishes a real winning concurrent `bh` from a
    stray reappearing dir or true corruption (see the reraise test below)."""
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    def _winner_finishes_via_file_exists(*_args, **_kwargs):
        new.mkdir()
        (new / "config.yaml").write_text("providers: [github]\n")
        raise FileExistsError("destination path already exists")

    monkeypatch.setattr(home_migration.shutil, "move", _winner_finishes_via_file_exists)

    home_migration.migrate_home_if_needed()  # must not raise

    assert (new / "config.yaml").exists()


def test_migrate_home_if_needed_never_nests_old_when_stray_dir_reappears(tmp_path, monkeypatch):
    """bh-2gd1.1 literal acceptance criterion: a stray (non-migrated) directory reappearing
    at the destination in the TOCTOU gap between our guard checks and the move must NEVER
    result in `old` silently living at `new/old-ws/...`. Real `shutil.move` doesn't raise at
    all when handed an existing directory destination — it moves src INSIDE it — so this is
    reproduced by actually recreating that directory right before delegating to the real
    `shutil.move`, not by mocking a generic exception. The nest must be detected and undone,
    and the collision surfaced rather than silently accepted."""
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    (old / "sentinel-old").write_text("old")
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    real_move = home_migration.shutil.move
    calls = {"count": 0}

    def _stray_reappears_then_real_move(src, dst):
        # Only inject the stray on the first (real) move attempt — the code's own
        # post-hoc correction also goes through this same shutil.move reference, and that
        # corrective move must behave like the real thing, not re-trigger the race.
        if calls["count"] == 0:
            Path(dst).mkdir()  # a stray dir, no config.yaml — not a genuine migrated home
            (Path(dst) / "sentinel-stray").write_text("stray")
        calls["count"] += 1
        return real_move(src, dst)  # real shutil.move: nests src INSIDE dst, doesn't raise

    monkeypatch.setattr(home_migration.shutil, "move", _stray_reappears_then_real_move)

    with pytest.raises(RuntimeError):
        home_migration.migrate_home_if_needed()

    assert not (new / "old-ws").exists()  # old never left nested inside new
    assert old.is_dir()  # corrected back: old restored to its original location
    assert (old / "sentinel-old").exists()


def test_migrate_home_if_needed_reraises_genuine_shutil_error_instead_of_swallowing(
    tmp_path, monkeypatch
):
    """bh-2gd1.1: a genuine (non-race) `shutil.Error` — e.g. a permission failure part-way
    through `shutil.move`'s cross-filesystem copytree fallback — must not be swallowed as a
    false 'lost the race' success. Only a collision where the destination has become a
    genuinely migrated home (config.yaml present) may be treated as a lost race; anything
    else must propagate so a real corruption/partial migration is never reported as if it
    succeeded."""
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    def _raise_real_error(*_args, **_kwargs):
        raise shutil.Error("[Errno 13] Permission denied: 'old-ws/some-file'")

    monkeypatch.setattr(home_migration.shutil, "move", _raise_real_error)

    with pytest.raises(shutil.Error):
        home_migration.migrate_home_if_needed()

    assert old.is_dir()  # left in place — not silently discarded
    assert not new.exists()  # not falsely marked migrated


def test_migrate_home_if_needed_skips_when_old_absent(tmp_path, monkeypatch):
    old = tmp_path / "old-ws"  # never created
    new = tmp_path / "new-beadhive"
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    home_migration.migrate_home_if_needed()

    assert not new.exists()


def test_migrate_home_if_needed_skips_when_home_env_explicitly_set(tmp_path, monkeypatch):
    """An explicit BH_HOME/WS_HOME override means the operator already made a deliberate
    choice — migration must stay out of the way even if the legacy default dir exists."""
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.setenv("BH_HOME", str(tmp_path / "custom"))

    home_migration.migrate_home_if_needed()

    assert old.is_dir()  # untouched
    assert not new.exists()


# ----: guard against a stray new-home + follow-on repairs -------------------


def test_home_migrated_false_for_stray_dir_without_config_yaml(tmp_path, monkeypatch):
    """A new-home dir with no config.yaml (e.g. a cache file some code path wrote before
    migration ever ran) must NOT read as 'already migrated'."""
    new = tmp_path / "new-beadhive"
    (new / "cache").mkdir(parents=True)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    assert home_migration._home_migrated() is False


def test_home_migrated_true_when_config_yaml_present(tmp_path, monkeypatch):
    new = tmp_path / "new-beadhive"
    new.mkdir()
    (new / "config.yaml").write_text("providers: [github]\n")
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    assert home_migration._home_migrated() is True


def test_migrate_home_if_needed_clears_stray_new_home_and_migrates(tmp_path, monkeypatch):
    """A stray new-home (cache only, no config.yaml) must not permanently block the real
    migration — it's cleared and the real move proceeds."""
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    (old / "config.yaml").write_text("providers: [github]\n")
    (old / "hub").mkdir()
    (new / "cache").mkdir(parents=True)  # the stray artifact
    (new / "cache" / "metadata.json").write_text("{}")
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)

    home_migration.migrate_home_if_needed()

    assert not old.exists()
    assert (new / "config.yaml").read_text() == "providers: [github]\n"
    assert (new / "hub").is_dir()
    assert not (new / "cache" / "metadata.json").exists()  # stray artifact cleared, not merged


def test_migrate_home_if_needed_rewrites_stale_worktrees_path(tmp_path, monkeypatch):
    """A config value that textually hardcodes the old home (e.g. a customized
    worktrees.path) survives the directory move unchanged unless rewritten explicitly."""
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    (old / "config.yaml").write_text(
        f"providers: [github]\nworktrees:\n  path: {old}/wt\n  ephemeral: false\n"
    )
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)

    home_migration.migrate_home_if_needed()

    rewritten = config.load()
    assert rewritten["worktrees"]["path"] == f"{new}/wt"


def test_migrate_home_if_needed_rewrites_tilde_relative_stale_path(tmp_path, monkeypatch):
    """The '~/<old-name>/...' form (the portable style bh config set itself writes) is
    rewritten too, not just the fully-expanded absolute form."""
    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    old.mkdir()
    (old / "config.yaml").write_text(
        f"providers: [github]\nworktrees:\n  path: ~/{old.name}/wt\n  ephemeral: false\n"
    )
    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)

    home_migration.migrate_home_if_needed()

    rewritten = config.load()
    assert rewritten["worktrees"]["path"] == f"~/{new.name}/wt"


def test_migrate_home_if_needed_repairs_worktree_links(tmp_path, monkeypatch):
    """End-to-end: a real git repo with a real worktree living under the old home goes
    `prunable` after a plain directory move; migrate_home_if_needed must leave it healthy."""
    ws_root = tmp_path / "workspace"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    old = tmp_path / "old-ws"
    new = tmp_path / "new-beadhive"
    wt_leaf = old / "wt" / "github" / "myorg" / "myrepo" / "feature-1"
    _git("worktree", "add", "-q", "-b", "wt/feature-1", str(wt_leaf), cwd=repo)
    (old / "config.yaml").write_text(
        f"providers: [github]\nworktrees:\n  path: {old}/wt\n  ephemeral: false\n"
    )

    monkeypatch.setattr(config, "_DEFAULT_HOME_OLD", old)
    monkeypatch.setattr(config, "_DEFAULT_HOME_NEW", new)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.delenv("BH_HOME", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)

    home_migration.migrate_home_if_needed()

    new_leaf = new / "wt" / "github" / "myorg" / "myrepo" / "feature-1"
    assert new_leaf.is_dir()  # the directory itself moved along with the rest of home
    listing = run(["git", "worktree", "list"], cwd=str(repo), capture=True, check=True).stdout
    assert "prunable" not in listing
    assert str(new_leaf) in listing


def test_migrate_hive_keys_if_needed_renames_otel_rig_and_git_workspace_rig_match(monkeypatch):
    """bh-41rh hard cutover: a config.yaml written before the rig->hive rename still carries
    the old key names; the one-time migration renames both, in place, on disk."""
    config.config_path().write_text(
        "providers: [github]\n"
        "managed_repos: []\n"
        "otel:\n"
        "  enabled: true\n"
        "  rig: myrig\n"
        "git_workspace:\n"
        "  enabled: true\n"
        "  rig_match: triplet\n"
    )

    config.migrate_hive_keys_if_needed()

    cfg = config.load()
    assert cfg["otel"]["hive"] == "myrig"
    assert "rig" not in cfg["otel"]
    assert cfg["git_workspace"]["hive_match"] == "triplet"
    assert "rig_match" not in cfg["git_workspace"]


def test_migrate_hive_keys_if_needed_is_idempotent(monkeypatch):
    config.config_path().write_text(
        "providers: [github]\nmanaged_repos: []\notel:\n  rig: myrig\n"
    )

    config.migrate_hive_keys_if_needed()
    config.migrate_hive_keys_if_needed()  # second call: already migrated — must no-op, not raise

    cfg = config.load()
    assert cfg["otel"]["hive"] == "myrig"


def test_migrate_hive_keys_if_needed_skips_when_neither_old_key_present():
    """A fresh (or already-migrated) config round-trips byte-for-byte — no spurious rewrite."""
    before = config.config_path().read_text()

    config.migrate_hive_keys_if_needed()

    assert config.config_path().read_text() == before


def test_migrate_hive_keys_if_needed_skips_when_config_absent(monkeypatch):
    config.config_path().unlink()

    config.migrate_hive_keys_if_needed()  # must not raise FileNotFoundError

    assert not config.config_path().exists()


def test_migrate_hive_keys_if_needed_never_overwrites_an_existing_new_key():
    """Both keys present (mid-migration edit, or hand-authored) — new key wins; old just drops."""
    config.config_path().write_text(
        "providers: [github]\nmanaged_repos: []\notel:\n  rig: old\n  hive: new\n"
    )

    config.migrate_hive_keys_if_needed()

    cfg = config.load()
    assert cfg["otel"]["hive"] == "new"
    assert "rig" not in cfg["otel"]


# ---- warn_stale_schema_version_if_needed (bh-5cgm.3) --------------------------
# Lightest-touch detection only — no migration, no rewrite, never called from a bare
# load()/getter. See config_schema.SCHEMA_VERSION (bh-5cgm.1) for the version compared
# against, and cli._root for the one real call site (mirroring migrate_hive_keys_if_needed).


def test_warn_stale_schema_version_missing_warns_once(caplog):
    """A config with no `schema_version` key at all is the pre-versioning (unversioned) case
    — exactly one warning naming `bh config validate`."""
    import logging

    config.config_path().write_text("providers: [github]\nmanaged_repos: []\n")

    with caplog.at_level(logging.WARNING):
        config.warn_stale_schema_version_if_needed()

    warnings = [r for r in caplog.records if "config_schema_version_stale" in r.message]
    assert len(warnings) == 1
    assert "bh config validate" in warnings[0].message


def test_warn_stale_schema_version_old_warns(caplog):
    """schema_version present but lower than SCHEMA_VERSION also warns."""
    import logging

    from beadhive.config_schema import SCHEMA_VERSION

    config.config_path().write_text(
        f"providers: [github]\nmanaged_repos: []\nschema_version: {SCHEMA_VERSION - 1}\n"
    )

    with caplog.at_level(logging.WARNING):
        config.warn_stale_schema_version_if_needed()

    warnings = [r for r in caplog.records if "config_schema_version_stale" in r.message]
    assert len(warnings) == 1


def test_warn_stale_schema_version_current_is_silent(caplog):
    """schema_version == SCHEMA_VERSION → no warning at all."""
    import logging

    from beadhive.config_schema import SCHEMA_VERSION

    config.config_path().write_text(
        f"providers: [github]\nmanaged_repos: []\nschema_version: {SCHEMA_VERSION}\n"
    )

    with caplog.at_level(logging.WARNING):
        config.warn_stale_schema_version_if_needed()

    assert not any("config_schema_version_stale" in r.message for r in caplog.records)


def test_warn_stale_schema_version_skips_when_config_absent():
    """No config file yet (pre `config init`) → no warning, no raise — that gap is `config
    init`'s job, not this check's."""
    config.config_path().unlink()

    config.warn_stale_schema_version_if_needed()  # must not raise FileNotFoundError


def test_bare_load_never_warns_on_a_stale_config(caplog):
    """Acceptance: a bare `load()`/getter must never trigger the warning — only the real CLI
    invocation path (cli._root) calls warn_stale_schema_version_if_needed at all."""
    import logging

    config.config_path().write_text("providers: [github]\nmanaged_repos: []\n")  # no schema_version

    with caplog.at_level(logging.WARNING):
        config.load()
        config.managed_repos()

    assert not any("config_schema_version_stale" in r.message for r in caplog.records)


def test_cli_invocation_triggers_the_stale_schema_version_warning(caplog):
    """The one real call site: `cli._root` runs on every actual `bh <command>` invocation, so
    a genuine CLI call (not a bare load()) surfaces the warning for a stale/unversioned
    config."""
    import logging

    from typer.testing import CliRunner

    from beadhive.cli import app

    config.config_path().write_text("providers: [github]\nmanaged_repos: []\n")  # no schema_version

    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(app, ["config", "path"])

    assert result.exit_code == 0
    assert any("config_schema_version_stale" in r.message for r in caplog.records)
