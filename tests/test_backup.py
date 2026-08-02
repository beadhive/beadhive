"""Tests for beadhive.backup (bh-cmqp.2) — boundary + retention for the three backup roots.

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
- usage_report: aggregates all three roots.
- config accessors: defaults + overrides for backup.hq_keep/hive_cap_mb/hive_rotate_keep.
- CLI: `bh backup export|usage|reclaim`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from beadhive import backup, config, hq
from beadhive.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dated_dir(root: Path, name: str, nbytes: int = 100) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "hq-issues.jsonl").write_bytes(b"x" * nbytes)
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
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: root)

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
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: root)

    result = backup.prune_hq_backups({"backup": {"hq_keep": 0}})

    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["2026-01-02"]
    assert result.removed == ["2026-01-01"]


def test_prune_hq_backups_dry_run_mutates_nothing(monkeypatch, tmp_path):
    root = tmp_path / "hq-backups"
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        _make_dated_dir(root, name)
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: root)

    result = backup.prune_hq_backups({"backup": {"hq_keep": 1}}, dry_run=True)

    assert sorted(p.name for p in root.iterdir()) == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert set(result.removed) == {"2026-01-01", "2026-01-02"}
    assert result.reclaimed_bytes == 200
    assert result.dry_run is True


def test_prune_hq_backups_empty_root_is_a_noop(monkeypatch, tmp_path):
    root = tmp_path / "hq-backups"  # never created
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: root)

    result = backup.prune_hq_backups({})

    assert result.removed == []
    assert result.reclaimed_bytes == 0


def test_prune_hq_backups_explicit_keep_overrides_config(monkeypatch, tmp_path):
    root = tmp_path / "hq-backups"
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        _make_dated_dir(root, name)
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: root)

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

    assert top == nested == home / "backups" / "github" / "acme" / "widget"


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

    assert top == nested == config.home() / "backups" / "_unmanaged" / "myrepo"


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


def test_usage_report_aggregates_all_three_roots(monkeypatch, tmp_path):
    # Relies on the autouse-sandboxed $BH_HOME (already seeded with a config.yaml) — the
    # mirror slot's cwd-fallback resolution needs `config.load()` to find a real file.
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "acme" / "widget"
    b = repo / ".beads" / "backup"
    b.mkdir(parents=True)
    (b / "chunk.darc").write_bytes(b"x" * 5000)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))

    hq_root = tmp_path / "hq-backups"
    _make_dated_dir(hq_root, "2026-01-01", nbytes=1234)
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: hq_root)

    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"},
        ]
    }

    entries = backup.usage_report(cfg)

    roots = {e.root for e in entries}
    assert roots == {"hq", "hive", "mirror"}
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
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: tmp_path / "no-hq-backups")

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
    assert config.backup_hq_keep({}) == 5
    assert config.backup_hq_keep({"backup": {"hq_keep": 9}}) == 9


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
    expected = tmp_path / "wshome" / "backups" / "github" / "acme" / "widget" / "issues.jsonl"
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
    hq_root = tmp_path / "wshome" / "hq-backups"
    _make_dated_dir(hq_root, "2026-01-01")
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: hq_root)

    result = runner.invoke(app, ["backup", "usage"])
    assert result.exit_code == 0
    assert "HQ pre-push backup" in result.output
    assert "total:" in result.output.lower()

    result_json = runner.invoke(app, ["backup", "usage", "--json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)
    assert any(e["root"] == "hq" for e in data)


def test_cli_backup_reclaim_root_hq(monkeypatch, tmp_path):
    _cli_env(monkeypatch, tmp_path, cfg_extra="backup:\n  hq_keep: 1\n")
    hq_root = tmp_path / "wshome" / "hq-backups"
    for name in ("2026-01-01", "2026-01-02", "2026-01-03"):
        _make_dated_dir(hq_root, name)
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: hq_root)

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
    assert "must be hq | hive | all" in result.output


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
