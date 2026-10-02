"""Consumer transaction invariants at the committed revision port boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import validate

from beadhive import (
    bd_cli,
    config,
    config_store,
    frame_eligibility,
    gitworkspace,
    host,
    host_cli,
    host_lease,
    hq,
    hq_control_plane,
    identity,
    registry,
)
from beadhive import (
    fleet_roster as hosts,
)
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot


class RevisionStore:
    def __init__(self, documents):
        self.lock = threading.Lock()
        self.revision = 1
        self.documents = tuple(documents)

    def load_snapshot(self):
        with self.lock:
            return FleetConfigSnapshot(
                "sql:fixture",
                str(self.revision),
                "fixture-generation",
                time.time(),
                time.time() + 60,
                self.documents,
            )

    def publish_snapshot(self, documents, *, expected_revision):
        with self.lock:
            if expected_revision != str(self.revision):
                raise ValueError("expected configuration revision changed")
            self.documents = documents
            self.revision += 1
        return self.load_snapshot()


@pytest.mark.parametrize(
    "hq", [None, False, {"mode": "git", "sql": None}, {"mode": "git", "sql": False}]
)
def test_malformed_host_sql_selector_fails_closed(hq):
    api = SimpleNamespace(load_host=lambda: {"hq": hq}, ConfigError=config.ConfigError)
    with pytest.raises(config.ConfigError, match="invalid SQL HOST bootstrap"):
        config_store.sql_selected(api)


@pytest.fixture
def selected_sql_host(tmp_path, monkeypatch):
    """Use the real HOST selector and a deterministic committed-store port."""
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    bootstrap = {
        "hq": {
            "mode": "dolt-server",
            "sql": {
                "enabled": True,
                "reader": {
                    "host": "sql.example.test",
                    "database": "beadhive_hq_config",
                    "user": "reader",
                    "server_name": "sql.example.test",
                    "ca_file": str(tmp_path / "ca.pem"),
                    "credential": {
                        "config_path": str(tmp_path / "fnox.toml"),
                        "profile": "test",
                        "key": "SQL_READER",
                    },
                },
                "floor_path": str(tmp_path / "floor.json"),
                "backend_identity": "fixture",
                "generation": "fixture-generation",
                "initial_revision": "a" * 32,
            },
        }
    }
    host_path = tmp_path / "config.yaml"
    host_path.write_text(json.dumps(bootstrap))
    store = RevisionStore((FleetConfigDocument("fleet.yaml", "hq:\n  mode: dolt-server\n"),))
    monkeypatch.setattr(
        config_store,
        "_sql_attachment",
        lambda api, host=None: (store, store.load_snapshot()),
    )
    config_store.clear_load_cache()
    return store, host_path, bootstrap


def test_active_sql_edit_rejects_switch_to_git_before_any_write(selected_sql_host):
    store, host_path, bootstrap = selected_sql_host
    with config._write_transaction(config.SCOPE_FLEET):
        fleet = config.load_fleet()
        bootstrap["hq"]["mode"] = "git"
        bootstrap["hq"]["sql"]["enabled"] = False
        host_path.write_text(json.dumps(bootstrap))
        with pytest.raises(config.ConfigError, match="changed during fleet transaction"):
            config.save_fleet(fleet)
        with pytest.raises(config.ConfigError, match="changed during fleet transaction"):
            config.load_fleet()
    assert store.revision == 1
    assert not config.fleet_path().exists()


@pytest.mark.parametrize("changed", ["reader", "generation"])
def test_active_sql_edit_rejects_binding_change_before_publication(selected_sql_host, changed):
    store, host_path, bootstrap = selected_sql_host
    with config._write_transaction(config.SCOPE_FLEET):
        fleet = config.load_fleet()
        if changed == "reader":
            bootstrap["hq"]["sql"]["reader"]["host"] = "other.example.test"
            bootstrap["hq"]["sql"]["reader"]["server_name"] = "other.example.test"
        else:
            bootstrap["hq"]["sql"]["generation"] = "other-generation"
        host_path.write_text(json.dumps(bootstrap))
        with pytest.raises(config.ConfigError, match="changed during fleet transaction"):
            config.save_fleet(fleet)
        with pytest.raises(config.ConfigError, match="changed during fleet transaction"):
            config.fleet_snapshot()
    assert store.revision == 1


def test_active_git_edit_rejects_switch_to_sql_before_read(selected_sql_host):
    store, host_path, bootstrap = selected_sql_host
    bootstrap["hq"]["mode"] = "git"
    bootstrap["hq"]["sql"]["enabled"] = False
    host_path.write_text(json.dumps(bootstrap))
    with config._write_transaction(config.SCOPE_FLEET):
        bootstrap["hq"]["mode"] = "dolt-server"
        bootstrap["hq"]["sql"]["enabled"] = True
        host_path.write_text(json.dumps(bootstrap))
        with pytest.raises(config.ConfigError, match="changed during fleet transaction"):
            config.load_fleet()
        with pytest.raises(config.ConfigError, match="changed during fleet transaction"):
            config.save_fleet({})
    assert store.revision == 1
    assert not config.fleet_path().exists()


def test_binding_drift_blocks_roster_registry_workspace_and_document_publish(
    selected_sql_host, tmp_path, monkeypatch
):
    from beadhive import guard

    store, host_path, bootstrap = selected_sql_host
    monkeypatch.setattr(gitworkspace, "workspace_root", lambda: tmp_path / "workspace")
    monkeypatch.setattr(guard, "guard_hq_registry_write", lambda *a, **kw: None)
    monkeypatch.setattr(identity, "resolve_actor", lambda: "dev/fixture")
    with config._write_transaction(config.SCOPE_FLEET):
        bootstrap["hq"]["sql"]["generation"] = "other-generation"
        host_path.write_text(json.dumps(bootstrap))
        for action in (
            lambda: hosts.manifest_paths(tmp_path / "absent-hq"),
            lambda: registry.register("github", "fixture", "repo", "fx", "prototype"),
            lambda: gitworkspace.workspace_sources({}),
            lambda: config.publish_fleet_document("workspace.toml", "[workspace]\n"),
        ):
            with pytest.raises(config.ConfigError, match="changed during fleet transaction"):
                action()
    assert store.revision == 1
    assert not config.fleet_path().exists()


@pytest.fixture
def selected_sql(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("work:\n  identity:\n    name: source-host\n")
    store = RevisionStore(
        (
            FleetConfigDocument(
                "fleet.yaml",
                "hq:\n  mode: dolt-server\nwork:\n  validate_cmd: focused\nmanaged_repos: []\n",
            ),
            FleetConfigDocument(
                "workspace.toml",
                "[[provider]]\nprovider = 'github'\nname = 'first'\ncustom = 'kept'\n",
            ),
            FleetConfigDocument(
                "workspace-extra.toml",
                "[[provider]]\nprovider = 'gitlab'\nname = 'second'\npath = 'outside'\n",
            ),
        )
    )
    monkeypatch.setattr(config_store, "sql_selected", lambda api, host=None: True)
    monkeypatch.setattr(
        config_store, "_sql_attachment", lambda api, host=None: (store, store.load_snapshot())
    )
    config_store.clear_load_cache()
    return store


def test_selected_sql_reads_committed_config_and_preserves_other_documents(selected_sql):
    store = selected_sql
    cfg = config.load()
    assert cfg["work"]["identity"]["name"] == "source-host"
    assert [group.account for group in gitworkspace.groups(cfg)] == ["first", "second"]
    with config._write_transaction(config.SCOPE_FLEET):
        fleet = config.load_fleet()
        fleet["delimiter"] = "/"
        config.save_fleet(fleet)
    assert store.revision == 2
    assert store.documents[1].content.endswith("custom = 'kept'\n")
    assert store.documents[2].path == "workspace-extra.toml"
    assert config.load()["delimiter"] == "/"


def test_original_revision_conflict_is_not_rebased(selected_sql):
    store = selected_sql
    ready = threading.Barrier(2)
    released = threading.Event()
    errors = []

    def stale_writer():
        try:
            with config._write_transaction(config.SCOPE_FLEET):
                fleet = config.load_fleet()
                ready.wait(timeout=5)
                released.wait(timeout=5)
                fleet["delimiter"] = "stale"
                config.save_fleet(fleet)
        except ValueError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=stale_writer)
    thread.start()
    ready.wait(timeout=5)
    with config._write_transaction(config.SCOPE_FLEET):
        fleet = config.load_fleet()
        fleet["delimiter"] = "winner"
        config.save_fleet(fleet)
    released.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == ["expected configuration revision changed"]
    assert store.revision == 2
    assert config.load()["delimiter"] == "winner"


def test_sql_outage_cannot_fall_back_to_local_fleet(selected_sql, tmp_path, monkeypatch):
    (tmp_path / "hq").mkdir()
    (tmp_path / "hq" / "fleet.yaml").write_text("delimiter: stale\n")

    def down(api, host=None):
        raise ValueError("SQL unavailable")

    monkeypatch.setattr(config_store, "_sql_attachment", down)
    with pytest.raises(ValueError, match="SQL unavailable"):
        config.load()
    with pytest.raises(ValueError, match="SQL unavailable"):
        with config._write_transaction(config.SCOPE_FLEET):
            pass
    assert (tmp_path / "hq" / "fleet.yaml").read_text() == "delimiter: stale\n"


def test_workspace_projection_serializes_revision_switch_and_external_override(
    selected_sql, tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    monkeypatch.setattr(gitworkspace, "workspace_root", lambda: root)
    root.mkdir()
    (root / "workspace-lock.toml").write_text("lockfile = 'untouched'\n")
    entered = threading.Event()
    release = threading.Event()

    def first_invocation():
        with gitworkspace.workspace_projection_lock(root):
            paths = gitworkspace.materialize_workspace_sources(config.load(), root)
            assert len(paths) == 2
            entered.set()
            release.wait(timeout=5)  # represents the git-workspace child reading both files

    worker = threading.Thread(target=first_invocation)
    worker.start()
    assert entered.wait(timeout=5)
    with pytest.raises(TimeoutError, match="projection lock deadline"):
        with gitworkspace.workspace_projection_lock(root, timeout=0.05):
            pass
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    with config._write_transaction(config.SCOPE_FLEET):
        fleet = config.load_fleet()
        fleet["delimiter"] = "/"
        config.save_fleet(fleet)
    with gitworkspace.workspace_projection_lock(root):
        paths = gitworkspace.materialize_workspace_sources(config.load(), root)
        assert len(paths) == 2
    manifest = json.loads((root / ".bh-workspace-generated.json").read_text())
    assert manifest["revision"] == "2"
    assert sorted(path.name for path in paths) == sorted(manifest["files"])
    assert [group.account for group in gitworkspace.groups(config.load())] == ["first", "second"]

    external = root / "workspace-local.toml"
    external.write_text("[[provider]]\nprovider = 'gitea'\nname = 'local'\n")
    with gitworkspace.workspace_projection_lock(root):
        assert gitworkspace.materialize_workspace_sources(config.load(), root) == [external]
    assert [group.account for group in gitworkspace.groups(config.load())] == ["local"]
    assert not any(root.glob("workspace-bh-*.toml"))
    assert (root / "workspace-lock.toml").read_text() == "lockfile = 'untouched'\n"


def _assert_child_reads_first_projected_source(root, projected):
    if shutil.which("git-workspace") is None:
        pytest.skip("git-workspace binary unavailable")
    assert projected == sorted(projected)
    result = subprocess.run(
        ["git-workspace", "--workspace", str(root), "lock"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert str(projected[0]) in result.stderr
    assert "order-first-invalid" in result.stderr
    assert "order-second-invalid" not in result.stderr
    assert not (root / "workspace-lock.toml").exists()


def test_git_workspace_child_discovers_all_central_documents_in_selected_order(
    selected_sql, tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    monkeypatch.setattr(gitworkspace, "workspace_root", lambda: root)
    selected_sql.documents = (
        selected_sql.documents[0],
        FleetConfigDocument(
            "workspace.toml", '[[provider]]\nprovider = "order-first-invalid"\nname = "one"\n'
        ),
        FleetConfigDocument(
            "workspace-z.toml", '[[provider]]\nprovider = "order-second-invalid"\nname = "two"\n'
        ),
    )
    with gitworkspace.workspace_projection_lock(root):
        projected = gitworkspace.materialize_workspace_sources(config.load(), root)
        _assert_child_reads_first_projected_source(root, projected)


def test_git_workspace_child_preserves_two_off_root_source_order(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    (hq_dir / "workspace-a.toml").write_text(
        '[[provider]]\nprovider = "order-first-invalid"\nname = "one"\n'
    )
    (hq_dir / "workspace-z.toml").write_text(
        '[[provider]]\nprovider = "order-second-invalid"\nname = "two"\n'
    )
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setattr(config, "hq_dir", lambda: hq_dir)
    monkeypatch.setattr(gitworkspace, "workspace_root", lambda: root)
    with gitworkspace.workspace_projection_lock(root):
        projected = gitworkspace.materialize_workspace_sources({}, root)
        _assert_child_reads_first_projected_source(root, projected)
    assert not (hq_dir / "workspace-lock.toml").exists()


def test_roster_documents_publish_without_hq_checkout(selected_sql, tmp_path):
    hq_dir = tmp_path / "absent-hq"
    manifest = hosts.HostManifest(
        host_id="11111111-1111-4111-8111-111111111111",
        label="fixture",
        os="linux",
        arch="x86_64",
        role="viewer",
        identity=hosts.IdentityMechanism(kind="none", value=""),
    )
    path = hosts.save(hq_dir, manifest)
    assert not hq_dir.exists()
    assert path == hosts.manifest_path(hq_dir, manifest.host_id)
    assert hosts.exists(hq_dir, manifest.host_id)
    assert hosts.load(hq_dir, manifest.host_id) == manifest
    assert [item.name for item in hosts.manifest_paths(hq_dir)] == [path.name]
    assert selected_sql.documents[-1].path == f"hosts/{manifest.host_id}.yaml"
    hosts.remove(hq_dir, manifest.host_id)
    assert not hosts.exists(hq_dir, manifest.host_id)
    assert not hq_dir.exists()


def test_missing_selected_manifest_keeps_unbound_legacy_and_denies_enrolled_frame(
    monkeypatch, tmp_path
):
    class ConfigOnlyPlane(hq_control_plane.SqlControlPlane):
        def __init__(self):
            pass

        def load_host_manifest(self, host_id):
            raise hq_control_plane.CommittedManifestAbsent(
                f"committed host manifest unavailable for {host_id}"
            )

        def read_hive_lease_record(self, prefix):
            pytest.fail("config-only host entered runtime authority")

    monkeypatch.setattr(host, "host_id", lambda: "fixture-host")
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda _root: ConfigOnlyPlane())
    monkeypatch.setattr(config, "load_host", lambda: {"hq": {"mode": "dolt-server"}})
    assert host_lease._frame_plane(tmp_path) is None
    assert frame_eligibility.decision_for("fixture-host", hq_dir=tmp_path) is None

    monkeypatch.setattr(
        config,
        "load_host",
        lambda: {"hq": {"mode": "dolt-server"}, "host": {"frame_id": "frame"}},
    )
    with pytest.raises(host_lease.HostLeaseRejected, match="no committed host manifest"):
        host_lease._frame_plane(tmp_path)
    assert not frame_eligibility.decision_for("fixture-host", hq_dir=tmp_path).allowed


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("SQL unavailable"),
        RuntimeError("invalid manifest"),
        FileNotFoundError("reader credential absent"),
    ],
)
def test_selected_roster_failure_never_looks_like_unbound_legacy(monkeypatch, tmp_path, failure):
    class BrokenPlane(hq_control_plane.SqlControlPlane):
        def __init__(self):
            pass

        def load_host_manifest(self, host_id):
            raise failure

    monkeypatch.setattr(config, "load_host", lambda: {"hq": {"mode": "dolt-server"}})
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda _root: BrokenPlane())
    decision = frame_eligibility.decision_for("fixture-host", hq_dir=tmp_path)
    assert decision is not None
    assert dict(decision.predicates) == {"authority_available": False}
    if isinstance(failure, FileNotFoundError):
        monkeypatch.setattr(host, "host_id", lambda: "fixture-host")
        with pytest.raises(FileNotFoundError, match="reader credential absent"):
            host_lease._frame_plane(tmp_path)


def test_roster_removal_rejects_preflight_revision_change(selected_sql, tmp_path):
    store = selected_sql
    hq_dir = tmp_path / "absent-hq"
    manifest = hosts.HostManifest(
        host_id="11111111-1111-4111-8111-111111111111",
        label="fixture",
        os="linux",
        arch="x86_64",
        role="viewer",
        identity=hosts.IdentityMechanism(kind="none", value=""),
    )
    hosts.save(hq_dir, manifest)

    @hosts.original_revision
    def preflight_then_remove():
        assert hosts.load(hq_dir, manifest.host_id) == manifest
        with store.lock:
            store.revision += 1  # another writer committed after preflight
        hosts.remove(hq_dir, manifest.host_id)

    with pytest.raises(ValueError, match="expected configuration revision changed"):
        preflight_then_remove()
    assert hosts.exists(hq_dir, manifest.host_id)


def test_host_rm_commits_selected_roster_without_hq_checkout(selected_sql, tmp_path):
    hq_dir = config.hq_dir()
    manifest = hosts.HostManifest(
        host_id="11111111-1111-4111-8111-111111111111",
        label="fixture",
        os="linux",
        arch="x86_64",
        role="viewer",
        identity=hosts.IdentityMechanism(kind="none", value=""),
    )
    hosts.save(hq_dir, manifest)
    assert not hq_dir.exists()
    host_cli.rm_cmd(manifest.host_id, dry_run=False, confirm=True, force=True)
    assert not hosts.exists(hq_dir, manifest.host_id)
    assert selected_sql.revision == 3
    assert not hq_dir.exists()


def test_hq_status_reports_config_revision_separately_from_absent_beads(selected_sql, monkeypatch):
    schema = json.loads(
        (Path(__file__).parents[1] / "docs/schemas/hq-status-v1.schema.json").read_text()
    )
    payload = hq.status_payload(generated_at=1234)
    validate(payload, schema)
    assert payload["config_authority"]["revision"] == "1"
    assert payload["beads_channel"] == {
        "state": "unavailable",
        "reason_code": "hq_not_initialized",
    }
    assert payload["availability"]["state"] == "unavailable"
    assert payload["freshness"]["state"] == "unknown"

    def down(api, host=None):
        raise ValueError("SQL unavailable")

    monkeypatch.setattr(config_store, "_sql_attachment", down)
    unavailable = hq.status_payload(generated_at=1235)
    validate(unavailable, schema)
    assert unavailable["config_authority"] == {
        "state": "unavailable",
        "reason_code": "config_sql_unavailable",
    }


def test_hq_status_keeps_present_beads_channel_usable(selected_sql, monkeypatch):
    schema = json.loads(
        (Path(__file__).parents[1] / "docs/schemas/hq-status-v1.schema.json").read_text()
    )
    scanned = []
    monkeypatch.setattr(hq, "local_readiness", lambda _path: "ready")
    monkeypatch.setattr(
        bd_cli,
        "status_snapshot",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{"summary":{"total_issues":0}}'),
    )

    def scan(path, *, fetch):
        scanned.append((path, fetch))
        return object()

    monkeypatch.setattr(hq.safety, "scan", scan)
    monkeypatch.setattr(
        hq,
        "_hq_remote_status",
        lambda _result: {
            "configured": True,
            "working_tree_dirty": False,
            "git": {
                "state": "clean",
                "ahead": 0,
                "behind": 0,
                "has_upstream": True,
                "dirty": False,
            },
            "dolt": {"state": "clean", "ahead": 0, "behind": 0, "reason": None},
        },
    )
    payload = hq.status_payload(generated_at=1234)
    validate(payload, schema)
    assert payload["config_authority"]["revision"] == "1"
    assert payload["availability"] == {"state": "available", "reason_code": "hq_initialized"}
    assert payload["beads_channel"] == {
        "state": "available",
        "reason_code": "beads_status_readable",
    }
    assert payload["remote"]["configured"] is True
    assert scanned == [(config.hq_dir(), True)]

    monkeypatch.setattr(
        bd_cli, "status_snapshot", lambda *a, **kw: SimpleNamespace(returncode=1, stdout="")
    )
    broken = hq.status_payload(generated_at=1235)
    validate(broken, schema)
    assert broken["availability"]["state"] == "available"  # Git facts remain observable.
    assert broken["beads_channel"] == {
        "state": "unavailable",
        "reason_code": "beads_status_unavailable",
    }


def test_beads_status_package_route_keeps_root_engine_and_timeout(monkeypatch, tmp_path):
    calls = []

    def run(args, cwd, *, capture, timeout):
        calls.append((args, cwd, capture, timeout))
        return SimpleNamespace(returncode=0, stdout='{"summary":{"total_issues":0}}')

    monkeypatch.setattr(bd_cli.bd, "run", run)
    result = bd_cli.status_snapshot(tmp_path, timeout=10)
    assert result.returncode == 0
    assert calls == [(["status", "--json", "--no-activity"], tmp_path, True, 10)]


@pytest.mark.parametrize(
    "outcome",
    [
        SimpleNamespace(returncode=0, stdout='{"summary":[]}'),
        SimpleNamespace(returncode=0, stdout='{"summary":{"total_issues":true}}'),
        SimpleNamespace(returncode=0, stdout="not-json"),
        subprocess.TimeoutExpired("bd status", 10),
    ],
)
def test_beads_channel_rejects_invalid_or_timed_out_read(monkeypatch, tmp_path, outcome):
    def probe(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(bd_cli, "status_snapshot", probe)
    assert hq._beads_channel_status(tmp_path, local_ready=True) == {
        "state": "unavailable",
        "reason_code": "beads_status_unavailable",
    }


def test_host_switch_requires_qualified_committed_attach(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    binding = {
        "enabled": False,
        "reader": {
            "host": "sql.example.test",
            "database": "beadhive_hq_config",
            "user": "reader",
            "server_name": "sql.example.test",
            "ca_file": str(tmp_path / "ca.pem"),
            "credential": {
                "config_path": str(tmp_path / "fnox.toml"),
                "profile": "test",
                "key": "SQL_READER",
            },
        },
        "floor_path": str(tmp_path / "floor.json"),
        "backend_identity": "fixture",
        "generation": "fixture-generation",
        "initial_revision": "a" * 32,
    }
    host_path = tmp_path / "config.yaml"
    host_path.write_text(json.dumps({"hq": {"sql": binding}}))
    (tmp_path / "hq").mkdir()
    (tmp_path / "hq" / "fleet.yaml").write_text("delimiter: git\n")
    assert config.load()["delimiter"] == "git"

    def outage(api, host=None):
        raise ValueError("SQL unavailable")

    monkeypatch.setattr(config_store, "_sql_attachment", outage)
    with pytest.raises(ValueError, match="SQL unavailable"):
        config.set_value("hq.sql.enabled", "true")
    assert config.load_host()["hq"]["sql"]["enabled"] is False

    store = RevisionStore((FleetConfigDocument("fleet.yaml", "hq:\n  mode: dolt-server\n"),))
    monkeypatch.setattr(
        config_store, "_sql_attachment", lambda api, host=None: (store, store.load_snapshot())
    )
    assert config.set_value("hq.sql.enabled", "true")["ok"]
    assert config.load_host()["hq"]["sql"]["enabled"] is True
    assert config.fleet_snapshot().commit_revision == "1"
