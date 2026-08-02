"""`bh host retire` (bh-twc8.2) — the host-scope safety verdict + guarded ordered teardown.

Covers the acceptance bar directly:
  * `assess()` folds every hive (`safety.assess_retire`), every managed worktree
    (classification-based, catching risk a plain git dirty/ahead check misses), every held or
    unreadable lease, and Factory HQ's own ahead/behind on BOTH halves into ONE
    SAFE/NEEDS_BACKUP/BLOCKED verdict.
  * the gate mirrors `bh hive retire`'s own posture: SAFE proceeds; NEEDS_BACKUP needs
    `--backup`/`--confirm`; BLOCKED needs `--confirm` — even under `--dry-run`.
  * `--dry-run` prints the full ordered plan and mutates NOTHING.
  * a live run releases held leases, syncs+pushes every hive, reclaims local clones/worktrees,
    deregisters this host's manifest, and pushes HQ — in that order.
  * THE critical scope constraint: host retire NEVER touches `managed_repos` (fleet-wide
    registration) — a hive is reclaimed host-locally (`retire.reclaim_hive`) but stays
    registered for the fleet.

Real `git` builds hive clones + Factory HQ + their bare remotes (mirrors test_sync_remote.py /
test_hq_push.py's style). `bd`'s Dolt state is FAKED via `beadhive.engine.get_engine` (the
`_StubEngine` pattern those files already use) — no real `bd`/network dependency. The `world`
fixture isolates `$GIT_WORKSPACE`/`BH_HOME`/`config.hq_dir()` under a pytest tmp_path — never
the operator's real `~/.beadhive`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from beadhive import config, host, host_cli, host_lease, host_retire, hosts
from beadhive import engine as engine_mod
from beadhive.cli import app
from beadhive.engine import FederationPeer, FederationStatus
from beadhive.identity import workspace_root
from beadhive.safety import RetireVerdict
from beadhive.wt_status import WtClassification, WtStatus
from harness.world import git

runner = CliRunner()

HOST_A = "11111111-1111-4111-8111-111111111111"

# `host_lease._parse_stamp` round-trips an ISO stamp through `time.mktime`/`time.timezone`,
# which is DST-naive: parsing a stamp minted while the host's local zone observes DST comes
# back an hour off (`time.timezone` is always the STANDARD offset). Pinning the clock to a
# January instant (no US DST) sidesteps it — the SAME convention test_host_lease_cli.py /
# test_host_remove_cli.py already use, for the same reason.
T0 = 1_800_000_000.0


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_clean_hive(org="myorg", repo="myrepo") -> tuple[Path, Path]:
    root = Path(workspace_root())
    remote = root / "_remotes" / f"{repo}.git"
    remote.mkdir(parents=True)
    git("init", "-q", "--bare", "-b", "main", cwd=remote)
    clone = root / "github" / org / repo
    clone.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=clone)
    git("config", "user.email", "t@fixture", cwd=clone)
    git("config", "user.name", "T", cwd=clone)
    (clone / "f.txt").write_text("x")
    git("add", "-A", cwd=clone)
    git("commit", "-qm", "init", cwd=clone)
    git("remote", "add", "origin", str(remote), cwd=clone)
    git("push", "-q", "-u", "origin", "main", cwd=clone)
    return clone, remote


def _make_dirty_hive(org="myorg", repo="myrepo") -> tuple[Path, Path]:
    clone, remote = _make_clean_hive(org=org, repo=repo)
    (clone / "f.txt").write_text("uncommitted drift")
    return clone, remote


def _register_hive(prefix="mr", org="myorg", repo="myrepo", provider="github") -> None:
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": provider, "org": org, "repo": repo, "prefix": prefix, "kind": "personal"}
    )
    config.save(cfg)


def _init_hq_with_remote(world) -> tuple[Path, Path]:
    hq_dir = config.hq_dir()
    hq_dir.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=hq_dir)
    git("config", "user.email", "hq@fixture", cwd=hq_dir)
    git("config", "user.name", "HQ Fixture", cwd=hq_dir)
    (hq_dir / ".beads").mkdir()
    (hq_dir / "note.txt").write_text("hq\n")
    git("add", "-A", cwd=hq_dir)
    git("commit", "-qm", "init", cwd=hq_dir)
    remote = world.remotes / "hq.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=world.remotes)
    git("remote", "add", "origin", str(remote), cwd=hq_dir)
    git("push", "-q", "-u", "origin", "main", cwd=hq_dir)
    return hq_dir, remote


def _mint_host(monkeypatch, host_id=HOST_A, label="fixture-host") -> None:
    monkeypatch.setattr(host, "load", lambda: {"host_id": host_id, "label": label})


def _write_manifest(hq_dir, host_id, *, label="fixture-host", role="worker", push=True) -> Path:
    manifest = hosts.HostManifest(
        host_id=host_id, label=label, os="darwin", arch="arm64", role=role,
        identity=hosts.IdentityMechanism(kind="none", value=""),
    )
    path = hosts.save(hq_dir, manifest)
    git("add", "-A", cwd=hq_dir)
    git("commit", "-qm", f"chore(host): seed {host_id}", cwd=hq_dir)
    if push:
        git("push", "-q", "origin", "main", cwd=hq_dir)
    return path


def _stub_engine(monkeypatch, *, ahead=0, behind=0, reachable=True, push_ok=True):
    """Fake the Dolt/federation half of every `.beads`-carrying fixture (HQ here — hive fixtures
    are plain git repos with no `.beads`, so they never touch this seam at all)."""
    fed = FederationStatus(
        ok=reachable,
        error="" if reachable else "unreachable",
        peers=(
            (FederationPeer(peer="origin", url="x", reachable=True, ahead=ahead, behind=behind),)
            if reachable
            else ()
        ),
    )

    class _Stub:
        def federation_status(self, cwd, *, timeout=None):
            return fed

        def push_state(self, cwd, actor="", message=""):
            rc = 0 if push_ok else 1
            err = "" if push_ok else "boom"
            return subprocess.CompletedProcess(["bd", "dolt", "push"], rc, "", err)

    monkeypatch.setattr(engine_mod, "get_engine", lambda cfg=None: _Stub())
    return fed


def _clean_world(world, monkeypatch, *, host_id=HOST_A):
    """One clean, pushed hive + a clean, pushed HQ + this host's own manifest — the fully-SAFE
    baseline several tests start from."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0)  # see T0's docstring above
    _make_clean_hive()
    _register_hive()
    hq_dir, hq_remote = _init_hq_with_remote(world)
    _mint_host(monkeypatch, host_id)
    _write_manifest(hq_dir, host_id)
    _stub_engine(monkeypatch)
    return hq_dir, hq_remote


# ---------------------------------------------------------------------------
# assess() — the folded verdict (read-only)
# ---------------------------------------------------------------------------


def test_assess_is_safe_for_a_fully_clean_host(world, monkeypatch):
    _clean_world(world, monkeypatch)

    a = host_retire.assess()

    assert a.verdict == RetireVerdict.SAFE
    assert a.hives[0].present is True
    assert a.hives[0].verdict == RetireVerdict.SAFE
    assert a.hq_verdict == RetireVerdict.SAFE
    assert a.leases_held == []
    assert a.leases_unreadable == []


def test_assess_folds_a_dirty_hive_as_needs_backup(world, monkeypatch):
    _make_dirty_hive()
    _register_hive()
    hq_dir, _remote = _init_hq_with_remote(world)
    _mint_host(monkeypatch)
    _write_manifest(hq_dir, HOST_A)
    _stub_engine(monkeypatch)

    a = host_retire.assess()

    assert a.verdict == RetireVerdict.NEEDS_BACKUP
    assert a.hives[0].verdict == RetireVerdict.NEEDS_BACKUP
    assert any("uncommitted" in r for r in a.hives[0].reasons)


def test_assess_skips_a_hive_with_no_local_clone(world, monkeypatch):
    """`managed_repos` is fleet-wide — a hive registered but never cloned on THIS host is not
    this host's problem: it must never drag the verdict down."""
    _register_hive()  # registered, but never cloned under workspace_root()
    hq_dir, _remote = _init_hq_with_remote(world)
    _mint_host(monkeypatch)
    _write_manifest(hq_dir, HOST_A)
    _stub_engine(monkeypatch)

    a = host_retire.assess()

    assert a.verdict == RetireVerdict.SAFE
    assert a.hives[0].present is False


def test_assess_escalates_unreadable_leases_to_blocked(world, monkeypatch):
    _clean_world(world, monkeypatch)
    monkeypatch.setattr(
        host_cli.host_lease, "read",
        lambda *a, **k: (_ for _ in ()).throw(host_cli.gitref.RemoteUnreachable("boom")),
    )

    a = host_retire.assess()

    assert a.verdict == RetireVerdict.BLOCKED
    assert a.leases_unreadable and a.leases_unreadable[0][0] == "mr"


def test_assess_reports_a_held_lease_without_escalating_the_verdict(world, monkeypatch):
    """A lease THIS host holds is releasable (step 1 of the pipeline does it safely) — not a
    data-loss risk on its own, so it must not force NEEDS_BACKUP by itself."""
    hq_dir, _remote = _clean_world(world, monkeypatch)
    host_lease.adopt("origin", "mr", host_id=HOST_A, label="fixture-host", cwd=hq_dir, ttl=600.0)

    a = host_retire.assess()

    assert a.verdict == RetireVerdict.SAFE
    assert [p for p, _lease in a.leases_held] == ["mr"]


def test_assess_folds_hq_git_ahead_as_needs_backup(world, monkeypatch):
    hq_dir, _remote = _clean_world(world, monkeypatch)
    (hq_dir / "note.txt").write_text("drift\n")
    git("commit", "-aqm", "drift", cwd=hq_dir)

    a = host_retire.assess()

    assert a.verdict == RetireVerdict.NEEDS_BACKUP
    assert a.hq_verdict == RetireVerdict.NEEDS_BACKUP
    assert any("ahead" in r for r in a.hq_reasons)


def test_assess_folds_hq_dolt_ahead_as_needs_backup(world, monkeypatch):
    _clean_world(world, monkeypatch)
    _stub_engine(monkeypatch, ahead=3)

    a = host_retire.assess()

    assert a.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("dolt" in r.lower() for r in a.hq_reasons)


def test_assess_escalates_on_an_active_worktree_classification(world, monkeypatch):
    """An open-bead worktree can be perfectly git-clean+pushed — `assess_retire` alone would
    call it SAFE. The worktree-classification fold is what catches this."""
    _clean_world(world, monkeypatch)
    st = WtStatus(
        hive="mr", leaf="bh-1", branch="wt/bead/issue/bh-1",
        path=str(Path(workspace_root()) / "github" / "myorg" / "myrepo-wt"),
        bead_id="bh-1", classification=WtClassification.ACTIVE,
        merged=False, dirty=False, safe=False,
    )
    monkeypatch.setattr(
        host_retire.worktree, "managed", lambda cfg: [("mr", st.path, st.branch)]
    )
    monkeypatch.setattr(
        host_retire.worktree, "_classify_entry", lambda entry, rows, cfg: [st]
    )

    a = host_retire.assess()

    assert a.verdict == RetireVerdict.NEEDS_BACKUP
    assert a.worktrees_at_risk == [st]


def test_assess_requires_a_local_hq_clone(world):
    with pytest.raises(typer.Exit):
        host_retire.assess()


# ---------------------------------------------------------------------------
# _gate — consent gate mirroring `bh hive retire`'s SAFE/NEEDS_BACKUP/BLOCKED posture
# ---------------------------------------------------------------------------


def _assessment(verdict: RetireVerdict) -> host_retire.HostAssessment:
    return host_retire.HostAssessment(host_id=HOST_A, hq_dir="/tmp/hq", verdict=verdict)


def test_gate_safe_proceeds_without_any_flags():
    host_retire._gate(_assessment(RetireVerdict.SAFE), backup=False, confirm=False)  # no raise


def test_gate_needs_backup_refuses_without_backup_or_confirm():
    with pytest.raises(typer.Exit):
        host_retire._gate(_assessment(RetireVerdict.NEEDS_BACKUP), backup=False, confirm=False)


def test_gate_needs_backup_passes_with_backup():
    host_retire._gate(_assessment(RetireVerdict.NEEDS_BACKUP), backup=True, confirm=False)


def test_gate_needs_backup_passes_with_confirm():
    host_retire._gate(_assessment(RetireVerdict.NEEDS_BACKUP), backup=False, confirm=True)


def test_gate_blocked_refuses_without_confirm():
    with pytest.raises(typer.Exit):
        host_retire._gate(_assessment(RetireVerdict.BLOCKED), backup=True, confirm=False)


def test_gate_blocked_passes_with_confirm():
    host_retire._gate(_assessment(RetireVerdict.BLOCKED), backup=False, confirm=True)


# ---------------------------------------------------------------------------
# retire() / `bh host retire` — the guarded, ordered pipeline
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_full_ordered_plan_and_mutates_nothing(world, monkeypatch):
    hq_dir, _hq_remote = _clean_world(world, monkeypatch)
    hive_clone = Path(workspace_root()) / "github" / "myorg" / "myrepo"
    before_repos = list(config.load()["managed_repos"])
    before_manifest = hosts.load(hq_dir, HOST_A)

    result = runner.invoke(app, ["host", "retire", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    for step in host_retire.PLAN:
        assert step in result.output
    assert hive_clone.exists()  # nothing reclaimed
    assert config.load()["managed_repos"] == before_repos  # registry byte-identical
    assert hosts.load(hq_dir, HOST_A) == before_manifest  # manifest untouched


def test_retire_releases_a_held_lease(world, monkeypatch):
    hq_dir, _remote = _clean_world(world, monkeypatch)
    host_lease.adopt("origin", "mr", host_id=HOST_A, label="fixture-host", cwd=hq_dir, ttl=600.0)

    result = runner.invoke(app, ["host", "retire"])

    assert result.exit_code == 0, result.output
    assert host_lease.read("origin", "mr", cwd=hq_dir).is_tombstone


def test_retire_reclaims_the_hive_but_leaves_managed_repos_byte_identical(world, monkeypatch):
    """THE scope constraint: `bh host retire` must NEVER touch fleet-wide registration —
    only this host's own clone/worktrees, leases, and manifest."""
    hq_dir, _remote = _clean_world(world, monkeypatch)
    hive_clone = Path(workspace_root()) / "github" / "myorg" / "myrepo"
    before_repos = list(config.load()["managed_repos"])

    result = runner.invoke(app, ["host", "retire"])

    assert result.exit_code == 0, result.output
    assert not hive_clone.exists()  # reclaimed (soft-archived) locally
    assert config.load()["managed_repos"] == before_repos  # still registered for the fleet


def test_retire_deregisters_the_manifest_and_pushes_hq(world, monkeypatch):
    hq_dir, hq_remote = _clean_world(world, monkeypatch)

    result = runner.invoke(app, ["host", "retire"])

    assert result.exit_code == 0, result.output
    with pytest.raises(FileNotFoundError):
        hosts.load(hq_dir, HOST_A)
    remote_head = git("rev-parse", "main", cwd=hq_remote).stdout.strip()
    local_head = git("rev-parse", "main", cwd=hq_dir).stdout.strip()
    assert remote_head == local_head  # the deregistration commit reached the remote


def test_retire_refuses_when_a_hive_is_dirty_without_backup_or_confirm(world, monkeypatch):
    _make_dirty_hive()
    _register_hive()
    hq_dir, _remote = _init_hq_with_remote(world)
    _mint_host(monkeypatch)
    _write_manifest(hq_dir, HOST_A)
    _stub_engine(monkeypatch)

    result = runner.invoke(app, ["host", "retire"])

    assert result.exit_code == 1
    assert "--backup" in result.output or "--confirm" in result.output
    assert hosts.load(hq_dir, HOST_A) is not None  # manifest untouched


def test_retire_confirm_completes_despite_a_dirty_hive(world, monkeypatch):
    _make_dirty_hive()
    _register_hive()
    hq_dir, _remote = _init_hq_with_remote(world)
    _mint_host(monkeypatch)
    _write_manifest(hq_dir, HOST_A)
    _stub_engine(monkeypatch)

    result = runner.invoke(app, ["host", "retire", "--confirm"])

    assert result.exit_code == 0, result.output
    with pytest.raises(FileNotFoundError):
        hosts.load(hq_dir, HOST_A)


def test_retire_cli_exits_nonzero_when_a_step_fails(world, monkeypatch):
    hq_dir, _remote = _clean_world(world, monkeypatch)
    monkeypatch.setattr(
        host_retire.hosts, "remove",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    result = runner.invoke(app, ["host", "retire"])

    assert result.exit_code == 1
    assert "incomplete" in result.output


def test_no_open_hq_no_local_store_refuses_cleanly(world, monkeypatch):
    _mint_host(monkeypatch)

    result = runner.invoke(app, ["host", "retire", "--dry-run"])

    assert result.exit_code == 1
    assert "Factory HQ" in result.output
