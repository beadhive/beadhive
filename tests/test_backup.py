"""Tests for beadhive.backup (bh-cmqp.2, bh-5009a) — boundary + retention for the backup roots.

Covers:
- prune_hq_backups: keep-N over dated directories, the min_keep=1 floor, dry-run mutates
  nothing.
- mirror_root/_hive_slug: cwd-SUBDIRECTORY-independent default (the bh-mw97 failure class),
  both the identity-resolved case and the unmanaged-repo fallback.
- hive_backup_dir/hive_backup_usage: plain path/size accessors.
- rotate_hive_backup: missing dir, under-cap no-op, dry-run preview mutates nothing, refuses
  without --confirm, a real rotate (mocked bd) renames + reinit's + syncs, and a mid-rotate
  bd failure rolls the rename back.
- prune_hive_rotated: keep-N over rotated generations, min_keep=0 (can prune to zero).
- usage_report: aggregates every root, plus leftover in-repo stores and legacy locations.
- bh-5009a: the consolidated layout — category roots, one stamp format, manifest.json,
  per-hive migrate retention, and `bh backup migrate-layout`'s relocation of the old roots.
- config accessors: defaults + overrides for backup.hq_keep/hive_cap_mb/hive_rotate_keep/
  migrate_keep/total_warn_mb.
- CLI: `bh backup export|usage|reclaim|migrate-layout`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from beadhive import backup, config
from beadhive.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dated_dir(root: Path, name: str, nbytes: int = 100) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "issues.jsonl").write_bytes(b"x" * nbytes)
    return d


def _fake_bd_ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _git_init(path: Path) -> None:
    """A real (empty) git repo at `path` — `identity.workspace_identity` needs `git
    rev-parse --show-toplevel` to actually succeed, not just a plausible directory layout."""
    from harness.world import git

    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)


# ---------------------------------------------------------------------------
# prune_hq_backups
# ---------------------------------------------------------------------------


def test_prune_hq_backups_keeps_newest_n(monkeypatch, tmp_path):
    root = tmp_path / "hq-backups"
    for name in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        _make_dated_dir(root, name)
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: root)

    result = backup.prune_hq_backups({"backup": {"hq_keep": 2}})

    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["2026-01-03", "2026-01-04"]
    assert set(result.removed) == {"2026-01-01", "2026-01-02"}
    assert result.reclaimed_bytes == 200
    assert result.dry_run is False


def test_prune_hq_backups_never_drops_below_one(monkeypatch, tmp_path):
    """A keep=0 (or negative) config value is clamped to 1 — a fresh backup must always be
    left restorable."""
    root = tmp_path / "hq-backups"
    for name in ("2026-01-01", "2026-01-02"):
        _make_dated_dir(root, name)
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: root)

    result = backup.prune_hq_backups({"backup": {"hq_keep": 0}})

    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["2026-01-02"]
    assert result.removed == ["2026-01-01"]


def test_prune_hq_backups_dry_run_mutates_nothing(monkeypatch, tmp_path):
    root = tmp_path / "hq-backups"
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        _make_dated_dir(root, name)
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: root)

    result = backup.prune_hq_backups({"backup": {"hq_keep": 1}}, dry_run=True)

    assert sorted(p.name for p in root.iterdir()) == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert set(result.removed) == {"2026-01-01", "2026-01-02"}
    assert result.reclaimed_bytes == 200
    assert result.dry_run is True


def test_prune_hq_backups_empty_root_is_a_noop(monkeypatch, tmp_path):
    root = tmp_path / "hq-backups"  # never created
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: root)

    result = backup.prune_hq_backups({})

    assert result.removed == []
    assert result.reclaimed_bytes == 0


def test_prune_hq_backups_explicit_keep_overrides_config(monkeypatch, tmp_path):
    root = tmp_path / "hq-backups"
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        _make_dated_dir(root, name)
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: root)

    result = backup.prune_hq_backups({"backup": {"hq_keep": 5}}, keep=1)

    assert sorted(p.name for p in root.iterdir()) == ["2026-01-03"]
    assert len(result.removed) == 2


# ---------------------------------------------------------------------------
# mirror_root / _hive_slug — cwd independence (bh-mw97 failure class)
# ---------------------------------------------------------------------------


def test_mirror_root_is_stable_across_subdirectories(monkeypatch, tmp_path):
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "acme" / "widget"
    _git_init(repo)
    (repo / "sub" / "deeper").mkdir(parents=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    home = tmp_path / "bh-home"
    monkeypatch.setenv("BH_HOME", str(home))
    cfg: dict = {"managed_repos": []}

    top = backup.mirror_root(cfg, cwd=repo)
    nested = backup.mirror_root(cfg, cwd=repo / "sub" / "deeper")

    assert top == nested == home / "backups" / "mirrors" / "github" / "acme" / "widget"


def test_mirror_root_falls_back_to_git_toplevel_when_unmanaged(monkeypatch, tmp_path):
    """A repo bh has no git-workspace/worktree identity for still gets a cwd-subdirectory-
    stable answer — via the git toplevel name, not raw Path.cwd(). Uses the autouse-sandboxed
    $BH_HOME (already seeded with a config.yaml) rather than a fresh override — the fallback
    path this exercises (`registry.current_hive`/`_entry_for_path`) needs `config.load()` to
    find a real file, which a bare `monkeypatch.setenv("BH_HOME", ...)` with no config.yaml
    doesn't provide."""
    unmanaged = tmp_path / "elsewhere" / "myrepo"
    _git_init(unmanaged)
    (unmanaged / "sub").mkdir(parents=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws-empty"))
    cfg: dict = {"managed_repos": []}

    top = backup.mirror_root(cfg, cwd=unmanaged)
    nested = backup.mirror_root(cfg, cwd=unmanaged / "sub")

    assert top == nested == config.home() / "backups" / "mirrors" / "_unmanaged" / "myrepo"


def test_mirror_usage_reports_zero_when_never_exported(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws-empty"))
    cfg = {"managed_repos": []}

    path, size = backup.mirror_usage(cfg, cwd=tmp_path)

    assert size == 0
    assert path.name == "issues.jsonl"


# ---------------------------------------------------------------------------
# hive_backup_dir / hive_backup_usage
# ---------------------------------------------------------------------------


def test_hive_backup_dir_and_usage(tmp_path):
    hive_dir = tmp_path / "myhive"
    b = hive_dir / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk1.darc").write_bytes(b"a" * 1000)
    (b / "chunk2.darc").write_bytes(b"b" * 2000)

    assert backup.hive_backup_dir(hive_dir) == b
    assert backup.hive_backup_usage(hive_dir) == 3000


def test_hive_backup_usage_missing_dir_is_zero(tmp_path):
    assert backup.hive_backup_usage(tmp_path / "no-such-hive") == 0


# ---------------------------------------------------------------------------
# bh-ypfnu: bd_backup_target / bd_backup_points_into_migrate_root / repoint_bd_backup
# ---------------------------------------------------------------------------


def _write_dolt_backup_json(hive_dir: Path, url: str) -> None:
    (hive_dir / ".beads").mkdir(parents=True, exist_ok=True)
    (hive_dir / ".beads" / "dolt-backup.json").write_text(
        json.dumps({"backup_url": url, "backup_name": "default"})
    )


def test_bd_backup_target_reads_the_registered_absolute_path(tmp_path):
    hive_dir = tmp_path / "hive"
    _write_dolt_backup_json(hive_dir, f"file://{tmp_path}/somewhere/dolt-native")

    assert backup.bd_backup_target(hive_dir) == tmp_path / "somewhere" / "dolt-native"


def test_bd_backup_target_none_when_no_registration_file(tmp_path):
    assert backup.bd_backup_target(tmp_path / "hive") is None


def test_bd_backup_target_none_for_a_non_filesystem_destination(tmp_path):
    """A DoltHub remote (or any other non-`file://` destination an operator configured on
    purpose) is never something this module has business touching."""
    hive_dir = tmp_path / "hive"
    _write_dolt_backup_json(hive_dir, "https://doltremoteapi.dolthub.com/user/repo")

    assert backup.bd_backup_target(hive_dir) is None


def test_bd_backup_points_into_migrate_root_true_for_the_current_root(monkeypatch, tmp_path):
    """bh-infra's own measured shape: still pointed at THIS run's migrate snapshot, MIS-pointed
    rather than dangling (the set still physically exists)."""
    cfg = {}
    monkeypatch.setattr(backup, "migrate_root", lambda cfg=None: tmp_path / "backups" / "migrate")
    monkeypatch.setattr(backup, "legacy_migrate_root", lambda cfg=None: tmp_path / "no-legacy")
    hive_dir = tmp_path / "hive"
    target = tmp_path / "backups" / "migrate" / "github/beadhive/infra" / "2026-08-08T192258Z"
    target.mkdir(parents=True)
    _write_dolt_backup_json(hive_dir, f"file://{target}/dolt-native")

    result = backup.bd_backup_points_into_migrate_root(hive_dir, cfg)

    assert result == target / "dolt-native"


def test_bd_backup_points_into_migrate_root_true_for_the_legacy_root_even_when_gone(
    monkeypatch, tmp_path
):
    """nvhack's own measured shape: pointed at a path an EARLIER `migrate_layout` run already
    relocated out from under it — DANGLING, the directory no longer physically exists — but the
    recorded path text still reads as inside the legacy root, so this must still fire."""
    cfg = {}
    monkeypatch.setattr(backup, "migrate_root", lambda cfg=None: tmp_path / "no-current")
    monkeypatch.setattr(
        backup, "legacy_migrate_root", lambda cfg=None: tmp_path / "storage-migrate-backups"
    )
    hive_dir = tmp_path / "hive"
    gone = tmp_path / "storage-migrate-backups" / "github-x-y" / "2026-08-08T165333Z"
    _write_dolt_backup_json(hive_dir, f"file://{gone}/dolt-native")
    assert not gone.exists()  # the whole point: it's gone, and this must still detect it

    result = backup.bd_backup_points_into_migrate_root(hive_dir, cfg)

    assert result == gone / "dolt-native"


def test_bd_backup_points_into_migrate_root_none_when_pointed_at_root_2(monkeypatch, tmp_path):
    cfg = {}
    monkeypatch.setattr(backup, "migrate_root", lambda cfg=None: tmp_path / "backups" / "migrate")
    monkeypatch.setattr(backup, "legacy_migrate_root", lambda cfg=None: tmp_path / "no-legacy")
    hive_dir = tmp_path / "hive"
    _write_dolt_backup_json(hive_dir, f"file://{hive_dir}/.beads/backup")

    assert backup.bd_backup_points_into_migrate_root(hive_dir, cfg) is None


def test_bd_backup_points_into_migrate_root_none_with_no_registration(tmp_path):
    assert backup.bd_backup_points_into_migrate_root(tmp_path / "hive", {}) is None


def test_repoint_bd_backup_calls_engine_backup_at_root_2(monkeypatch, tmp_path):
    from beadhive import engine as engine_mod

    calls = []

    class _FakeEngine:
        def backup(self, cwd, dest, *, actor=""):
            calls.append((Path(cwd), Path(dest), actor))
            return subprocess.CompletedProcess(["bd"], 0, "", "")

    monkeypatch.setattr(engine_mod, "get_engine", lambda cfg: _FakeEngine())
    hive_dir = tmp_path / "hive"

    err = backup.repoint_bd_backup(hive_dir, {}, actor="dev")

    assert err == ""
    assert calls == [(hive_dir, backup.hive_backup_dir(hive_dir), "dev")]


def test_repoint_bd_backup_reports_a_failure_without_raising(monkeypatch, tmp_path):
    from beadhive import engine as engine_mod

    class _FailEngine:
        def backup(self, cwd, dest, *, actor=""):
            return subprocess.CompletedProcess(["bd"], 1, "", "boom")

    monkeypatch.setattr(engine_mod, "get_engine", lambda cfg: _FailEngine())

    err = backup.repoint_bd_backup(tmp_path / "hive", {}, actor="dev")

    assert "boom" in err


# ---------------------------------------------------------------------------
# rotate_hive_backup
# ---------------------------------------------------------------------------


def test_rotate_missing_backup_dir_is_a_noop(tmp_path):
    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()

    out = backup.rotate_hive_backup(hive_dir, {}, dry_run=False, confirm=True)

    assert out.ok is True
    assert "does not exist" in out.actions[0]


def test_rotate_under_cap_is_a_noop(tmp_path):
    hive_dir = tmp_path / "hive"
    b = hive_dir / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "small.darc").write_bytes(b"x" * 100)

    out = backup.rotate_hive_backup(
        hive_dir, {"backup": {"hive_cap_mb": 500}}, dry_run=False, confirm=True
    )

    assert out.ok is True
    assert "under the 500 MB cap" in out.actions[0]
    assert b.is_dir()  # untouched


def test_rotate_dry_run_previews_without_mutating(monkeypatch, tmp_path):
    hive_dir = tmp_path / "hive"
    b = hive_dir / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 100)

    calls = []
    monkeypatch.setattr(backup, "run", lambda *a, **k: calls.append(a) or _fake_bd_ok(*a, **k))

    out = backup.rotate_hive_backup(
        hive_dir, {"backup": {"hive_cap_mb": 0}}, dry_run=True, confirm=True
    )

    assert out.dry_run is True
    assert "would `bd backup remove`" in out.actions[0]
    assert calls == []  # zero mutation: no bd calls, dir untouched
    assert b.is_dir()
    assert out.rotated_to is None


def test_rotate_refuses_without_confirm(tmp_path):
    hive_dir = tmp_path / "hive"
    b = hive_dir / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 100)

    out = backup.rotate_hive_backup(
        hive_dir, {"backup": {"hive_cap_mb": 0}}, dry_run=False, confirm=False
    )

    assert out.ok is False
    assert "without --confirm" in out.actions[-1]
    assert b.is_dir()  # untouched


def test_rotate_real_run_renames_reinits_and_syncs(monkeypatch, tmp_path):
    hive_dir = tmp_path / "hive"
    b = hive_dir / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 100)

    calls: list[list[str]] = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup, "run", _fake_run)

    out = backup.rotate_hive_backup(
        hive_dir, {"backup": {"hive_cap_mb": 0}}, dry_run=False, confirm=True
    )

    assert out.ok is True
    assert out.rotated_to is not None
    assert out.rotated_to.is_dir()
    assert (out.rotated_to / "chunk.darc").is_file()
    assert not b.exists()  # bd backup init was faked — nothing recreated it on disk
    subs = [c[3:] for c in calls]  # ["bd", "-C", hive_dir, *sub]
    assert subs[0] == ["backup", "remove"]
    assert subs[1] == ["backup", "init", str(b)]
    assert subs[2] == ["backup", "sync"]


def test_rotate_force_ignores_the_cap(monkeypatch, tmp_path):
    hive_dir = tmp_path / "hive"
    b = hive_dir / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 100)
    monkeypatch.setattr(backup, "run", _fake_bd_ok)

    out = backup.rotate_hive_backup(
        hive_dir, {"backup": {"hive_cap_mb": 99999}}, dry_run=False, confirm=True, force=True
    )

    assert out.ok is True
    assert out.rotated_to is not None


def test_rotate_rolls_back_the_rename_on_bd_init_failure(monkeypatch, tmp_path):
    hive_dir = tmp_path / "hive"
    b = hive_dir / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 100)

    def _fake_run(cmd, **kw):
        if cmd[3:5] == ["backup", "init"]:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup, "run", _fake_run)

    out = backup.rotate_hive_backup(
        hive_dir, {"backup": {"hive_cap_mb": 0}}, dry_run=False, confirm=True
    )

    assert out.ok is False
    assert out.rotated_to is None
    assert b.is_dir()  # rolled back — the canonical path is never left empty
    assert (b / "chunk.darc").is_file()
    assert "rolled the rename back" in out.actions[-1]


def test_rotate_bd_remove_failure_leaves_dir_untouched(monkeypatch, tmp_path):
    hive_dir = tmp_path / "hive"
    b = hive_dir / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 100)

    def _fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(backup, "run", _fake_run)

    out = backup.rotate_hive_backup(
        hive_dir, {"backup": {"hive_cap_mb": 0}}, dry_run=False, confirm=True
    )

    assert out.ok is False
    assert b.is_dir()
    assert not any(p.name.startswith("backup.") for p in (hive_dir / ".beads").iterdir())


# ---------------------------------------------------------------------------
# prune_hive_rotated
# ---------------------------------------------------------------------------


def test_prune_hive_rotated_keeps_newest_n(tmp_path):
    hive_dir = tmp_path / "hive"
    beads = hive_dir / ".beads"
    for name in ("backup.20260101T000000Z", "backup.20260102T000000Z", "backup.20260103T000000Z"):
        _make_dated_dir(beads, name)

    result = backup.prune_hive_rotated(hive_dir, {"backup": {"hive_rotate_keep": 1}})

    remaining = sorted(p.name for p in beads.iterdir())
    assert remaining == ["backup.20260103T000000Z"]
    assert len(result.removed) == 2


def test_prune_hive_rotated_can_reach_zero(tmp_path):
    """Unlike the HQ prune, the hive-rotate generations have no min_keep=1 floor — the live
    backup lives back at the canonical un-rotated path."""
    hive_dir = tmp_path / "hive"
    beads = hive_dir / ".beads"
    _make_dated_dir(beads, "backup.20260101T000000Z")

    result = backup.prune_hive_rotated(hive_dir, {"backup": {"hive_rotate_keep": 0}})

    assert list(beads.iterdir()) == []
    assert len(result.removed) == 1


def test_prune_hive_rotated_ignores_the_live_backup_dir(tmp_path):
    """The un-rotated `.beads/backup/` itself is never a rotate-generation candidate."""
    hive_dir = tmp_path / "hive"
    beads = hive_dir / ".beads"
    (beads / "backup").mkdir(parents=True)
    _make_dated_dir(beads, "backup.20260101T000000Z")

    backup.prune_hive_rotated(hive_dir, {"backup": {"hive_rotate_keep": 0}})

    assert (beads / "backup").is_dir()  # untouched


# ---------------------------------------------------------------------------
# usage_report
# ---------------------------------------------------------------------------


def test_usage_report_aggregates_every_root(monkeypatch, tmp_path):
    # Relies on the autouse-sandboxed $BH_HOME (already seeded with a config.yaml) — the
    # mirror slot's cwd-fallback resolution needs `config.load()` to find a real file.
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "acme" / "widget"
    b = repo / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 5000)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))

    hq_root = tmp_path / "backups" / "hq"
    _make_dated_dir(hq_root, "2026-01-01", nbytes=1234)
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: hq_root)

    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"},
        ]
    }

    entries = backup.usage_report(cfg)

    roots = {e.root for e in entries}
    assert roots == {"hq", "hive", "mirror", "migrate"}
    hq_entry = next(e for e in entries if e.root == "hq")
    assert hq_entry.size_bytes == 1234
    hive_entry = next(e for e in entries if e.root == "hive")
    assert hive_entry.size_bytes == 5000
    assert hive_entry.label.startswith("wid")


def test_usage_report_skips_hives_never_backed_up(monkeypatch, tmp_path):
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "acme" / "widget"
    repo.mkdir(parents=True)  # no .beads/backup at all
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: tmp_path / "no-hq-backups")

    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"},
        ]
    }

    entries = backup.usage_report(cfg)

    assert not any(e.root == "hive" for e in entries)


# ---------------------------------------------------------------------------
# config accessors
# ---------------------------------------------------------------------------


def test_backup_hq_keep_default_and_override():
    assert config.backup_hq_keep({}) == 3
    assert config.backup_hq_keep({"backup": {"hq_keep": 9}}) == 9


def test_backup_migrate_keep_default_and_override():
    assert config.backup_migrate_keep({}) == 3
    assert config.backup_migrate_keep({"backup": {"migrate_keep": 1}}) == 1


def test_backup_total_warn_mb_default_and_override():
    assert config.backup_total_warn_mb({}) == 2048
    assert config.backup_total_warn_mb({"backup": {"total_warn_mb": 0}}) == 0


def test_backup_hive_cap_mb_default_and_override():
    assert config.backup_hive_cap_mb({}) == 500
    assert config.backup_hive_cap_mb({"backup": {"hive_cap_mb": 100}}) == 100


def test_backup_hive_rotate_keep_default_and_override():
    assert config.backup_hive_rotate_keep({}) == 3
    assert config.backup_hive_rotate_keep({"backup": {"hive_rotate_keep": 1}}) == 1


def test_backup_in_known_sections():
    assert "backup" in config.KNOWN_SECTIONS


# ---------------------------------------------------------------------------
# CLI: bh backup export / usage / reclaim
# ---------------------------------------------------------------------------


def _cli_env(monkeypatch, tmp_path, cfg_extra: str = "") -> Path:
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    home = tmp_path / "wshome"
    home.mkdir()
    cfg_path = tmp_path / "config.yaml"
    # schema_version pinned to current: sidesteps a pre-existing, unrelated bug where the
    # stale-schema warning leaks into --json stdout (also reproduces on `hive archive list
    # --json` — not this bead's concern to fix).
    cfg_path.write_text(f"schema_version: 1\nproviders: [github]\nmanaged_repos: []\n{cfg_extra}")
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("NO_COLOR", "1")
    return ws_root


def test_cli_backup_export_defaults_to_fixed_per_hive_path(monkeypatch, tmp_path):
    ws_root = _cli_env(monkeypatch, tmp_path)
    repo = ws_root / "github" / "acme" / "widget"
    _git_init(repo)
    (repo / "sub").mkdir(parents=True)

    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        out = cmd[cmd.index("-o") + 1]
        Path(out).write_text("")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    import beadhive.cli as cli_mod

    monkeypatch.setattr(cli_mod, "run", _fake_run)
    monkeypatch.chdir(repo / "sub")

    result = runner.invoke(app, ["backup", "export"])

    assert result.exit_code == 0
    expected = (
        tmp_path / "wshome" / "backups" / "mirrors" / "github" / "acme" / "widget" / "issues.jsonl"
    )
    assert str(expected) in result.output
    assert calls[0][:2] == ["bd", "export"]


def test_cli_backup_export_explicit_dest_still_works(monkeypatch, tmp_path):
    _cli_env(monkeypatch, tmp_path)

    def _fake_run(cmd, **kw):
        out = cmd[cmd.index("-o") + 1]
        Path(out).write_text("")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    import beadhive.cli as cli_mod

    monkeypatch.setattr(cli_mod, "run", _fake_run)
    dest = tmp_path / "explicit-dest"

    result = runner.invoke(app, ["backup", "export", str(dest)])

    assert result.exit_code == 0
    assert (dest / "issues.jsonl").exists()


def test_cli_backup_usage_text_and_json(monkeypatch, tmp_path):
    _cli_env(monkeypatch, tmp_path)
    hq_root = tmp_path / "wshome" / "backups" / "hq"
    _make_dated_dir(hq_root, "2026-01-01")
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: hq_root)

    result = runner.invoke(app, ["backup", "usage"])
    assert result.exit_code == 0
    assert "HQ pre-push backup" in result.output
    assert "total:" in result.output.lower()

    result_json = runner.invoke(app, ["backup", "usage", "--json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)
    assert any(e["root"] == "hq" for e in data["roots"])
    assert data["total_bytes"] > 0


def test_cli_backup_reclaim_root_hq(monkeypatch, tmp_path):
    _cli_env(monkeypatch, tmp_path, cfg_extra="backup:\n  hq_keep: 1\n")
    hq_root = tmp_path / "wshome" / "backups" / "hq"
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        _make_dated_dir(hq_root, name)
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: hq_root)

    result = runner.invoke(app, ["backup", "reclaim", "--root", "hq", "--dry-run"])
    assert result.exit_code == 0
    assert "would prune" in result.output
    assert len(list(hq_root.iterdir())) == 3  # dry-run: untouched

    result = runner.invoke(app, ["backup", "reclaim", "--root", "hq"])
    assert result.exit_code == 0
    assert "pruned" in result.output
    assert len(list(hq_root.iterdir())) == 1


def test_cli_backup_reclaim_rejects_bad_root(monkeypatch, tmp_path):
    _cli_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["backup", "reclaim", "--root", "bogus"])
    assert result.exit_code != 0
    assert "must be hq | hive | migrate | all" in result.output


def test_cli_backup_reclaim_root_hive_refuses_without_confirm(monkeypatch, tmp_path):
    ws_root = _cli_env(monkeypatch, tmp_path, cfg_extra="backup:\n  hive_cap_mb: 0\n")
    repo = ws_root / "github" / "acme" / "widget"
    b = repo / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 100)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["backup", "reclaim", "--root", "hive"])

    assert result.exit_code == 1
    assert "without --confirm" in result.output
    assert b.is_dir()


def test_cli_backup_reclaim_root_hive_confirmed(monkeypatch, tmp_path):
    ws_root = _cli_env(monkeypatch, tmp_path, cfg_extra="backup:\n  hive_cap_mb: 0\n")
    repo = ws_root / "github" / "acme" / "widget"
    b = repo / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 100)
    monkeypatch.chdir(repo)

    import beadhive.backup as backup_mod

    monkeypatch.setattr(backup_mod, "run", _fake_bd_ok)

    result = runner.invoke(app, ["backup", "reclaim", "--root", "hive", "--confirm"])

    assert result.exit_code == 0
    assert "rotated" in result.output
    assert not b.exists()
    rotated = [p for p in (repo / ".beads").iterdir() if p.name.startswith("backup.")]
    assert len(rotated) == 1


# ---------------------------------------------------------------------------
# bh-5009a: the consolidated layout — manifests, migrate retention, relocation
# ---------------------------------------------------------------------------


def _migrate_set(root: Path, slug: str, name: str, nbytes: int = 100) -> Path:
    d = root / slug / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "issues.jsonl").write_bytes(b"x" * nbytes)
    return d


def test_category_roots_are_anchored_on_bh_home(monkeypatch, tmp_path):
    """The whole point of anchoring on config.home(): the pre-bh-5009a `~/.beadhive/hq-backups`
    was hardcoded, so no $BH_HOME override could move it."""
    home = tmp_path / "elsewhere"
    monkeypatch.setenv("BH_HOME", str(home))

    assert backup.hq_root() == home / "backups" / "hq"
    assert backup.mirrors_root() == home / "backups" / "mirrors"
    assert backup.migrate_root() == home / "backups" / "migrate"


def test_stamp_is_lexically_sortable_and_shared_across_roots():
    from datetime import UTC, datetime

    early = backup.stamp(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    late = backup.stamp(datetime(2026, 8, 8, 16, 53, 33, tzinfo=UTC))
    assert early == "2026-01-02T030405Z"
    assert sorted([late, early]) == [early, late]
    # A pre-bh-5009a date-only HQ name is a strict prefix of the same format, so the two still
    # interleave correctly while both roots are being read.
    assert sorted([late, "2026-08-07"]) == ["2026-08-07", late]


def test_write_and_read_manifest_round_trip(tmp_path):
    backup.write_manifest(
        tmp_path,
        kind="migrate",
        hive="github/briancripe/nvidia-hackathon",
        prefix="nvhack",
        verified=True,
        artifacts={"dolt-native": 28268281, "issues.jsonl": 554601},
        issue_count=434,
        source_dolt_mode="embedded",
        target_dolt_mode="server",
    )

    data = backup.read_manifest(tmp_path)
    assert data["kind"] == "migrate"
    assert data["hive"] == "github/briancripe/nvidia-hackathon"
    assert data["prefix"] == "nvhack"  # BOTH addressing schemes recorded (the rename hazard)
    assert data["verified"] is True
    assert data["issue_count"] == 434
    assert data["artifacts"]["dolt-native"] == 28268281
    assert data["taken_at"].endswith("Z") and data["bh_version"]


def test_read_manifest_of_a_pre_bh_5009a_set_is_empty_not_unverified(tmp_path):
    """Every set already on disk predates the manifest — callers must read {} as "unknown"."""
    assert backup.read_manifest(tmp_path) == {}


def test_prune_migrate_backups_is_per_hive_not_across_the_root(monkeypatch, tmp_path):
    """A fleet migration must never let one hive's sets evict another hive's only one."""
    root = tmp_path / "backups" / "migrate"
    monkeypatch.setattr(backup, "migrate_root", lambda cfg=None: root)
    monkeypatch.setattr(backup, "legacy_migrate_root", lambda cfg=None: tmp_path / "nonexistent")
    for name in ("2026-01-01T000000Z", "2026-01-02T000000Z", "2026-01-03T000000Z"):
        _migrate_set(root, "github/acme/widget", name)
    _migrate_set(root, "github/acme/other", "2020-01-01T000000Z")  # older than all of the above

    result = backup.prune_migrate_backups(cfg={"backup": {"migrate_keep": 1}})

    assert sorted(p.name for p in (root / "github/acme/widget").iterdir()) == ["2026-01-03T000000Z"]
    assert [p.name for p in (root / "github/acme/other").iterdir()] == ["2020-01-01T000000Z"]
    assert result.reclaimed_bytes == 200


def test_usage_report_surfaces_migration_artifacts_and_legacy_locations(monkeypatch, tmp_path):
    """The defect bh-5009a exists for: the strongest backup bh takes was invisible to the one
    command whose job is to show what backups exist."""
    ws_root = tmp_path / "ws"
    (ws_root / "github" / "acme" / "widget").mkdir(parents=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: tmp_path / "backups" / "hq")

    _migrate_set(tmp_path / "backups" / "migrate", "github/acme/widget", "2026-01-01T000000Z")
    monkeypatch.setattr(backup, "migrate_root", lambda cfg=None: tmp_path / "backups" / "migrate")
    legacy = tmp_path / "storage-migrate-backups"
    _make_dated_dir(legacy / "github-acme-widget", "2025-01-01T000000Z", nbytes=77)
    monkeypatch.setattr(backup, "legacy_migrate_root", lambda cfg=None: legacy)
    monkeypatch.setattr(backup, "legacy_hq_root", lambda cfg=None: tmp_path / "no-legacy-hq")

    cfg = {"managed_repos": [{"provider": "github", "org": "acme", "repo": "widget"}]}
    entries = backup.usage_report(cfg)

    migrate_entry = next(e for e in entries if e.root == "migrate")
    legacy_entry = next(e for e in entries if e.root == "legacy")
    # Each byte counted ONCE: the category row is the current root, the legacy row is what is
    # still to be relocated. Counting both in the category row would inflate the reported total
    # by exactly the bytes the operator is being told to move.
    assert migrate_entry.size_bytes == 100
    assert legacy_entry.size_bytes == 77
    assert sum(e.size_bytes for e in entries) == 177
    assert "backup.migrate_keep" in migrate_entry.detail
    assert "migrate-layout" in legacy_entry.detail


def test_usage_report_lists_leftover_in_repo_pre_migrate_stores(monkeypatch, tmp_path):
    ws_root = tmp_path / "ws"
    beads = ws_root / "github" / "acme" / "widget" / ".beads"
    (beads / "embeddeddolt.pre-migrate-20260808T000000Z").mkdir(parents=True)
    (beads / "embeddeddolt.pre-migrate-20260808T000000Z" / "noms").write_bytes(b"x" * 4096)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: tmp_path / "backups" / "hq")

    cfg = {"managed_repos": [{"provider": "github", "org": "acme", "repo": "widget"}]}
    entry = next(e for e in backup.usage_report(cfg) if e.root == "pre-migrate")

    assert entry.size_bytes == 4096
    assert "reclaim --root migrate" in entry.detail


def test_total_warning_fires_only_past_the_threshold():
    entries = [backup.RootUsage(root="hq", label="hq", path=Path("/x"), size_bytes=3 * 1024**2)]
    assert backup.total_warning(entries, {"backup": {"total_warn_mb": 2}})
    assert backup.total_warning(entries, {"backup": {"total_warn_mb": 4}}) == ""
    assert backup.total_warning(entries, {"backup": {"total_warn_mb": 0}}) == ""  # disabled


def test_migrate_layout_relocates_every_legacy_root(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("BH_HOME", str(home))

    _make_dated_dir(home / "hq-backups", "2026-01-01", nbytes=11)
    _make_dated_dir(home / "storage-migrate-backups" / "github-acme-widget", "2026-01-02T000000Z")
    legacy_mirror = home / "backups" / "github" / "acme" / "widget"
    legacy_mirror.mkdir(parents=True)
    (legacy_mirror / "issues.jsonl").write_bytes(b"x" * 22)

    cfg = {"managed_repos": [{"provider": "github", "org": "acme", "repo": "widget"}]}
    preview = backup.migrate_layout(cfg, dry_run=True)
    assert len(preview.moves) == 3
    assert (home / "hq-backups" / "2026-01-01").is_dir()  # preview mutated nothing

    result = backup.migrate_layout(cfg, dry_run=False)

    assert result.ok, [m.error for m in result.moves]
    assert (home / "backups" / "hq" / "2026-01-01" / "issues.jsonl").is_file()
    assert (
        home / "backups" / "migrate" / "github" / "acme" / "widget" / "2026-01-02T000000Z"
    ).is_dir()
    assert (home / "backups" / "mirrors" / "github" / "acme" / "widget" / "issues.jsonl").is_file()
    assert not (home / "hq-backups").exists()  # emptied shells removed
    assert backup.legacy_roots(cfg) == []


def test_migrate_layout_files_an_unclaimed_legacy_key_under_unresolved(monkeypatch, tmp_path):
    """An orphan from a retired or renamed hive is still somebody's only pre-migration backup —
    never guessed at, never discarded."""
    home = tmp_path / "home"
    monkeypatch.setenv("BH_HOME", str(home))
    _make_dated_dir(home / "storage-migrate-backups" / "github-gone-away", "2026-01-02T000000Z")

    result = backup.migrate_layout({"managed_repos": []}, dry_run=False)

    assert result.ok
    assert any("_unresolved" in note for note in result.notes)
    assert (
        home / "backups" / "migrate" / "_unresolved" / "github-gone-away" / "2026-01-02T000000Z"
    ).is_dir()


def test_migrate_layout_never_merges_over_an_existing_destination(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("BH_HOME", str(home))
    _make_dated_dir(home / "hq-backups", "2026-01-01", nbytes=11)
    _make_dated_dir(home / "backups" / "hq", "2026-01-01", nbytes=99)  # already relocated

    result = backup.migrate_layout({"managed_repos": []}, dry_run=False)

    assert not result.ok
    assert "already exists" in result.moves[0].error
    assert (home / "backups" / "hq" / "2026-01-01" / "issues.jsonl").stat().st_size == 99
    assert (home / "hq-backups" / "2026-01-01").is_dir()  # source left intact


def test_migrate_layout_heals_a_dangling_registration_left_by_an_earlier_relocation(
    monkeypatch, tmp_path
):
    """nvhack's own measured, current, on-disk state: an EARLIER (pre-bh-ypfnu) `migrate-
    layout` run already relocated its migrate set, but the hive's `.beads/dolt-backup.json`
    still names the now-gone legacy path — dangling. This run has nothing left to relocate for
    that hive (the legacy dir is empty), so the heal must fire independently of whether there
    is a filesystem move to make."""
    home = tmp_path / "home"
    monkeypatch.setenv("BH_HOME", str(home))
    ws_root = tmp_path / "ws"
    hive_dir = ws_root / "github" / "briancripe" / "nvidia-hackathon"
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    gone = home / "storage-migrate-backups" / "github-briancripe-nvidia-hackathon" / "old-stamp"
    _write_dolt_backup_json(hive_dir, f"file://{gone}/dolt-native")
    assert not gone.exists()  # already relocated away by an earlier run — nothing to move here

    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "briancripe", "repo": "nvidia-hackathon", "prefix": "nv"}
        ]
    }
    preview = backup.migrate_layout(cfg, dry_run=True)
    heal_preview = next(m for m in preview.moves if m.kind == "backup-registration")
    assert "would" in heal_preview.how
    # dry-run: the registration is untouched.
    assert (hive_dir / ".beads" / "dolt-backup.json").read_text().count(str(gone)) == 1

    calls = []

    from beadhive import engine as engine_mod

    class _FakeEngine:
        def backup(self, cwd, dest, *, actor=""):
            calls.append((Path(cwd), Path(dest)))
            _write_dolt_backup_json(Path(cwd), f"file://{dest}")
            return subprocess.CompletedProcess(["bd"], 0, "", "")

    monkeypatch.setattr(engine_mod, "get_engine", lambda cfg: _FakeEngine())

    result = backup.migrate_layout(cfg, dry_run=False)

    heal = next(m for m in result.moves if m.kind == "backup-registration")
    assert not heal.error, heal.error
    assert calls == [(hive_dir, backup.hive_backup_dir(hive_dir))]
    assert backup.bd_backup_target(hive_dir) == backup.hive_backup_dir(hive_dir)
    assert backup.bd_backup_points_into_migrate_root(hive_dir, cfg) is None


def test_migrate_layout_heals_a_registration_mispointed_at_the_current_migrate_root(
    monkeypatch, tmp_path
):
    """bh-infra's own measured, current, on-disk state: still pointed at THIS run's own migrate
    snapshot, which still physically exists — mis-pointed, not dangling. Must be healed too."""
    home = tmp_path / "home"
    monkeypatch.setenv("BH_HOME", str(home))
    ws_root = tmp_path / "ws"
    hive_dir = ws_root / "github" / "beadhive" / "infra"
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    current = home / "backups" / "migrate" / "github" / "beadhive" / "infra" / "2026-08-08T192258Z"
    current.mkdir(parents=True)
    _write_dolt_backup_json(hive_dir, f"file://{current}/dolt-native")

    from beadhive import engine as engine_mod

    class _FakeEngine:
        def backup(self, cwd, dest, *, actor=""):
            _write_dolt_backup_json(Path(cwd), f"file://{dest}")
            return subprocess.CompletedProcess(["bd"], 0, "", "")

    monkeypatch.setattr(engine_mod, "get_engine", lambda cfg: _FakeEngine())

    cfg = {"managed_repos": [{"provider": "github", "org": "beadhive", "repo": "infra"}]}
    result = backup.migrate_layout(cfg, dry_run=False)

    heal = next(m for m in result.moves if m.kind == "backup-registration")
    assert not heal.error, heal.error
    assert backup.bd_backup_target(hive_dir) == backup.hive_backup_dir(hive_dir)
    # the migrate set itself is untouched — this heals the REGISTRATION, never the snapshot.
    assert (current / "dolt-native").exists() or current.is_dir()


def test_migrate_layout_reports_a_repoint_failure_without_raising(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("BH_HOME", str(home))
    ws_root = tmp_path / "ws"
    hive_dir = ws_root / "github" / "acme" / "widget"
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    gone = home / "backups" / "migrate" / "github" / "acme" / "widget" / "2026-01-01T000000Z"
    _write_dolt_backup_json(hive_dir, f"file://{gone}/dolt-native")

    from beadhive import engine as engine_mod

    class _FailEngine:
        def backup(self, cwd, dest, *, actor=""):
            return subprocess.CompletedProcess(["bd"], 1, "", "boom")

    monkeypatch.setattr(engine_mod, "get_engine", lambda cfg: _FailEngine())

    cfg = {"managed_repos": [{"provider": "github", "org": "acme", "repo": "widget"}]}
    result = backup.migrate_layout(cfg, dry_run=False)

    heal = next(m for m in result.moves if m.kind == "backup-registration")
    assert "boom" in heal.error
    assert not result.ok


# ---------------------------------------------------------------------------
# bh-ypfnu: usage_report reflects where bd is actually pointed (root #2 row)
# ---------------------------------------------------------------------------


def test_usage_report_flags_a_hive_whose_bd_backup_is_mispointed_into_a_migrate_set(
    monkeypatch, tmp_path
):
    ws_root = tmp_path / "ws"
    hive_dir = ws_root / "github" / "acme" / "widget"
    (hive_dir / ".beads" / "backup").mkdir(parents=True)
    ((hive_dir / ".beads" / "backup") / "chunk.darc").write_bytes(b"x" * 500)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: tmp_path / "backups" / "hq")

    target = tmp_path / "backups" / "migrate" / "github" / "acme" / "widget" / "2026-01-01T000000Z"
    target.mkdir(parents=True)
    monkeypatch.setattr(backup, "migrate_root", lambda cfg=None: tmp_path / "backups" / "migrate")
    monkeypatch.setattr(backup, "legacy_migrate_root", lambda cfg=None: tmp_path / "no-legacy")
    _write_dolt_backup_json(hive_dir, f"file://{target}/dolt-native")

    cfg = {"managed_repos": [{"provider": "github", "org": "acme", "repo": "widget"}]}
    hive_entry = next(e for e in backup.usage_report(cfg) if e.root == "hive")

    assert "NOT actually here" in hive_entry.detail
    assert str(target) in hive_entry.detail


def test_usage_report_hive_row_is_ordinary_when_bd_is_correctly_pointed(monkeypatch, tmp_path):
    ws_root = tmp_path / "ws"
    hive_dir = ws_root / "github" / "acme" / "widget"
    (hive_dir / ".beads" / "backup").mkdir(parents=True)
    ((hive_dir / ".beads" / "backup") / "chunk.darc").write_bytes(b"x" * 500)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setattr(backup, "hq_root", lambda cfg=None: tmp_path / "backups" / "hq")
    _write_dolt_backup_json(hive_dir, f"file://{hive_dir}/.beads/backup")

    cfg = {"managed_repos": [{"provider": "github", "org": "acme", "repo": "widget"}]}
    hive_entry = next(e for e in backup.usage_report(cfg) if e.root == "hive")

    assert "cap" in hive_entry.detail
    assert "NOT actually here" not in hive_entry.detail


def test_cli_backup_migrate_layout_requires_confirm(monkeypatch, tmp_path):
    _cli_env(monkeypatch, tmp_path)
    home = tmp_path / "wshome"
    _make_dated_dir(home / "hq-backups", "2026-01-01")

    result = runner.invoke(app, ["backup", "migrate-layout"])
    assert result.exit_code == 0
    assert "preview" in result.output
    assert (home / "hq-backups" / "2026-01-01").is_dir()

    result = runner.invoke(app, ["backup", "migrate-layout", "--confirm"])
    assert result.exit_code == 0
    assert (home / "backups" / "hq" / "2026-01-01").is_dir()
    assert "relocated 1 set" in result.output


def test_hq_is_treated_as_a_backed_up_store_not_excluded_as_a_non_hive(monkeypatch, tmp_path):
    """`registry.hives` excludes the HQ singleton, but HQ has its own `.beads/backup`, migrates
    storage mode, and therefore writes a `migrate/` set. Routing these sweeps through
    `registry.hives` sent HQ's own 68.7 MB pre-migration backup to `migrate/_unresolved/` on a
    real host, and left its bd backup out of `usage` entirely."""
    from beadhive import registry

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("schema_version: 1\nmanaged_repos: []\n")
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.setenv("BH_CONFIG", str(home / "config.yaml"))
    hq_backup = home / "hq" / ".beads" / "backup"
    hq_backup.mkdir(parents=True)
    (hq_backup / "chunk.darc").write_bytes(b"x" * 3000)
    _make_dated_dir(home / "storage-migrate-backups" / "local-factory-hq", "2026-01-02T000000Z")

    cfg = {
        "managed_repos": [
            {
                "provider": registry.HQ_PROVIDER,
                "org": registry.HQ_ORG,
                "repo": registry.HQ_REPO,
                "prefix": registry.HQ_PREFIX,
                "kind": registry.HQ_KIND,
            }
        ]
    }

    assert backup._legacy_migrate_slug_map(cfg) == {"local-factory-hq": "local/factory/hq"}
    result = backup.migrate_layout(cfg, dry_run=False)
    assert result.notes == []  # resolved to a real triplet, not filed under _unresolved
    assert (home / "backups" / "migrate" / "local" / "factory" / "hq").is_dir()
    assert any(e.root == "hive" and e.size_bytes == 3000 for e in backup.usage_report(cfg))
