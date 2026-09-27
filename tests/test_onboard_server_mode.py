"""Server mode on first install (bh-areg.7) — `onboard._act_bd_init`'s three paths default a
BRAND-NEW hive onto bd's shared server, per `docs/design/dolt-server-mode-adr.md` / `bh-ukit.4`
("the default for newly-onboarded hives ... Not per-hive opt-in").

Hermetic: `hive.run` is faked throughout, so no real `bd`/dolt process ever runs. See
`tests/test_onboard_server_mode_int.py` for the real-bd round trip (all three paths, plus the
"existing embedded hive untouched by upgrade" scenario a mock can't prove).

Four things pinned down here:
  1. All three `_act_bd_init` paths activate server mode (`--shared-server` on the two direct
     `bd init` calls; `BEADS_DOLT_SHARED_SERVER=1` for `bd bootstrap`, which has no flag of its
     own).
  2. `_ensure_server_mode_persisted` (constraint 1) — reuses `store_locator.
     ensure_server_mode_persisted` for the metadata write, always re-asserts the
     `dolt.shared-server` config key, and warns visibly (never silently) when it had to fix a
     drift.
  3. Automatic backups remain disabled on a freshly minted shared-server hive even when a Git
     remote exists; enabling them is an operator choice.
  4. The idempotent existing-hive skip path (`.beads` already present) NEVER reaches server-mode
     wiring — an existing hive (embedded or otherwise) is untouched by re-running onboard.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import hive, onboard, store_locator


class _Result:
    """Minimal ``subprocess.CompletedProcess``-shaped stand-in, matching what ``hive.run``
    actually returns (read via ``getattr`` in production code, never attribute access)."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ctx(tmp_path, *, furnish: bool) -> onboard.Ctx:
    ctx = onboard.Ctx(
        hive="github/acme/widget",
        target=str(tmp_path),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(tmp_path),
        prefix="widget",
        furnish=furnish,
    )
    ctx._derived = True  # skip real registry/classify lookups — irrelevant to this bead
    return ctx


def _fake_run_factory(*, git_remote_stdout: str = ""):
    """Fake ``hive.run``: records every command; ``git remote`` returns *git_remote_stdout*;
    everything else succeeds trivially. Never touches a real filesystem/store."""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kw):  # noqa: ARG001
        calls.append(list(cmd))
        if cmd[:2] == ["git", "remote"]:
            return _Result(returncode=0, stdout=git_remote_stdout)
        return _Result(returncode=0)

    return calls, _fake_run


def _patch_neighbors(monkeypatch):
    """Stub the neighbors of `_act_bd_init` that are irrelevant to this bead's own wiring
    (covered by their own bead's tests) so each test below exercises exactly the server-mode
    wiring."""
    monkeypatch.setattr(onboard, "_configure_auto_export", lambda ctx: None)
    monkeypatch.setattr(onboard, "_guard_beads_remote", lambda ctx: None)
    monkeypatch.setattr(onboard, "_bypass_gh2455_dirty_config", lambda ctx: None)
    monkeypatch.setattr(hive, "_ensure_bd_backup_gitignored", lambda base: False)


def _patch_runs(monkeypatch, fake_run):
    """Git stays on ``hive.run``; every bd call meets at the engine subprocess seam."""
    monkeypatch.setattr(hive, "run", fake_run)
    monkeypatch.setattr(onboard.bd_mod, "_run", fake_run)


# ---------------------------------------------------------------------------
# All three paths activate server mode
# ---------------------------------------------------------------------------


def test_furnished_path_passes_shared_server_flag(tmp_path, monkeypatch):
    _patch_neighbors(monkeypatch)
    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda base: False)
    calls, fake_run = _fake_run_factory()
    _patch_runs(monkeypatch, fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=True))

    init_call = calls[0]
    assert init_call[:2] == ["bd", "init"]
    assert "--shared-server" in init_call


def test_zero_footprint_path_passes_shared_server_flag(tmp_path, monkeypatch):
    _patch_neighbors(monkeypatch)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: False)
    monkeypatch.setattr(hive, "_relocate_bd_gitignore", lambda base: False)
    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda base: False)
    calls, fake_run = _fake_run_factory()
    _patch_runs(monkeypatch, fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=False))

    init_call = calls[0]
    assert init_call[:2] == ["bd", "init"]
    assert "--shared-server" in init_call
    assert "--setup-exclude" in init_call


def test_bootstrap_path_activates_shared_server_via_env_var(tmp_path, monkeypatch):
    """`bd bootstrap` has no `--shared-server` flag of its own — the only lever is the env var
    bd itself reads (measured against a real bd binary: a fresh bootstrap with this set
    persists `dolt_mode: "server"` on its own)."""
    _patch_neighbors(monkeypatch)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: True)
    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda base: False)
    envs: list[dict] = []
    calls, fake_run = _fake_run_factory()

    def _capturing_run(cmd, **kw):
        envs.append(kw.get("env") or {})
        return fake_run(cmd, **kw)

    _patch_runs(monkeypatch, _capturing_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=False))

    assert calls[0] == ["bd", "bootstrap", "--non-interactive"]
    assert envs[0].get("BEADS_DOLT_SHARED_SERVER") == "1"


# ---------------------------------------------------------------------------
# _ensure_server_mode_persisted (constraint 1)
# ---------------------------------------------------------------------------


def test_ensure_server_mode_persisted_asserts_config_key_even_when_no_write_needed(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda base: False)
    calls, fake_run = _fake_run_factory()
    _patch_runs(monkeypatch, fake_run)

    onboard._ensure_server_mode_persisted(_ctx(tmp_path, furnish=True))

    assert any(call[-4:] == ["config", "set", "dolt.shared-server", "true"] for call in calls)
    assert capsys.readouterr().err == ""  # measured common case: silent


def test_ensure_server_mode_persisted_warns_visibly_when_it_had_to_fix_drift(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda base: True)
    calls, fake_run = _fake_run_factory()
    _patch_runs(monkeypatch, fake_run)

    onboard._ensure_server_mode_persisted(_ctx(tmp_path, furnish=True))

    assert any(call[-4:] == ["config", "set", "dolt.shared-server", "true"] for call in calls)
    err = capsys.readouterr().err
    assert "dolt_mode" in err
    assert "⚠" in err


# ---------------------------------------------------------------------------
# Automatic Dolt backups remain opt-in
# ---------------------------------------------------------------------------


def test_onboarding_does_not_enable_backup_when_a_git_remote_exists(tmp_path, monkeypatch):
    """A remote does not opt a new shared-server hive into per-worktree backup copies."""
    calls, fake_run = _fake_run_factory(git_remote_stdout="origin\n")
    _patch_runs(monkeypatch, fake_run)

    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda base: False)
    _patch_neighbors(monkeypatch)
    onboard._act_bd_init(_ctx(tmp_path, furnish=True))

    assert not any(call[:4] == ["bd", "config", "set", "backup.enabled"] for call in calls)


def test_ensure_bd_backup_gitignored_adds_rotated_store_rule_for_furnished_hive(tmp_path):
    beads = tmp_path / ".beads"
    beads.mkdir()
    gitignore = beads / ".gitignore"
    gitignore.write_text("backup/\n")

    assert hive._ensure_bd_backup_gitignored(tmp_path) is True
    assert gitignore.read_text() == "backup/\nbackup.*/\n"
    assert hive._ensure_bd_backup_gitignored(tmp_path) is False


def test_ensure_bd_backup_gitignored_uses_git_exclude_for_zero_footprint_hive(tmp_path):
    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)
    exclude = git_info / "exclude"
    exclude.write_text(
        "# Beads fork protection (bd init)\n.beads/\n**/RECOVERY*.md\n**/SESSION*.md\n.ws/\n"
    )

    assert hive._ensure_bd_backup_gitignored(tmp_path) is True
    contents = exclude.read_text()
    assert "/.beads/backup/" in contents
    assert "/.beads/backup.*/" in contents
    assert hive._remove_stealth_exclude(tmp_path) is True
    contents = exclude.read_text().splitlines()
    assert ".beads/" not in contents
    assert "/.beads/backup/" in contents
    assert "/.beads/backup.*/" in contents
    assert ".ws/" in contents
    assert hive._ensure_bd_backup_gitignored(tmp_path) is False


# ---------------------------------------------------------------------------
# Existing-hive skip path never touches any of the above (upgrade safety)
# ---------------------------------------------------------------------------


def test_existing_hive_skip_path_never_calls_server_mode_wiring(tmp_path, monkeypatch):
    """The idempotent `.beads`-exists skip must return before `bd init`, before
    `_ensure_server_mode_persisted` — an operator on an embedded hive must stay embedded across
    an upgrade (bh-areg.7's own constraint 2)."""
    (tmp_path / ".beads").mkdir()
    persisted_calls = []
    ignore_calls = []
    monkeypatch.setattr(
        onboard, "_ensure_server_mode_persisted", lambda ctx: persisted_calls.append(ctx)
    )
    monkeypatch.setattr(
        hive, "_ensure_bd_backup_gitignored", lambda base: ignore_calls.append(base)
    )
    monkeypatch.setattr(onboard, "_configure_auto_export", lambda ctx: None)
    calls, fake_run = _fake_run_factory()
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=True))

    assert calls == []  # no bd init, no bd bootstrap — nothing ran at all
    assert persisted_calls == []
    assert ignore_calls == [tmp_path]


def test_existing_hive_skip_path_leaves_dolt_mode_untouched_on_disk(tmp_path, monkeypatch):
    """Belt-and-suspenders on the real filesystem fact, not just the call-count assertion
    above: an existing embedded hive's persisted `dolt_mode` is byte-for-byte unchanged."""
    import json

    (tmp_path / ".beads").mkdir()
    metadata = tmp_path / ".beads" / "metadata.json"
    metadata.write_text(json.dumps({"dolt_mode": "embedded"}))
    monkeypatch.setattr(onboard, "_configure_auto_export", lambda ctx: None)
    monkeypatch.setattr(hive, "run", lambda cmd, **kw: _Result(returncode=0))

    onboard._act_bd_init(_ctx(tmp_path, furnish=True))

    assert store_locator.dolt_mode(tmp_path) == "embedded"


# ---------------------------------------------------------------------------
# A failed mint (e.g. a busy dolt-server port) exits legibly and cleans up after
# itself — the review's busy-port finding. See test_onboard_server_mode_int.py for
# the real-bd reproduction (a raw CalledProcessError + wreckage, before this fix).
# ---------------------------------------------------------------------------


def test_run_bd_mint_translates_a_failure_into_a_legible_exit(tmp_path, monkeypatch, capsys):
    """`check=False` + an explicit exit — never a raw `subprocess.CalledProcessError` escaping
    to bh's generic top-level handler (a traceback plus a structlog JSON blob a first-time
    user has no way to parse)."""
    monkeypatch.setattr(onboard.bd_mod, "_run", lambda cmd, **kw: _Result(returncode=1))
    monkeypatch.setattr(hive, "cleanup_failed_bd_init", lambda base: None)

    with pytest.raises(typer.Exit) as exc:
        onboard._run_bd_mint(["bd", "init"], _ctx(tmp_path, furnish=True), env={})

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "✗" in err
    assert "onboarding did not complete" in err
    # Never the raw exception shape a first-time user can't act on.
    assert "CalledProcessError" not in err
    assert "Traceback" not in err


def test_run_bd_mint_cleans_up_wreckage_before_exiting(tmp_path, monkeypatch):
    """The review's second, worse finding: wreckage left behind by a failed mint must be gone
    by the time `_run_bd_mint` returns control to the caller, so an immediate retry's
    `.beads`-exists skip is never fooled into reporting a hive ready that has no store."""
    cleaned = []
    monkeypatch.setattr(onboard.bd_mod, "_run", lambda cmd, **kw: _Result(returncode=1))
    monkeypatch.setattr(hive, "cleanup_failed_bd_init", lambda base: cleaned.append(base))

    with pytest.raises(typer.Exit):
        onboard._run_bd_mint(["bd", "init"], _ctx(tmp_path, furnish=True), env={})

    assert cleaned == [tmp_path]


def test_run_bd_mint_never_calls_cleanup_on_success(tmp_path, monkeypatch):
    cleaned = []
    monkeypatch.setattr(onboard.bd_mod, "_run", lambda cmd, **kw: _Result(returncode=0))
    monkeypatch.setattr(hive, "cleanup_failed_bd_init", lambda base: cleaned.append(base))

    onboard._run_bd_mint(["bd", "init"], _ctx(tmp_path, furnish=True), env={})

    assert cleaned == []


def test_cleanup_failed_bd_init_removes_beads_dir(tmp_path):
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / ".gitignore").write_text("*.db\n")

    hive.cleanup_failed_bd_init(tmp_path)

    assert not (tmp_path / ".beads").exists()


def test_cleanup_failed_bd_init_deletes_a_gitignore_bd_created_outright(tmp_path):
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".gitignore").write_text(
        "\n# Beads / Dolt files (added by bd init)\n.dolt/\n*.db\n"
    )

    hive.cleanup_failed_bd_init(tmp_path)

    assert not (tmp_path / ".beads").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_cleanup_failed_bd_init_relocates_a_block_appended_to_an_existing_gitignore(tmp_path):
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".gitignore").write_text(
        "node_modules/\n\n# Beads / Dolt files (added by bd init)\n.dolt/\n*.db\n"
    )

    hive.cleanup_failed_bd_init(tmp_path)

    assert not (tmp_path / ".beads").exists()
    gi = (tmp_path / ".gitignore").read_text()
    assert "node_modules/" in gi
    assert "Beads / Dolt files" not in gi
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert "Beads / Dolt files" in exclude
    assert ".dolt/" in exclude


def test_cleanup_failed_bd_init_is_a_no_op_when_nothing_to_clean(tmp_path):
    hive.cleanup_failed_bd_init(tmp_path)  # must not raise on a pristine directory
    assert not (tmp_path / ".beads").exists()
