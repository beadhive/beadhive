"""Composed public SQL seed and signed frame lifecycle on one isolated HQ.

Only the server-local runtime schema and credentials are fixture provisioned. Config
starts from the public empty-store seed; frames use their signed public inboxes and
the separately credentialed receiver/operator, with one shared test clock.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pymysql
import pytest
import yaml

from beadhive import (
    config,
    frame_eligibility,
    host,
    host_provision,
    hq_control_plane,
    hq_sql_config,
    hq_sql_runtime,
    localloop,
    registry,
    store_locator,
)
from beadhive import hq_authority_guard as authority_guard
from beadhive.beadyard_identity import parse_document
from beadhive.beadyard_identity_file import create_identity
from beadhive.frame_eligibility import EligibilityFacts, eligible
from beadhive.host_fence import EpochFence
from beadhive.host_heartbeat_core import HeartbeatLease, ObservationAuthority
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hosts import HostManifest
from beadhive.hq_control_plane import SqlControlPlane
from beadhive.hq_hive_policy import project_hive_policies
from beadhive.hq_seed import apply, plan, prepare
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from beadhive.hq_sql_operator import SqlRuntimeOperator
from beadhive.hq_sql_receiver import SqlTrustedReceiver
from beadhive.hq_sql_runtime_schema import COMMITTED_SCHEMA, PROTECTED_LIVE_SCHEMA, inbox_ddl
from beadhive.hq_sql_signatures import canonical, fingerprint, sign_authority
from beadhive.modules.config.domain.ports import FleetConfigDocument
from harness.beads import skip_if_no_bd
from test_hq_sql_config_int import _binding, _Broker, _cli, _empty_config_server

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]


class Clock:
    def __init__(self):
        self.value = float(int(time.time()))

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _git(repo: Path, *args: str):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _key(path: Path):
    result = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return Path(str(path) + ".pub").read_text().strip()


def _manifest(owner: str, frame: str, host: str, instance: str) -> HostManifest:
    return HostManifest.model_validate(
        {
            "frame_id": frame,
            "host_id": host,
            "instance_ref": instance,
            "beadyard_id": owner,
            "state": "pending",
            "label": host,
            "os": "linux",
            "arch": "x86_64",
            "role": "executor",
            "identity": {"kind": "none", "value": ""},
            "release": {"id": "fixture", "digest": "sha256:" + "1" * 64},
            "capabilities": {
                "isolation": "container",
                "trust_zone": "self-hosted",
                "arch": "x86_64",
                "harnesses": ["claude"],
                "max_sessions": 1,
            },
        }
    )


def _source_git(tmp_path: Path):
    hq = tmp_path / "git-hq"
    hq.mkdir()
    _git(hq, "init", "-q", "-b", "main")
    _git(hq, "config", "user.name", "Fixture")
    _git(hq, "config", "user.email", "fixture@example.invalid")
    owner = create_identity(hq)
    assert parse_document((hq / "beadyard.json").read_text()) == owner
    (hq / "fleet.yaml").write_text(
        "schema_version: 1\nhq:\n  mode: git\nmanaged_repos:\n"
        "  - provider: github\n    org: fixture\n    repo: hive-bh\n"
        "    prefix: bh\n    kind: org-native\n"
        "    frame_policy:\n      config_revision: desired-1\n"
        "      requires: {max_sessions: 1}\n      evict_after_s: 301\n"
    )
    manifests = {
        "a": _manifest(owner, "frame-a", "host-a", "vm-a"),
        "b": _manifest(owner, "frame-b", "host-b", "vm-b"),
        "a2": _manifest(owner, "frame-a", "host-a2", "vm-a2"),
    }
    (hq / "hosts").mkdir()
    for manifest in manifests.values():
        (hq / "hosts" / f"{manifest.host_id}.yaml").write_text(
            json.dumps(manifest.model_dump(mode="json", exclude_none=True))
        )
    _git(hq, "add", "beadyard.json", "fleet.yaml", "hosts")
    _git(hq, "commit", "-qm", "fixture public source")
    return hq, owner, manifests


def _root(port: int, database: str):
    return pymysql.connect(
        host="127.0.0.1", port=port, user="root", database=database, autocommit=True
    )


def _runtime_binding(tmp_path: Path, port: int, user: str):
    return {**_binding(tmp_path, port, user), "database": "beadhive_hq_runtime"}


def _runtime_schema(tmp_path: Path, port: int):
    _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_runtime")
    _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(COMMITTED_SCHEMA))
    _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(PROTECTED_LIVE_SCHEMA))


def _grant_config_read(tmp_path: Path, port: int, user: str):
    _cli(
        tmp_path,
        port,
        "; ".join(
            f"GRANT SELECT ON beadhive_hq_config.{table} TO '{user}'@'localhost'"
            for table in (
                "hq_config_meta",
                "hq_config_documents",
                "hq_config_publications",
            )
        ),
    )


def _runtime_roles(tmp_path: Path, port: int):
    grants = {
        "observer": (
            ("SELECT", "hq_authority"),
            ("SELECT", "hq_principal_registry"),
            ("SELECT,INSERT", "hq_live_receipts"),
            ("SELECT,UPDATE", "hq_live_floors"),
            ("SELECT,INSERT", "hq_live_results"),
            ("SELECT,INSERT,UPDATE", "hq_live_hive_leases"),
            ("SELECT,INSERT,UPDATE", "hq_live_public_observations"),
            ("SELECT,INSERT,UPDATE", "hq_live_registrations"),
        ),
        "authority_writer": (
            ("SELECT,UPDATE", "hq_authority"),
            ("SELECT,INSERT", "hq_principal_registry"),
            ("SELECT,INSERT", "hq_live_floors"),
            ("SELECT", "hq_live_receipts"),
            ("SELECT", "hq_live_public_observations"),
            ("SELECT", "hq_live_registrations"),
            ("SELECT", "dolt_status"),
        ),
    }
    statements = [
        f"CREATE USER '{user}'@'localhost' IDENTIFIED BY 'fixture-secret'" for user in grants
    ]
    for user, permissions in grants.items():
        statements.extend(
            f"GRANT {verbs} ON beadhive_hq_runtime.{table} TO '{user}'@'localhost'"
            for verbs, table in permissions
        )
    statements.extend(
        f"GRANT EXECUTE ON PROCEDURE beadhive_hq_runtime.{procedure} "
        "TO 'authority_writer'@'localhost'"
        for procedure in ("dolt_add", "dolt_commit")
    )
    _cli(tmp_path, port, "; ".join(statements))
    for user in grants:
        _grant_config_read(tmp_path, port, user)


def _frame_role(tmp_path: Path, port: int, principal: str, epoch: int):
    inbox = f"hq_live_inbox_{principal}_{epoch}"
    _cli(tmp_path, port, "USE beadhive_hq_runtime; " + inbox_ddl(principal, epoch))
    permissions = (
        ("SELECT", "hq_authority"),
        ("SELECT", "hq_principal_registry"),
        ("SELECT,INSERT,UPDATE", inbox),
        ("SELECT", "hq_live_results"),
        ("SELECT", "hq_live_hive_leases"),
        ("SELECT", "hq_live_public_observations"),
    )
    statements = [f"CREATE USER '{principal}'@'localhost' IDENTIFIED BY 'fixture-secret'"]
    statements.extend(
        f"GRANT {verbs} ON beadhive_hq_runtime.{table} TO '{principal}'@'localhost'"
        for verbs, table in permissions
    )
    statements.append(f"GRANT SELECT ON beadhive_hq_runtime.{inbox} TO 'observer'@'localhost'")
    _cli(tmp_path, port, "; ".join(statements))
    _grant_config_read(tmp_path, port, principal)
    return inbox


def _request_id(port: int, inbox: str, digest: str):
    connection = _root(port, "beadhive_hq_runtime")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT request_id FROM {inbox} WHERE payload_sha256=%s",
                (digest.removeprefix("sha256:"),),
            )
            row = cursor.fetchone()
            assert row is not None
            return row[0]
    finally:
        connection.close()


def _signed_initial_runtime(port, snapshot, owner, operator_key, operator_public, clock):
    policies = project_hive_policies(snapshot, valid_until=clock() + 3600, now=clock())
    state = {
        "domain": authority_guard.DOMAIN,
        "generation": "fixture-runtime-generation",
        "revision": 1,
        "issued_at": clock() - 1,
        "expires_at": clock() + 3600,
        "frames": {},
    }
    authority_guard.validate_state(state)
    signed = {
        "backend_identity": "fixture-runtime-backend",
        "generation": state["generation"],
        "revision": 1,
        "config_backend": snapshot.backend_identity,
        "config_generation": snapshot.generation,
        "config_head": snapshot.commit_revision,
        "state": state,
        "hive_policies": policies,
    }
    signature = sign_authority(signed, signing_key=str(operator_key))
    connection = _root(port, "beadhive_hq_runtime")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO hq_authority VALUES (1,1,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    signed["backend_identity"],
                    signed["generation"],
                    signed["config_backend"],
                    signed["config_generation"],
                    signed["config_head"],
                    canonical(state),
                    hashlib.sha256(canonical(state)).hexdigest(),
                    canonical(policies),
                    hashlib.sha256(canonical(policies)).hexdigest(),
                    signature,
                ),
            )
            cursor.execute("CALL DOLT_ADD('hq_authority','hq_principal_registry')")
            cursor.execute(
                "CALL DOLT_COMMIT('-m','fixture signed initial runtime','--author',"
                "'Fixture <fixture@example.invalid>')"
            )
            cursor.execute("SELECT DOLT_HASHOF('HEAD')")
            return cursor.fetchone()[0]
    finally:
        connection.close()


def _signed_beat(frame, owner, clock, sequence, *, state="active", release=None):
    manifest = frame["manifest"]
    beat = HeartbeatLease(
        domain="beadhive/frame-heartbeat/v2",
        beadyard_id=owner,
        audience="fixture-fleet",
        frame_id=manifest.frame_id,
        holderIdentity=manifest.host_id,
        instance_ref=manifest.instance_ref,
        key_id=frame["authority"].key_fingerprint,
        epoch=frame["authority"].epoch,
        config_revision="desired-1",
        seq=sequence,
        renewTime=datetime.fromtimestamp(clock(), UTC).isoformat(),
        state_seen=state,
        release=release or manifest.release.model_dump(),
        report_digest="sha256:" + "2" * 64,
        conformance={
            "profile": "fixture",
            "status": "conformant",
            "checks": [{"id": "required", "status": "pass"}],
        },
    )
    digest = frame["plane"].heartbeat(beat, signing_key=str(frame["key"]))
    frame["beat"] = beat
    return digest


def _observed_predicates(frame, policy):
    _head, desired, observation = frame["plane"].read_eligibility(frame["manifest"])
    decision = eligible(frame["manifest"], policy, EligibilityFacts(observation, desired))
    predicates = dict(decision.predicates)
    # Use the same qualified, holder-filtered protected read as the production
    # intake port. The physical unfiltered row is checked separately below:
    # an incumbent may remain recorded while its new-intake right is revoked.
    qualified = frame["plane"].read_hive_lease_record(
        "bh", holder_identity=frame["manifest"].host_id
    )[1]
    predicates["current_hive_lease_holder"] = qualified is not None and qualified.held_by(
        frame["manifest"].host_id
    )
    assert set(predicates) == {
        "authority_available",
        "admitted_active",
        "not_cordoned",
        "reviewed_admission_or_emergency",
        "current_frame_incarnation",
        "beadyard_binding",
        "authenticated_fresh_heartbeat",
        "release_matches",
        "conformance_pass",
        "capabilities_match_admission",
        "hive_requirements",
        "executor_role",
        "available_capacity",
        "dispatch_enabled",
        "current_hive_lease_holder",
    }
    # This lifecycle uses ordinary reviewed admission and never declares an
    # emergency, so no emergency review is required and the predicate holds.
    assert predicates["reviewed_admission_or_emergency"] is True
    return predicates


def _transition(frame, policy, *, false, holder):
    """Record all named intake facts beside the physical protected lease row."""
    predicates = _observed_predicates(frame, policy)
    assert {name for name, value in predicates.items() if not value} == set(false)
    physical = frame["plane"].read_hive_lease_record("bh")[1]
    assert (None if physical is None else (physical.host_id, physical.epoch)) == holder
    return predicates


def _selected_intake(tmp_path, monkeypatch, frame, owner):
    """Run the production local intake adapter through a public selected HOST.

    Only the plane factory is injected so its real SQL adapter shares the test
    clock and fixture credential broker; config resolution and carrier reads are
    the normal public paths. No HQ checkout or Git credential is created.
    """
    name = frame["manifest"].host_id
    home = tmp_path / f"home-{name}"
    home.mkdir(exist_ok=True)
    hq = tmp_path / f"absent-hq-{name}"
    workspace = tmp_path / f"workspace-{name}"
    hive = workspace / "github" / "fixture" / "hive-bh"
    hive.mkdir(parents=True, exist_ok=True)
    if not (hive / ".git").exists():
        _git(hive, "init", "-q")
    with monkeypatch.context() as local:
        local.setenv("BH_HOME", str(home))
        local.setenv("BH_HQ", str(hq))
        local.setenv("GIT_WORKSPACE", str(workspace))
        local.setattr(hq_sql_config, "FnoxBroker", _Broker)
        local.setattr(hq_sql_runtime, "FnoxBroker", _Broker)
        sql = {k: v for k, v in frame["plane"].settings.items() if k != "publisher"}
        if not config.config_path().exists():
            config.save(
                {
                    "hq": {"beadyard_id": owner, "sql": sql},
                }
            )
            host.path().write_text(
                json.dumps(
                    {
                        "host_id": name,
                        "label": name,
                        "signing_key": str(frame["key"]),
                    }
                )
            )
        local.setattr(hq_control_plane, "control_plane", lambda _hq=None: frame["plane"])
        cfg = config.load()
        assert cfg["hq"]["mode"] == "dolt-server"
        decision = frame_eligibility.local_intake_decision("bh", cfg=cfg, hive_dir=hive)
        assert not hq.exists()
        return decision


def _selected_outage_denies_work(tmp_path, monkeypatch, frame, owner):
    """A selected HOST cannot use cached config or Git when its SQL reader is down."""
    from beadhive import dispatch_hive_run

    home = tmp_path / "outage-host-home"
    home.mkdir()
    workspace = tmp_path / "outage-host-workspace"
    hive = workspace / "github" / "fixture" / "hive-bh"
    hive.mkdir(parents=True)
    absent_hq = tmp_path / "outage-host-absent-hq"
    with monkeypatch.context() as local:
        local.setenv("BH_HOME", str(home))
        local.setenv("BH_HQ", str(absent_hq))
        local.setenv("GIT_WORKSPACE", str(workspace))
        local.setattr(hq_sql_config, "FnoxBroker", _Broker)
        sql = {k: v for k, v in frame["plane"].settings.items() if k != "publisher"}
        config.save({"hq": {"beadyard_id": owner, "sql": sql}})
        # Save the qualified selector first, then model the endpoint becoming
        # unavailable. A public save itself correctly refuses an unreachable reader.
        path = config.config_path()
        selected = yaml.safe_load(path.read_text())
        selected["hq"]["sql"]["reader"]["port"] = 1
        path.write_text(yaml.safe_dump(selected))
        host.path().write_text(json.dumps({"host_id": frame["manifest"].host_id}))
        assert config.fleet_sql_selected()

        class NeverRenew:
            def renew(self, *, active):
                pytest.fail("underlying lease renewed after SQL config outage")

        class NeverClaim:
            def claim_issue(self, *args, **kwargs):
                pytest.fail("claim wrote after SQL config outage")

        assert dispatch_hive_run.process_eligible("bh", hive_dir=hive) is False
        keeper = localloop.EligibilityLeaseKeeper(NeverRenew(), "bh", {}, hive, fresh_config=True)
        status = keeper.renew(active=True)
        assert not status.held and status.detail == "configuration unavailable"
        guarded = frame_eligibility.GuardedClaimSession(NeverClaim(), hive)
        with pytest.raises(hq_sql_config.SqlConfigError, match="connection unavailable"):
            guarded.claim_issue("fixture")
        assert not absent_hq.exists()


def _bd(hive: Path, *args: str, environment: dict[str, str]):
    result = subprocess.run(
        ["bd", "-C", str(hive), *args],
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _real_fresh_beads_hydration(tmp_path, monkeypatch, owner, config_settings):
    """Attach an actual cloned `bh` hive store under a fresh SQL-selected HOST."""
    isolated_home = tmp_path / "isolated-bd-home"
    isolated_home.mkdir()
    isolated_shared = tmp_path / "unused-isolated-shared-server"
    embedded = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BEADS_", "BD_", "DOLT_"))
    }
    embedded.update(
        HOME=str(isolated_home),
        XDG_CONFIG_HOME=str(isolated_home / "config"),
        XDG_DATA_HOME=str(isolated_home / "data"),
        XDG_CACHE_HOME=str(isolated_home / "cache"),
        BEADS_SHARED_SERVER_DIR=str(isolated_shared),
    )
    source = tmp_path / "original-hive"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    remote = tmp_path / "hive-bh.git"
    _git(source, "init", "--bare", "-q", "-b", "main", str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    initialized = subprocess.run(
        ["bd", "init", "--prefix", "bh", "--database", "bh", "--non-interactive"],
        cwd=source,
        env=embedded,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    source_metadata = json.loads((source / ".beads" / "metadata.json").read_text())
    assert source_metadata["dolt_mode"] == "embedded"
    assert source_metadata["dolt_database"] == "bh"
    assert store_locator.database_dir(source).resolve().is_relative_to(source.resolve())
    assert (store_locator.database_dir(source) / ".dolt").is_dir()
    assert not isolated_shared.exists()
    issue_id = _bd(source, "--actor", "existing-actor", "q", "existing issue", environment=embedded)
    assert issue_id.startswith("bh-")
    (source / "README.md").write_text("# Isolated hive fixture\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "fixture hive source")
    _git(source, "push", "-q", "origin", "main")
    _bd(source, "dolt", "remote", "add", "origin", f"git+file://{remote}", environment=embedded)
    _bd(source, "dolt", "push", environment=embedded)
    assert _git(source, "ls-remote", "origin", "refs/dolt/data")
    source_issue = json.loads(_bd(source, "show", issue_id, "--json", environment=embedded))[0]

    fresh_home = tmp_path / "fresh-host-home"
    fresh_home.mkdir()
    fresh_hq = tmp_path / "fresh-host-no-hq"
    workspace = tmp_path / "fresh-host-workspace"
    target = workspace / "github" / "fixture" / "hive-bh"
    target.parent.mkdir(parents=True)
    cloned = subprocess.run(
        ["git", "clone", "-q", str(remote), str(target)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert cloned.returncode == 0, cloned.stderr
    cloned_metadata = json.loads((target / ".beads" / "metadata.json").read_text())
    assert cloned_metadata["dolt_database"] == "bh"
    assert not store_locator.database_dir(target).exists()
    with monkeypatch.context() as local:
        for key in list(os.environ):
            if key.startswith(("BEADS_", "BD_", "DOLT_")):
                local.delenv(key, raising=False)
        for key in (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "BEADS_SHARED_SERVER_DIR",
        ):
            local.setenv(key, embedded[key])
        local.setenv("BH_HOME", str(fresh_home))
        local.setenv("BH_HQ", str(fresh_hq))
        local.setenv("GIT_WORKSPACE", str(workspace))
        local.setattr(hq_sql_config, "FnoxBroker", _Broker)
        selected_sql = {
            key: value
            for key, value in config_settings.items()
            if key not in {"publisher", "authority_writer", "observer", "runtime"}
        }
        config.save({"hq": {"beadyard_id": owner, "sql": {**selected_sql, "enabled": True}}})
        cfg = config.load()
        assert cfg["hq"]["mode"] == "dolt-server"
        assert config.fleet_snapshot().beadyard_id == owner
        assert not fresh_hq.exists()
        assert host_provision._sql_store_state(target, cfg) == host_provision.STORE_UNBOOTSTRAPPED
        entry = registry.resolve_hive(cfg, "bh")
        assert registry.hive_dir(entry) == target
        assert host_provision._bootstrap_hive(cfg, entry) == ""
        assert host_provision._sql_store_state(target, cfg) == host_provision.STORE_READY
        metadata = json.loads((target / ".beads" / "metadata.json").read_text())
        assert metadata["dolt_mode"] == "embedded"
        assert metadata["dolt_database"] == "bh"
        assert store_locator.database_dir(target).name == "bh"
        assert store_locator.database_dir(target).resolve().is_relative_to(target.resolve())
        assert not isolated_shared.exists()
        issue = json.loads(_bd(target, "show", issue_id, "--json", environment=embedded))
        assert issue[0]["id"] == issue_id and issue[0]["title"] == "existing issue"
        assert issue[0]["revision"] == source_issue["revision"]
        assert not fresh_hq.exists()
    return issue_id


def _frame(tmp_path, sql_dir, port, config_settings, runtime_head, owner, manifest, epoch, clock):
    key = tmp_path / f"{manifest.host_id}.key"
    public = _key(key)
    authority = ObservationAuthority(
        manifest.frame_id,
        manifest.host_id,
        manifest.instance_ref,
        fingerprint(public),
        epoch,
        "fixture-fleet",
        "desired-1",
        clock() + 1800,
        owner,
    )
    principal = SqlRuntimeOperator.principal_for(authority)
    inbox = _frame_role(sql_dir, port, principal, epoch)
    settings = {
        **config_settings,
        "enabled": True,
        "runtime": _runtime_binding(sql_dir, port, principal),
        "runtime_floor_path": str(tmp_path / f"floor-{manifest.host_id}.json"),
        "runtime_backend_identity": "fixture-runtime-backend",
        "runtime_generation": "fixture-runtime-generation",
        "runtime_initial_revision": runtime_head,
    }
    return {
        "key": key,
        "public": public,
        "authority": authority,
        "principal": principal,
        "inbox": inbox,
        "manifest": manifest,
        "plane": SqlControlPlane(settings, broker=_Broker(), clock=clock),
    }


@skip_if_no_bd
def test_public_seed_then_signed_two_frame_lifecycle_with_one_clock(tmp_path, monkeypatch):
    clock = Clock()
    git_hq, owner, manifests = _source_git(tmp_path)
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    with _empty_config_server(sql_dir) as (port, schema_parent, config_settings):
        config_store = SqlFleetConfigRevisionStore(config_settings, broker=_Broker(), clock=clock)

        def fresh_plan():
            return plan(
                hq_dir=git_hq,
                fleet_path=git_hq / "fleet.yaml",
                workspace_sources=(),
                destination=config_store,
            )

        seed_plan = fresh_plan()
        assert not seed_plan.issues and seed_plan.semantic_parity
        assert seed_plan.beadyard_id == owner
        assert seed_plan.expected_schema_parent == schema_parent
        intent = tmp_path / "seed-intent.json"
        prepare(seed_plan, intent)
        seed_receipt = apply(seed_plan, intent, config_store, fresh_plan=fresh_plan)
        assert seed_receipt is not None and seed_receipt.beadyard_id == owner
        config_settings["initial_revision"] = seed_receipt.committed_revision
        snapshot = config_store.load_snapshot()
        assert snapshot.commit_revision == seed_receipt.committed_revision
        assert snapshot.beadyard_id == owner
        assert {row.path for row in snapshot.documents} >= {
            "beadyard.json",
            "fleet.yaml",
            "hosts/host-a.yaml",
            "hosts/host-b.yaml",
            "hosts/host-a2.yaml",
        }
        issue_id = _real_fresh_beads_hydration(tmp_path, monkeypatch, owner, config_settings)
        assert issue_id.startswith("bh-")

        _runtime_schema(sql_dir, port)
        _runtime_roles(sql_dir, port)
        operator_key = tmp_path / "operator.key"
        operator_public = _key(operator_key)
        runtime_head = _signed_initial_runtime(
            port, snapshot, owner, operator_key, operator_public, clock
        )
        common_settings = {
            **config_settings,
            "enabled": True,
            "runtime_floor_path": str(tmp_path / "runtime-floor.json"),
            "runtime_backend_identity": "fixture-runtime-backend",
            "runtime_generation": "fixture-runtime-generation",
            "runtime_initial_revision": runtime_head,
            "runtime_operator_public_key": operator_public,
        }
        operator_settings = {
            **common_settings,
            "runtime": None,
            "authority_writer": _runtime_binding(sql_dir, port, "authority_writer"),
        }
        operator = SqlControlPlane(operator_settings, broker=_Broker(), clock=clock)
        observer_settings = {
            **common_settings,
            "runtime": None,
            "observer": _runtime_binding(sql_dir, port, "observer"),
        }
        receiver = SqlTrustedReceiver(observer_settings, broker=_Broker(), clock=clock)
        frames = {
            name: _frame(
                tmp_path,
                sql_dir,
                port,
                common_settings,
                runtime_head,
                owner,
                manifest,
                1 if name != "a2" else 2,
                clock,
            )
            for name, manifest in manifests.items()
        }
        head = runtime_head
        for name in ("a", "b"):
            frame = frames[name]
            desired = {
                "declared": True,
                "release": frame["manifest"].release.model_dump(),
                "caps": frame["manifest"].capabilities.model_dump(),
                "profile": "fixture",
            }
            head = operator.grant(
                frame["authority"],
                frame["public"],
                desired,
                expected=head,
                operator_key=str(operator_key),
            )
            digest = frame["plane"].publish_registration_evidence(
                frame["manifest"], signing_key=str(frame["key"])
            )
            request = _request_id(port, frame["inbox"], digest)
            assert receiver.accept_registration(frame["principal"], request) == digest
            for sequence in (1, 2, 3):
                digest = _signed_beat(frame, owner, clock, sequence, state="pending")
                request = _request_id(port, frame["inbox"], digest)
                assert receiver.accept_heartbeat(frame["principal"], request) == digest
            admitted = operator.lifecycle(
                "admit",
                frame["manifest"].frame_id,
                "apply",
                expected=head,
                expected_host_id=frame["manifest"].host_id,
                expected_release=frame["manifest"].release.digest,
                operator_key=str(operator_key),
                confirm=True,
            )
            assert admitted["state"] == "active"
            head = admitted["revision"]

        policy = project_hive_policies(snapshot, valid_until=clock() + 3600, now=clock())["bh"]
        for name in ("a", "b"):
            frame = frames[name]
            _transition(frame, policy, false={"current_hive_lease_holder"}, holder=None)
        assert parse_document((git_hq / "beadyard.json").read_text()) == owner

        # A is the first protected holder. B is admitted and fresh but cannot
        # claim work while A's signed receipt and unexpired lease still hold.
        a, b = frames["a"], frames["b"]
        a_lease = HostLease(
            host_id="host-a",
            label="host-a",
            epoch=1,
            adopted_at=now_stamp(clock()),
            expires_at=now_stamp(clock() + 1200),
        )
        request_id, *_ = a["plane"].propose_hive_lease(
            "bh", a_lease, expected="", operation="adopt", signing_key=str(a["key"])
        )
        hive_revision = receiver.accept_hive_lease(a["principal"], request_id)
        assert a["plane"].read_hive_lease_record("bh")[1] == a_lease
        _transition(a, policy, false=set(), holder=("host-a", 1))
        assert _selected_intake(tmp_path, monkeypatch, a, owner).allowed
        _selected_outage_denies_work(tmp_path, monkeypatch, a, owner)
        b_predicates = _transition(
            b, policy, false={"current_hive_lease_holder"}, holder=("host-a", 1)
        )
        assert not b_predicates["current_hive_lease_holder"]
        b_intake = _selected_intake(tmp_path, monkeypatch, b, owner)
        assert not b_intake.allowed and "current_hive_lease_holder" in b_intake.reason

        def takeover_attempt():
            lease = HostLease(
                host_id="host-b",
                label="host-b",
                epoch=2,
                adopted_at=now_stamp(clock()),
                expires_at=now_stamp(clock() + 600),
            )
            proposal, *_ = b["plane"].propose_hive_lease(
                "bh", lease, expected=hive_revision, operation="adopt", signing_key=str(b["key"])
            )
            return receiver.accept_hive_lease(b["principal"], proposal), lease

        with pytest.raises(ValueError, match="live incumbent is not evictable"):
            takeover_attempt()
        _transition(a, policy, false=set(), holder=("host-a", 1))

        # At the exclusive heartbeat TTL A becomes ineligible. The separate
        # evict_after_s threshold still keeps the lease with A until it passes.
        clock.advance(299)
        digest = _signed_beat(b, owner, clock, 4)
        assert (
            receiver.accept_heartbeat(b["principal"], _request_id(port, b["inbox"], digest))
            == digest
        )
        _transition(a, policy, false=set(), holder=("host-a", 1))
        clock.advance(1)
        a_predicates = _transition(
            a,
            policy,
            false={"authenticated_fresh_heartbeat", "current_hive_lease_holder"},
            holder=("host-a", 1),
        )
        assert a_predicates["authenticated_fresh_heartbeat"] is False
        assert a_predicates["current_hive_lease_holder"] is False
        a_intake = _selected_intake(tmp_path, monkeypatch, a, owner)
        assert not a_intake.allowed and "authenticated_fresh_heartbeat" in a_intake.reason
        assert a["plane"].read_hive_lease_record("bh")[1] == a_lease
        with pytest.raises(ValueError, match="live incumbent is not evictable"):
            takeover_attempt()
        clock.advance(1)
        with pytest.raises(ValueError, match="live incumbent is not evictable"):
            takeover_attempt()
        _transition(
            a,
            policy,
            false={"authenticated_fresh_heartbeat", "current_hive_lease_holder"},
            holder=("host-a", 1),
        )
        clock.advance(1)
        hive_revision, b_lease = takeover_attempt()
        assert b["plane"].read_hive_lease_record("bh")[1] == b_lease
        _transition(b, policy, false=set(), holder=("host-b", 2))
        assert _selected_intake(tmp_path, monkeypatch, b, owner).allowed
        _transition(
            a,
            policy,
            false={"authenticated_fresh_heartbeat", "current_hive_lease_holder"},
            holder=("host-b", 2),
        )

        # Drain stops new intake without transferring B's already held lease.
        drained = operator.lifecycle(
            "drain",
            "frame-b",
            "apply",
            expected=head,
            expected_host_id="host-b",
            expected_release=manifests["b"].release.digest,
            deadline=clock() + 300,
            operator_key=str(operator_key),
            confirm=True,
        )
        assert drained["state"] == "draining"
        head = drained["revision"]
        assert b["plane"].read_hive_lease_record("bh")[1] == b_lease
        b_predicates = _transition(
            b,
            policy,
            false={"admitted_active", "not_cordoned", "current_hive_lease_holder"},
            holder=("host-b", 2),
        )
        assert b_predicates["not_cordoned"] is False
        b_intake = _selected_intake(tmp_path, monkeypatch, b, owner)
        assert not b_intake.allowed and "not_cordoned" in b_intake.reason

        # A replacement for A is a distinct incarnation: new signer, instance,
        # holder and epoch. The original actor/key/lease history stays recorded.
        a2 = frames["a2"]
        desired_a2 = {
            "declared": True,
            "release": a2["manifest"].release.model_dump(),
            "caps": a2["manifest"].capabilities.model_dump(),
            "profile": "fixture",
        }
        head = operator.grant(
            a2["authority"],
            a2["public"],
            desired_a2,
            expected=head,
            operator_key=str(operator_key),
        )
        registration = a2["plane"].publish_registration_evidence(
            a2["manifest"], signing_key=str(a2["key"])
        )
        assert (
            receiver.accept_registration(
                a2["principal"], _request_id(port, a2["inbox"], registration)
            )
            == registration
        )
        for sequence in (1, 2, 3):
            digest = _signed_beat(a2, owner, clock, sequence, state="pending")
            assert (
                receiver.accept_heartbeat(a2["principal"], _request_id(port, a2["inbox"], digest))
                == digest
            )

        # Hold an authentic old-signer beat in the old inbox. It is not allowed
        # to refresh the old incarnation once the operator supersedes it.
        old_digest = _signed_beat(a, owner, clock, 4)
        old_request = _request_id(port, a["inbox"], old_digest)
        superseded = operator.lifecycle(
            "admit",
            "frame-a",
            "apply",
            expected=head,
            expected_host_id="host-a2",
            expected_release=manifests["a2"].release.digest,
            operator_key=str(operator_key),
            confirm=True,
            supersede=True,
        )
        assert superseded["state"] == "active"
        head = superseded["revision"]
        with pytest.raises(ValueError, match="no current operator-granted incarnation"):
            receiver.accept_heartbeat(a["principal"], old_request)
        assert b["plane"].read_hive_lease_record("bh")[1] == b_lease
        _transition(a2, policy, false={"current_hive_lease_holder"}, holder=("host-b", 2))
        a2_intake = _selected_intake(tmp_path, monkeypatch, a2, owner)
        assert not a2_intake.allowed and "current_hive_lease_holder" in a2_intake.reason

        # The incumbent reports its drain, then sends a signed release differing
        # from the admitted manifest. The receiver records the evidence; the
        # eligibility predicate and operator quarantine stop new work.
        drained_digest = _signed_beat(b, owner, clock, 5, state="drained")
        assert (
            receiver.accept_heartbeat(b["principal"], _request_id(port, b["inbox"], drained_digest))
            == drained_digest
        )
        drift_digest = _signed_beat(
            b,
            owner,
            clock,
            6,
            state="drained",
            release={"id": "fixture", "digest": "sha256:" + "3" * 64},
        )
        assert (
            receiver.accept_heartbeat(b["principal"], _request_id(port, b["inbox"], drift_digest))
            == drift_digest
        )
        drift = _transition(
            b,
            policy,
            false={
                "admitted_active",
                "not_cordoned",
                "release_matches",
                "current_hive_lease_holder",
            },
            holder=("host-b", 2),
        )
        assert drift["release_matches"] is False and drift["not_cordoned"] is False
        quarantined = operator.lifecycle(
            "quarantine",
            "frame-b",
            "apply",
            expected=head,
            expected_host_id="host-b",
            expected_release="sha256:" + "3" * 64,
            operator_key=str(operator_key),
            confirm=True,
        )
        assert quarantined["state"] == "quarantined"
        assert b["plane"].read_hive_lease_record("bh")[1] == b_lease
        _transition(
            b,
            policy,
            false={
                "admitted_active",
                "not_cordoned",
                "release_matches",
                "current_hive_lease_holder",
            },
            holder=("host-b", 2),
        )
        b_intake = _selected_intake(tmp_path, monkeypatch, b, owner)
        assert not b_intake.allowed and "admitted_active" in b_intake.reason

        # A later legitimate config publication must not cause a retry of the
        # original public seed request to reseed, reparent, or hide that edit.
        later_documents = tuple(
            FleetConfigDocument(
                row.path,
                row.content + "\nwork:\n  validate_cmd: echo later-config\n"
                if row.path == "fleet.yaml"
                else row.content,
            )
            for row in snapshot.documents
        )
        latest = config_store.publish_snapshot(
            later_documents, expected_revision=snapshot.commit_revision
        )
        assert latest.commit_revision != seed_receipt.committed_revision
        assert latest.beadyard_id == owner
        recovered = apply(seed_plan, intent, config_store, fresh_plan=fresh_plan)
        assert recovered == replace(seed_receipt, observed_head=latest.commit_revision)
        assert config_store.load_snapshot() == latest


def _public_observations(port: int):
    connection = _root(port, "beadhive_hq_runtime")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT frame_id,sequence,digest,accepted_until FROM hq_live_public_observations "
                "ORDER BY frame_id"
            )
            return cursor.fetchall()
    finally:
        connection.close()


def _inject_inbox(port: int, inbox: str, body: bytes, *, created_at: float):
    """Write a raw inbox row as the server root, bypassing the sender's own checks."""
    import uuid

    connection = _root(port, "beadhive_hq_runtime")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {inbox} (request_id,kind,payload,payload_sha256,created_at) "
                "VALUES (%s,'heartbeat',%s,%s,%s)",
                (str(uuid.uuid4()), body, hashlib.sha256(body).hexdigest(), created_at),
            )
    finally:
        connection.close()


def _liveness_predicates(frame, policy):
    """Named intake facts plus the qualified holder read, without pinning the predicate set
    (that census belongs to the lifecycle test above)."""
    _head, desired, observation = frame["plane"].read_eligibility(frame["manifest"])
    predicates = dict(
        eligible(frame["manifest"], policy, EligibilityFacts(observation, desired)).predicates
    )
    qualified = frame["plane"].read_hive_lease_record(
        "bh", holder_identity=frame["manifest"].host_id
    )[1]
    predicates["current_hive_lease_holder"] = qualified is not None and qualified.held_by(
        frame["manifest"].host_id
    )
    assert "authenticated_fresh_heartbeat" in predicates
    return predicates


def _liveness_transition(frame, policy, *, false, holder):
    predicates = _liveness_predicates(frame, policy)
    assert {name for name, value in predicates.items() if not value} == set(false)
    physical = frame["plane"].read_hive_lease_record("bh")[1]
    assert (None if physical is None else (physical.host_id, physical.epoch)) == holder
    return predicates


def test_signed_liveness_needs_no_receiver_for_eligibility_or_holder_writes(tmp_path):
    """hq.sql.liveness: signed against a pinned Dolt server with NO receiver after admission.

    The receiver admits frame A and accepts its first adopt; from then on it never runs, so
    ``hq_live_public_observations`` stays frozen. The default-mode plane goes stale; the
    signed-mode plane stays fresh on each new signed beat, honours the exclusive 300 s TTL,
    never lets an older valid envelope outrank a newer one, skips wrong-key / wrong-epoch /
    wrong-frame / malformed rows (whatever their client ``created_at``), and keeps a holder
    whose hive lease lapsed three hours ago — same epoch — while default mode drops it.
    """
    from beadhive.hq_sql_signatures import sign_heartbeat

    clock = Clock()
    git_hq, owner, manifests = _source_git(tmp_path)
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    with _empty_config_server(sql_dir) as (port, _schema_parent, config_settings):
        config_store = SqlFleetConfigRevisionStore(config_settings, broker=_Broker(), clock=clock)

        def fresh_plan():
            return plan(
                hq_dir=git_hq,
                fleet_path=git_hq / "fleet.yaml",
                workspace_sources=(),
                destination=config_store,
            )

        seed_plan = fresh_plan()
        intent = tmp_path / "seed-intent.json"
        prepare(seed_plan, intent)
        seed_receipt = apply(seed_plan, intent, config_store, fresh_plan=fresh_plan)
        config_settings["initial_revision"] = seed_receipt.committed_revision
        snapshot = config_store.load_snapshot()

        _runtime_schema(sql_dir, port)
        _runtime_roles(sql_dir, port)
        operator_key = tmp_path / "operator.key"
        operator_public = _key(operator_key)
        runtime_head = _signed_initial_runtime(
            port, snapshot, owner, operator_key, operator_public, clock
        )
        common_settings = {
            **config_settings,
            "enabled": True,
            "runtime_floor_path": str(tmp_path / "runtime-floor.json"),
            "runtime_backend_identity": "fixture-runtime-backend",
            "runtime_generation": "fixture-runtime-generation",
            "runtime_initial_revision": runtime_head,
            "runtime_operator_public_key": operator_public,
        }
        operator = SqlControlPlane(
            {
                **common_settings,
                "runtime": None,
                "authority_writer": _runtime_binding(sql_dir, port, "authority_writer"),
            },
            broker=_Broker(),
            clock=clock,
        )
        receiver = SqlTrustedReceiver(
            {
                **common_settings,
                "runtime": None,
                "observer": _runtime_binding(sql_dir, port, "observer"),
            },
            broker=_Broker(),
            clock=clock,
        )
        a = _frame(
            tmp_path, sql_dir, port, common_settings, runtime_head, owner, manifests["a"], 1, clock
        )
        head = operator.grant(
            a["authority"],
            a["public"],
            {
                "declared": True,
                "release": a["manifest"].release.model_dump(),
                "caps": a["manifest"].capabilities.model_dump(),
                "profile": "fixture",
            },
            expected=runtime_head,
            operator_key=str(operator_key),
        )
        digest = a["plane"].publish_registration_evidence(a["manifest"], signing_key=str(a["key"]))
        assert receiver.accept_registration(a["principal"], _request_id(port, a["inbox"], digest))
        for sequence in (1, 2, 3):
            digest = _signed_beat(a, owner, clock, sequence, state="pending")
            assert receiver.accept_heartbeat(a["principal"], _request_id(port, a["inbox"], digest))
        admitted = operator.lifecycle(
            "admit",
            "frame-a",
            "apply",
            expected=head,
            expected_host_id="host-a",
            expected_release=a["manifest"].release.digest,
            operator_key=str(operator_key),
            confirm=True,
        )
        assert admitted["state"] == "active"
        a_lease = HostLease(
            host_id="host-a",
            label="host-a",
            epoch=1,
            adopted_at=now_stamp(clock()),
            expires_at=now_stamp(clock() + 1200),
        )
        proposal, *_ = a["plane"].propose_hive_lease(
            "bh", a_lease, expected="", operation="adopt", signing_key=str(a["key"])
        )
        receiver.accept_hive_lease(a["principal"], proposal)
        # ---- the receiver never runs again below this line ----
        frozen = _public_observations(port)
        assert [row[:2] for row in frozen] == [("frame-a", 3)]

        # Signed hive-lease mode resolves the holder at the hive's refs/bh/epoch (bh-qv8ig);
        # A's receiver-accepted adopt was fenced at epoch 1.
        fence = [EpochFence(epoch=1, host_id="host-a")]
        signed = {
            **a,
            "plane": SqlControlPlane(
                {**a["plane"].settings, "liveness": "signed"},
                broker=_Broker(),
                clock=clock,
                fence_reader=lambda _prefix: fence[0],
            ),
        }
        policy = project_hive_policies(snapshot, valid_until=clock() + 3600, now=clock())["bh"]

        # Both modes go stale once nothing new is signed.
        clock.advance(600)
        assert not _liveness_predicates(a, policy)["authenticated_fresh_heartbeat"]
        assert not _liveness_predicates(signed, policy)["authenticated_fresh_heartbeat"]

        # A new signed beat with no receiver: only signed mode sees it.
        _signed_beat(signed, owner, clock, 4)
        _liveness_transition(signed, policy, false=set(), holder=("host-a", 1))
        _liveness_transition(
            a,
            policy,
            false={"authenticated_fresh_heartbeat", "current_hive_lease_holder"},
            holder=("host-a", 1),
        )
        observation = signed["plane"].read_eligibility(signed["manifest"])[2]
        assert observation.lease.seq == 4 and observation.age_seconds == 0
        assert observation.age_basis == "signed-envelope-reader-clock"

        # Exclusive TTL on the signed renewTime: < 300 s passes, >= 300 s fails.
        clock.advance(299)
        assert _liveness_predicates(signed, policy)["authenticated_fresh_heartbeat"]
        clock.advance(1)
        assert not _liveness_predicates(signed, policy)["authenticated_fresh_heartbeat"]

        _signed_beat(signed, owner, clock, 5)
        assert _liveness_predicates(signed, policy)["authenticated_fresh_heartbeat"]
        # An authentic but OLDER envelope (higher seq, earlier renewTime) never outranks it.
        _signed_beat(signed, owner, lambda: clock() - 200, 6)
        assert signed["plane"].read_eligibility(signed["manifest"])[2].lease.seq == 5

        # Newer-dated rows that must not count, with absurd client created_at values.
        other_key = tmp_path / "intruder.key"
        _key(other_key)
        base = signed["beat"]
        newer = datetime.fromtimestamp(clock() + 5, UTC).isoformat()

        def resigned(key, **update):
            beat = HeartbeatLease.model_validate(
                {**base.model_dump(mode="json"), "renewTime": newer, "seq": 50, **update}
            )
            return canonical(sign_heartbeat(beat, signing_key=str(key)))

        intruder_public = Path(str(other_key) + ".pub").read_text().strip()
        rejected = [
            resigned(other_key, key_id=fingerprint(intruder_public)),
            resigned(a["key"], epoch=2),
            resigned(a["key"], frame_id="frame-b"),
            b"{not json",
            resigned(a["key"]).replace(b"{", b"{ ", 1),
        ]
        for body in rejected:
            _inject_inbox(port, a["inbox"], body, created_at=clock() + 10**9)
        observation = signed["plane"].read_eligibility(signed["manifest"])[2]
        assert observation.lease.seq == 5 and observation.fresh
        assert _public_observations(port) == frozen

        # The holder's lease lapsed three hours ago (same record, same epoch).
        connection = _root(port, "beadhive_hq_runtime")
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT lease_json FROM hq_live_hive_leases WHERE prefix='bh'")
                envelope = json.loads(cursor.fetchone()[0])
                envelope["lease"]["expires_at"] = now_stamp(clock() - 3 * 3600)
                cursor.execute(
                    "UPDATE hq_live_hive_leases SET lease_json=%s WHERE prefix='bh'",
                    (canonical(envelope),),
                )
        finally:
            connection.close()
        _signed_beat(signed, owner, clock, 7)
        _liveness_transition(signed, policy, false=set(), holder=("host-a", 1))
        held = signed["plane"].read_hive_lease_record("bh", holder_identity="host-a")[1]
        assert held.advisory_expiry and held.held_by("host-a", clock()) and held.epoch == 1
        assert a["plane"].read_hive_lease_record("bh", holder_identity="host-a")[1] is None
        assert _public_observations(port) == frozen

        # bh-qv8ig: a NEW tenure with no receiver at all. Once the fence moves to epoch 2 the
        # receiver's epoch-1 row is superseded (fail closed: the designed adopt half-state)...
        fence[0] = EpochFence(epoch=2, host_id="host-a")
        assert signed["plane"].read_hive_lease_record("bh", holder_identity="host-a")[1] is None
        # ...and the frame's own committed signed adopt, verified at read time against the
        # real inbox, signed authority history and accepted registration, IS the lease.
        tenure = HostLease(
            host_id="host-a",
            label="host-a",
            epoch=2,
            adopted_at=now_stamp(clock()),
            expires_at=now_stamp(clock() + 1200),
        )
        proposal, *_ = signed["plane"].propose_hive_lease(
            "bh", tenure, expected="", operation="adopt", signing_key=str(a["key"])
        )
        revision, held = signed["plane"].read_hive_lease_record("bh", holder_identity="host-a")
        assert (held.host_id, held.epoch, held.adopted_at) == ("host-a", 2, tenure.adopted_at)
        assert held.held_by("host-a", clock()) and revision
        # No acknowledgment was written by anyone: nothing processed the proposal.
        connection = _root(port, "beadhive_hq_runtime")
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM hq_live_results WHERE request_id=%s", (proposal,)
                )
                assert cursor.fetchone()[0] == 0
        finally:
            connection.close()
        # The epoch-1 row's reader (receiver acceptance) never sees epoch 2.
        assert a["plane"].read_hive_lease_record("bh")[1].epoch == 1
