"""Selected SQL bootstrap uses committed config/runtime without a Git HQ checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from beadhive import (
    config,
    frame_eligibility,
    gitref,
    guard,
    herdr_plugin,  # noqa: F401  # installs production compatibility ports
    host,
    host_cli,
    host_fence,
    host_lease,
    host_provision,
    hq_control_plane,
    registry,
)
from beadhive.integrations.herdr import application_services as herdr_services


def test_config_only_sql_host_has_hq_path_but_no_lease_authority(tmp_path, monkeypatch):
    hq = tmp_path / "no-git-hq"
    monkeypatch.setattr(config, "fleet_sql_selected", lambda: True)
    monkeypatch.setattr(config, "fleet_snapshot", lambda: object())
    monkeypatch.setattr(config, "hq_dir", lambda: hq)
    assert host_cli._require_hq_dir() == hq
    monkeypatch.setattr(host, "frame_binding", lambda: (True, False))
    monkeypatch.setattr(host, "host_id", lambda: "host-existing")
    plane = SimpleNamespace(
        config_backend="sql",
        load_host_manifest=lambda _host: SimpleNamespace(frame_id=None),
    )
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda _hq: plane)
    with pytest.raises(host_lease.HostLeaseRejected, match="AUTHORITY_NOT_READY"):
        host_lease.read("origin", "hive-existing", cwd=hq)
    assert not hq.exists()


def test_verified_sql_manifest_absence_never_reads_raw_git_lease(tmp_path, monkeypatch):
    hq = tmp_path / "no-git-hq"
    monkeypatch.setattr(host, "frame_binding", lambda: (True, False))
    monkeypatch.setattr(host, "host_id", lambda: "host-existing")

    def absent(_host):
        raise FileNotFoundError("verified absence")

    plane = SimpleNamespace(
        config_backend="sql",
        load_host_manifest=absent,
        verified_manifest_absence=lambda exc: str(exc) == "verified absence",
    )
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda _hq: plane)
    monkeypatch.setattr(
        gitref,
        "read_remote",
        lambda *_args, **_kwargs: pytest.fail("SQL absence fell back to Git lease"),
    )
    with pytest.raises(host_lease.HostLeaseRejected, match="AUTHORITY_NOT_READY"):
        host_lease.read("origin", "hive-existing", cwd=hq)
    assert not hq.exists()


def test_selected_sql_without_frame_binding_never_reads_raw_git_lease(tmp_path, monkeypatch):
    hq = tmp_path / "no-git-hq"
    monkeypatch.setattr(config, "fleet_sql_selected", lambda: True)
    monkeypatch.setattr(host, "frame_binding", lambda: (False, False))
    monkeypatch.setattr(host, "host_id", lambda: "host-existing")
    monkeypatch.setattr(
        gitref,
        "read_remote",
        lambda *_args, **_kwargs: pytest.fail("SQL unbound host fell back to Git lease"),
    )
    with pytest.raises(host_lease.HostLeaseRejected, match="AUTHORITY_NOT_READY"):
        host_lease.read("origin", "hv-new", cwd=hq)
    assert not hq.exists()


def test_sql_release_scan_reads_unfiltered_protected_records_without_hq_git(tmp_path, monkeypatch):
    hq = tmp_path / "no-git-hq"
    monkeypatch.setattr(config, "fleet_sql_selected", lambda: True)
    monkeypatch.setattr(config, "host_lease_renew_interval", lambda _cfg: 100)
    monkeypatch.setattr(
        registry,
        "all_hive_targets",
        lambda _cfg: iter((("hive-one", Path("unused-1")), ("hive-two", Path("unused-2")))),
    )
    lease = host_lease.HostLease(
        host_id="host-existing",
        label="existing",
        epoch=7,
        adopted_at="2050-01-01T00:00:00Z",
        expires_at="2050-01-01T01:00:00Z",
    )
    seen = []

    def read(prefix, *, holder_identity=None):
        seen.append((prefix, holder_identity))
        if prefix == "hive-two":
            raise ValueError("AUTHORITY_NOT_READY: protected lease unavailable")
        return "original-cas", lease

    monkeypatch.setattr(
        hq_control_plane,
        "control_plane",
        lambda _hq: SimpleNamespace(read_hive_lease_record=read),
    )
    held, unreadable = host_cli._scan_leases(hq, {}, host_id="host-existing")
    assert seen == [("hive-one", None), ("hive-two", None)]
    assert held == [("hive-one", lease)]
    assert unreadable == [("hive-two", "AUTHORITY_NOT_READY: protected lease unavailable")]
    assert not hq.exists()


def test_herdr_sql_adopt_keeps_existing_identity_and_requires_runtime(tmp_path, monkeypatch):
    hq = tmp_path / "no-git-hq"
    monkeypatch.setattr(config, "fleet_sql_selected", lambda: True)
    monkeypatch.setattr(config, "hq_dir", lambda: hq)
    monkeypatch.setattr(host, "host_id", lambda: "host-existing")
    manifest = SimpleNamespace(
        host_id="host-existing", frame_id="frame-existing", role="executor", label="existing"
    )
    ready = False
    plane = SimpleNamespace(
        authority_status=lambda: {"authority_ready": ready},
        load_host_manifest=lambda host_id: manifest,
    )
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda _hq: plane)
    adopted = []
    monkeypatch.setattr(
        herdr_services.host_adopt, "adopt", lambda **kw: adopted.append(kw) or object()
    )
    monkeypatch.setattr(herdr_services.registry, "hive_dir", lambda _entry: tmp_path / "hive")
    with pytest.raises(RuntimeError, match="AUTHORITY_NOT_READY"):
        herdr_services._adopt_expired_lease({}, {"prefix": "hive-existing"})
    assert adopted == []
    ready = True
    herdr_services._adopt_expired_lease({}, {"prefix": "hive-existing"})
    assert adopted[0]["host_id"] == "host-existing"
    assert adopted[0]["hq_cwd"] == hq
    assert adopted[0]["force"] is False
    assert not hq.exists()


def test_selected_sql_store_readiness_uses_engine_and_origin_facts(tmp_path, monkeypatch):
    hive = tmp_path / "hive"
    beads = hive / ".beads"
    beads.mkdir(parents=True)
    database = tmp_path / "shared" / "hive_db"
    monkeypatch.setattr(host_provision.store_locator, "database_dir", lambda _hive: database)
    calls = []

    def origin(args, **kwargs):
        calls.append((tuple(args), kwargs))
        return SimpleNamespace(returncode=0, stdout="hash\trefs/dolt/data\n")

    monkeypatch.setattr(host_provision, "run", origin)
    assert host_provision._sql_store_state(hive, {}) == host_provision.STORE_UNBOOTSTRAPPED
    assert len(calls) == 1
    monkeypatch.setattr(
        host_provision, "run", lambda *_args, **_kw: SimpleNamespace(returncode=0, stdout="")
    )
    assert host_provision._sql_store_state(hive, {}) == host_provision.STORE_UNPUBLISHED
    monkeypatch.setattr(
        host_provision, "run", lambda *_args, **_kw: SimpleNamespace(returncode=128, stdout="")
    )
    assert host_provision._sql_store_state(hive, {}) == host_provision.STORE_UNAVAILABLE

    (beads / "metadata.json").write_text('{"dolt_mode":"server"}')
    (database / ".dolt").mkdir(parents=True)

    class FakeEngine:
        def invoke(self, args, **kwargs):
            assert args == ["list", "--json", "--limit", "1", "--readonly"]
            assert kwargs["cwd"] == hive and kwargs["timeout"] == 15
            return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(host_provision.engine, "get_engine", lambda _cfg: FakeEngine())
    assert host_provision._sql_store_state(hive, {}) == host_provision.STORE_READY
    (database / ".dolt").rmdir()
    assert host_provision._sql_store_state(hive, {}) == host_provision.STORE_UNAVAILABLE


def test_selected_sql_bead_sync_dry_run_needs_no_hq_checkout(tmp_path, monkeypatch):
    hq = tmp_path / "absent-hq"
    hive = tmp_path / "hive"
    hive.mkdir()
    entry = {"prefix": "hv-new", "provider": "github", "org": "fixture", "repo": "hive"}
    monkeypatch.setattr(config, "hq_dir", lambda: hq)
    monkeypatch.setattr(config, "fleet_sql_selected", lambda: True)
    monkeypatch.setattr(host_provision, "_cfg_or_none", lambda: {"managed_repos": [entry]})
    monkeypatch.setattr(host_provision, "_present_hive_entries", lambda _cfg: [entry])
    monkeypatch.setattr(registry, "hive_dir", lambda _entry: hive)
    monkeypatch.setattr(
        host_provision,
        "_sql_store_state",
        lambda _hive, _cfg: host_provision.STORE_UNBOOTSTRAPPED,
    )
    outcome = host_provision._step_bead_sync(dry_run=True)
    assert outcome.status == "would" and "would bootstrap first" in outcome.detail
    assert not hq.exists()


def test_selected_sql_hydrates_once_then_reuses_engine_store_without_hq_git(tmp_path, monkeypatch):
    hq = tmp_path / "absent-hq"
    hive = tmp_path / "hive"
    hive.mkdir()
    database = tmp_path / "shared" / "bh"
    entry = {"prefix": "bh", "provider": "github", "org": "fixture", "repo": "hive-bh"}
    cfg = {"managed_repos": [entry]}
    monkeypatch.setattr(config, "hq_dir", lambda: hq)
    monkeypatch.setattr(config, "fleet_sql_selected", lambda: True)
    monkeypatch.setattr(host_provision, "_cfg_or_none", lambda: cfg)
    monkeypatch.setattr(registry, "hive_dir", lambda _entry: hive)
    monkeypatch.setattr(host_provision.store_locator, "database_dir", lambda _hive: database)
    monkeypatch.setattr(
        host_provision,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="hash\trefs/dolt/data\n"),
    )
    actions = []

    class FakeEngine:
        def invoke(self, args, **kwargs):
            actions.append(("probe", tuple(args)))
            assert kwargs["cwd"] == hive and kwargs["timeout"] == 15
            return SimpleNamespace(returncode=0, stdout="[]")

        def bootstrap(self, target):
            assert target == hive and not database.exists()
            actions.append(("bootstrap", target))
            (hive / ".beads").mkdir()
            (hive / ".beads" / "metadata.json").write_text('{"dolt_mode":"server"}')
            (database / ".dolt").mkdir(parents=True)
            return SimpleNamespace(returncode=0, stderr="")

    engine = FakeEngine()
    monkeypatch.setattr(host_provision.engine, "get_engine", lambda _cfg: engine)
    monkeypatch.setattr(
        host_provision.hive_sync,
        "hive_sync",
        lambda *, hive_id: actions.append(("sync", hive_id)) or [],
    )
    first = host_provision._step_bead_sync(dry_run=False)
    second = host_provision._step_bead_sync(dry_run=False)
    assert first.status == second.status == "done"
    assert actions == [
        ("bootstrap", hive),
        ("sync", "bh"),
        ("probe", ("list", "--json", "--limit", "1", "--readonly")),
        ("sync", "bh"),
    ]
    assert not hq.exists()


def test_selected_sql_adopt_release_packup_and_herdr_keep_hive_fence_without_hq_git(
    tmp_path, monkeypatch
):
    hq = tmp_path / "absent-hq"
    origin = tmp_path / "hive.git"
    hive = tmp_path / "hive"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(hive)], check=True)
    subprocess.run(["git", "-C", str(hive), "remote", "add", "origin", str(origin)], check=True)
    entry = {"provider": "github", "org": "fixture", "repo": "hive", "prefix": "hv-new"}
    cfg = {"managed_repos": [entry]}
    monkeypatch.setattr(config, "fleet_sql_selected", lambda: True)
    monkeypatch.setattr(config, "hq_dir", lambda: hq)
    monkeypatch.setattr(config, "fleet_snapshot", lambda: object())
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(host, "host_id", lambda: "host-existing")
    monkeypatch.setattr(host, "frame_binding", lambda: (True, True))
    monkeypatch.setattr(registry, "hive_dir", lambda _entry: hive)
    monkeypatch.setattr(frame_eligibility, "require_eligible", lambda *_a, **_kw: object())
    manifest = SimpleNamespace(
        host_id="host-existing", frame_id="frame-existing", role="executor", label="existing"
    )

    class ProtectedPlane:
        config_backend = "sql"

        def __init__(self):
            self.sha = ""
            self.lease = None
            self.history = []

        def load_host_manifest(self, _host):
            return manifest

        def authority_status(self):
            return {"authority_ready": True}

        def read_hive_lease_record(self, _prefix, *, holder_identity=None, incumbent_identity=None):
            assert incumbent_identity in (None, "host-existing")
            return self.sha, self.lease

        def publish_hive_lease(self, prefix, lease, *, expected, operation, force=False):
            assert prefix == "hv-new" and expected == self.sha
            assert operation in {"adopt", "release"}
            self.sha = f"protected-{len(self.history) + 1}"
            self.lease = lease
            self.history.append((operation, lease.epoch, lease.host_id))
            return self.sha

    plane = ProtectedPlane()
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda _hq: plane)
    real_set_local = gitref.set_local

    def hive_only_ref(*args, cwd):
        assert cwd == hive, "SQL lease attempted HQ Git cache"
        return real_set_local(*args, cwd=cwd)

    monkeypatch.setattr(gitref, "set_local", hive_only_ref)

    host_cli.adopt_cmd(hive="hv-new", force=False)
    _sha, fence = host_fence.read_fence("origin", cwd=hive)
    assert (fence.epoch, fence.host_id) == (1, "host-existing")
    assert plane.history == [("adopt", 1, "host-existing")]
    assert not hq.exists()

    # Currentness and claim-token epoch use the authenticated SQL lease, not
    # the missing local HQ ref. The full signed runtime is qualified by its
    # dedicated Dolt fixture; this exercises the public lifecycle composition.
    monkeypatch.setattr(frame_eligibility, "require_intake", lambda *_a, **_kw: object())
    monkeypatch.setattr(
        frame_eligibility,
        "authoritative_primary",
        lambda *_a, **_kw: ("hv-new", "host-existing", plane.lease),
    )
    assert guard.live_epoch("hv-new", cfg=cfg) == 1
    claimed = SimpleNamespace(
        bead="hv-new-1",
        seat="dev/existing",
        host_id="host-existing",
        epoch=1,
        worktree=hive,
        is_fenced=lambda: True,
        is_stale=lambda current: current > 1,
    )
    monkeypatch.setattr(
        frame_eligibility,
        "require_intake",
        lambda *_a, **_kw: pytest.fail("existing-claim epoch check requested new intake"),
    )
    guard.guard_claim_epoch(claimed, "hv-new", cfg=cfg, verb="work submit")

    host_cli.release_cmd(hive="hv-new", all_hives=False)
    assert plane.lease.is_tombstone and plane.lease.epoch == 1
    herdr_services._adopt_expired_lease(cfg, entry)
    assert plane.lease.host_id == "host-existing" and plane.lease.epoch == 2
    with pytest.raises(typer.Exit):
        guard.guard_claim_epoch(claimed, "hv-new", cfg=cfg, verb="work submit")
    _sha, fence = host_fence.read_fence("origin", cwd=hive)
    assert (fence.epoch, fence.host_id) == (2, "host-existing")
    host_cli.packup_cmd()
    assert plane.lease.is_tombstone and plane.lease.epoch == 2
    assert plane.history == [
        ("adopt", 1, "host-existing"),
        ("release", 1, ""),
        ("adopt", 2, "host-existing"),
        ("release", 2, ""),
    ]
    assert not hq.exists()


def test_selected_sql_dispatch_enable_requires_protected_lease_not_git_default(
    tmp_path, monkeypatch
):
    hq = tmp_path / "absent-hq"
    entry = {"provider": "github", "org": "fixture", "repo": "hive", "prefix": "bh"}
    cfg = {"managed_repos": [entry]}
    monkeypatch.setattr(config, "fleet_sql_selected", lambda: True)
    monkeypatch.setattr(config, "hq_dir", lambda: hq)
    monkeypatch.setattr(host, "host_id", lambda: "host-existing")
    monkeypatch.setattr(
        guard,
        "primary_state",
        lambda *_a, **_kw: pytest.fail("SQL enable used absent-HQ Git default"),
    )
    monkeypatch.setattr(
        frame_eligibility,
        "require_eligible",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            frame_eligibility.EligibilityError("frame draining")
        ),
    )
    monkeypatch.setattr(
        host_lease,
        "read",
        lambda *_a, **_kw: pytest.fail("unready SQL frame read a lease"),
    )
    allowed, detail = host_cli._ensure_lease_for_enable("bh", cfg)
    assert not allowed and "AUTHORITY_NOT_READY" in detail

    monkeypatch.setattr(frame_eligibility, "require_eligible", lambda *_a, **_kw: object())
    lease = host_lease.HostLease(
        host_id="host-existing",
        label="existing",
        epoch=7,
        adopted_at="2050-01-01T00:00:00Z",
        expires_at="2050-01-01T01:00:00Z",
    )
    monkeypatch.setattr(host_lease, "read", lambda *_a, **_kw: lease)
    allowed, detail = host_cli._ensure_lease_for_enable("bh", cfg)
    assert allowed and "lease already held" in detail
    foreign = host_lease.HostLease("other-host", "existing", 7, lease.adopted_at, lease.expires_at)
    monkeypatch.setattr(host_lease, "read", lambda *_a, **_kw: foreign)
    allowed, detail = host_cli._ensure_lease_for_enable("bh", cfg)
    assert not allowed and "lease held elsewhere" in detail
    assert not hq.exists()
