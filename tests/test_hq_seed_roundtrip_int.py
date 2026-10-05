"""Isolated TLS Dolt and signed Git seed, latest export and HOST selector round trip."""

from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from subprocess import run

import pytest

from beadhive import config, gitworkspace, hq_sql_config, registry, store_locator
from beadhive import hq_seed as seed_module
from beadhive.beadyard_identity import parse_document
from beadhive.beadyard_identity_file import create_identity
from beadhive.gitworkspace import WorkspaceSource
from beadhive.hq_seed import (
    apply,
    export_latest_to_signed_git,
    install_git_mirror,
    load_rollback_receipt,
    plan,
    plan_git_mirror,
    prepare,
)
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from beadhive.hq_transition import WriterSuspensionEvidence, project_latest_to_git
from beadhive.modules.config.adapters.workspace_selection import selected_git_sources
from beadhive.modules.config.domain.ports import FleetConfigDocument
from test_hq_authority_backend import _prepared_hqs, backend, git  # noqa: F401
from test_hq_sql_config_int import _Broker, _cli, _empty_config_server


def test_real_public_seed_latest_signed_git_mirror_and_selector(
    backend,  # noqa: F811 - imported pytest fixture
    tmp_path,
    monkeypatch,
):
    b = backend
    hq = b["op_repo"]
    owner = create_identity(hq)
    raw_identity = (hq / "beadyard.json").read_text()
    assert parse_document(raw_identity) == owner
    fleet_initial = (
        "schema_version: 1\nhq:\n  mode: git\nmanaged_repos:\n"
        "  - provider: github\n    org: fixture\n    repo: hive-bh\n"
        "    prefix: bh\n    kind: org-native\n"
    )
    (hq / "fleet.yaml").write_text(fleet_initial)
    git(hq, "add", "beadyard.json", "fleet.yaml")
    git(hq, "commit", "-qm", "fixture Git source")
    git(hq, "remote", "set-url", "origin", b["plane"]._remote(b["plane"]._policy()))
    git(hq, "push", "-q", "origin", "main")
    assert git(b["remote"], "show", "main:beadyard.json") == raw_identity.strip()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_a = '[[provider]]\nprovider = "github"\nname = "old"\npath = "github"\n'
    old_b = '[[provider]]\nprovider = "gitlab"\nname = "second"\npath = "gitlab"\n'
    (workspace / "workspace-a.toml").write_text(old_a)
    (workspace / "workspace.toml").write_text(old_b)
    sources = tuple(
        WorkspaceSource(path.name, path.read_text(), path)
        for path in selected_git_sources({}, root=workspace, hq_dir=hq)
    )
    assert [source.name for source in sources] == ["workspace-a.toml", "workspace.toml"]
    git_store = b["plane"].config_store(operator_key=str(b["operator"]))
    raw_git = (
        FleetConfigDocument("beadyard.json", raw_identity),
        FleetConfigDocument("fleet.yaml", fleet_initial),
        *(FleetConfigDocument(row.name, row.content) for row in sources),
    )
    initial_git = git_store.publish_snapshot(raw_git, expected_revision="")
    assert initial_git.beadyard_id == owner

    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    with _empty_config_server(sql_dir) as (port, schema_parent, settings):
        sql_store = SqlFleetConfigRevisionStore(settings, broker=_Broker())

        def fresh_plan():
            return plan(
                hq_dir=hq,
                fleet_path=hq / "fleet.yaml",
                workspace_sources=sources,
                destination=sql_store,
            )

        seed = fresh_plan()
        assert seed.issues == (), seed.issues
        assert seed.semantic_parity and seed.beadyard_id == owner
        assert seed.expected_schema_parent == schema_parent
        intent = tmp_path / "private" / "seed.json"
        prepare(seed, intent)
        initial_sql = apply(seed, intent, sql_store, fresh_plan=fresh_plan)
        assert initial_sql is not None
        assert initial_sql.beadyard_id == owner
        settings["initial_revision"] = initial_sql.committed_revision
        assert sql_store.load_snapshot().beadyard_id == owner

        # A different host starts from only HOST bootstrap metadata. It has no
        # HQ Git checkout, no local fleet.yaml, and no preexisting hive store.
        # The public config and registry projections must still resolve from
        # the just-seeded committed SQL snapshot.
        fresh_home = tmp_path / "fresh-host" / "home"
        fresh_home.mkdir(parents=True)
        fresh_hq = tmp_path / "fresh-host" / "no-hq-checkout"
        fresh_workspace = tmp_path / "fresh-host" / "workspace"
        fresh_workspace.mkdir()
        monkeypatch.setenv("BH_HOME", str(fresh_home))
        monkeypatch.setenv("BH_HQ", str(fresh_hq))
        monkeypatch.setenv("GIT_WORKSPACE", str(fresh_workspace))
        monkeypatch.setattr(config, "home", lambda: config._config_paths.home(config))
        monkeypatch.setattr(
            config, "hq_dir", lambda: config._config_paths.named_home(config, "hq", "hq")
        )
        monkeypatch.setattr(config, "load_host", lambda: config._config_store.load_host(config))
        monkeypatch.setattr(config, "load", lambda: config._config_store.load(config))
        monkeypatch.setattr(hq_sql_config, "FnoxBroker", lambda: _Broker())
        fresh_host = {
            "hq": {
                "beadyard_id": owner,
                "authority_anchor": str(b["op_anchor"]),
                "sql": {**settings, "enabled": True},
            }
        }
        config.save(fresh_host)
        fresh_cfg = config.load()
        assert config.fleet_snapshot().commit_revision == initial_sql.committed_revision
        assert fresh_cfg["hq"]["mode"] == "dolt-server"
        hive = registry.resolve_hive(fresh_cfg, "bh")
        assert (hive["provider"], hive["org"], hive["repo"]) == ("github", "fixture", "hive-bh")
        fresh_hive = registry.hive_dir(hive)
        assert fresh_hive == fresh_workspace / "github" / "fixture" / "hive-bh"
        # Before bd writes metadata, the prospective shared-server name is the
        # canonical sanitized prefix. There is no claimed initialized store.
        assert store_locator.dolt_mode(fresh_hive) is None
        assert store_locator.sanitize_database_name(hive["prefix"]) == "bh"
        assert not fresh_hq.exists()
        assert not (fresh_home / "fleet.yaml").exists()

        # Restore the public facade around the backend fixture's narrow CLI
        # monkeypatches. All paths and credentials remain under this test's /tmp.
        local_home = tmp_path / "bh-home"
        local_home.mkdir()
        monkeypatch.setenv("BH_HOME", str(local_home))
        monkeypatch.setenv("BH_HQ", str(hq))
        monkeypatch.setenv("GIT_WORKSPACE", str(workspace))
        monkeypatch.setattr(config, "home", lambda: config._config_paths.home(config))
        monkeypatch.setattr(
            config, "hq_dir", lambda: config._config_paths.named_home(config, "hq", "hq")
        )
        monkeypatch.setattr(config, "load_host", lambda: config._config_store.load_host(config))
        monkeypatch.setattr(config, "load", lambda: config._config_store.load(config))
        monkeypatch.setattr(hq_sql_config, "FnoxBroker", lambda: _Broker())
        local_host = {
            "hq": {
                "beadyard_id": owner,
                "authority_anchor": str(b["op_anchor"]),
                "sql": {**settings, "enabled": False},
            }
        }
        config.save(local_host)
        sql_host = deepcopy(local_host)
        sql_host["hq"]["sql"]["enabled"] = True
        config.save_after_verified_hq_seed(
            sql_host, revision=initial_sql.committed_revision, beadyard_id=owner
        )
        assert config.fleet_snapshot().commit_revision == initial_sql.committed_revision
        assert config.load()["hq"]["mode"] == "dolt-server"
        assert [source.name for source in gitworkspace.workspace_sources(config.load())] == [
            "workspace-a.toml",
            "workspace.toml",
        ]

        current = sql_store.load_snapshot()
        latest_workspace = '[[provider]]\nprovider = "github"\nname = "latest"\npath = "github"\n'
        later = tuple(
            FleetConfigDocument(
                item.path,
                item.content + "# later fleet edit\n"
                if item.path == "fleet.yaml"
                else latest_workspace
                if item.path == "workspace-a.toml"
                else item.content,
            )
            for item in current.documents
        )
        latest_sql = sql_store.publish_snapshot(later, expected_revision=current.commit_revision)
        assert latest_sql.commit_revision != initial_sql.committed_revision
        assert latest_sql.beadyard_id == owner
        assert config.fleet_snapshot().commit_revision == latest_sql.commit_revision
        assert gitworkspace.workspace_sources(config.load())[0].content == latest_workspace

        # Actually remove the isolated publisher's table/procedure grants and
        # exercise the three denied operations claimed by the signed artifact.
        _cli(
            sql_dir,
            port,
            "\n".join(
                (
                    "REVOKE INSERT, UPDATE, DELETE ON beadhive_hq_config.hq_config_documents "
                    "FROM 'bh_hq_config_publisher'@'localhost';",
                    "REVOKE INSERT, UPDATE ON beadhive_hq_config.hq_config_meta "
                    "FROM 'bh_hq_config_publisher'@'localhost';",
                    "REVOKE INSERT ON beadhive_hq_config.hq_config_publications "
                    "FROM 'bh_hq_config_publisher'@'localhost';",
                    "REVOKE EXECUTE ON PROCEDURE beadhive_hq_config.dolt_add "
                    "FROM 'bh_hq_config_publisher'@'localhost';",
                    "REVOKE EXECUTE ON PROCEDURE beadhive_hq_config.dolt_commit "
                    "FROM 'bh_hq_config_publisher'@'localhost';",
                )
            ),
        )
        import pymysql

        writer = pymysql.connect(
            host="127.0.0.1",
            port=port,
            user="bh_hq_config_publisher",
            password="fixture-secret",
            database="beadhive_hq_config",
            autocommit=False,
        )
        denied_codes = {}
        try:
            for operation, forbidden in (
                ("config_table_dml", "UPDATE hq_config_meta SET sequence=sequence WHERE id=1"),
                ("dolt_add", "CALL DOLT_ADD('hq_config_meta')"),
                (
                    "dolt_commit",
                    "CALL DOLT_COMMIT('-m','forbidden','--author',"
                    "'Fixture <fixture@example.invalid>')",
                ),
            ):
                with writer.cursor() as cursor, pytest.raises(pymysql.MySQLError) as denied:
                    cursor.execute(forbidden)
                code, message = denied.value.args[:2]
                assert isinstance(code, int) and re.search(
                    r"\bdenied\b|\bpermission\b|\bnot allowed\b", str(message), re.I
                ), (operation, code, "not a permission denial")
                denied_codes[operation] = code
                writer.rollback()
        finally:
            writer.close()
        assert sql_store.load_snapshot().commit_revision == latest_sql.commit_revision

        now = int(time.time())
        artifact = tmp_path / "private" / "suspension.json"
        body = {
            "backend_identity": latest_sql.backend_identity,
            "generation": latest_sql.generation,
            "original_head": latest_sql.commit_revision,
            "principal": "fixture-publisher",
            "observed_at": now,
            "valid_until": now + 180,
            "drained": True,
            "status_clean": True,
            "denied": ["dolt_add", "dolt_commit", "config_table_dml"],
        }
        artifact.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
        artifact.chmod(0o600)
        run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-f",
                str(b["operator"]),
                "-n",
                "beadhive-hq-writer-suspension",
                str(artifact),
            ],
            check=True,
            capture_output=True,
        )
        signature = artifact.with_suffix(".json.sig")
        signature.chmod(0o600)
        suspension = WriterSuspensionEvidence(
            latest_sql.backend_identity,
            latest_sql.generation,
            latest_sql.commit_revision,
            "fixture-publisher",
            hashlib.sha256(artifact.read_bytes()).hexdigest(),
            now,
            now + 180,
            artifact,
            signature,
        )
        proposed_host = deepcopy(sql_host)
        proposed_host["hq"]["sql"]["enabled"] = False
        original_host_bytes = config.config_path().read_bytes()
        export_intent = tmp_path / "private" / "git-export.json"
        real_publish = git_store.publish_snapshot
        publish_calls = 0

        def lost_final_ack(*args, **kwargs):
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls > 1:
                raise AssertionError("same original request republished Git")
            real_publish(*args, **kwargs)
            raise RuntimeError("simulated lost Git publication result")

        monkeypatch.setattr(git_store, "publish_snapshot", lost_final_ack)
        real_record = seed_module.record_rollback_receipt
        record_calls = 0

        def crash_before_receipt(path, witness):
            nonlocal record_calls
            record_calls += 1
            if record_calls == 1:
                raise OSError("simulated crash after signed push before receipt durability")
            return real_record(path, witness)

        monkeypatch.setattr(seed_module, "record_rollback_receipt", crash_before_receipt)
        export_args = dict(
            sql_store=sql_store,
            git_store=git_store,
            expected_sql_revision=latest_sql.commit_revision,
            expected_git_revision=initial_git.commit_revision,
            original_host_bytes=original_host_bytes,
            proposed_host=proposed_host,
            suspension=suspension,
            intent_path=export_intent,
        )
        with pytest.raises(OSError, match="after signed push before receipt durability"):
            export_latest_to_signed_git(**export_args)
        pending = json.loads(export_intent.with_name(export_intent.name + ".pending").read_text())
        signed_after_crash = git_store.load_snapshot()
        assert signed_after_crash.commit_revision != initial_git.commit_revision
        assert not export_intent.exists() and load_rollback_receipt(export_intent) is None
        assert pending["publication_id"] in git(
            b["remote"], "show", "-s", "--format=%B", signed_after_crash.commit_revision
        )
        receipt = export_latest_to_signed_git(**export_args)
        repeated = export_latest_to_signed_git(**export_args)
        assert repeated == receipt == load_rollback_receipt(export_intent)
        assert receipt.export_revision == signed_after_crash.commit_revision
        assert publish_calls == 1
        assert record_calls == 3
        assert receipt.beadyard_id == owner
        assert receipt.source_revision == latest_sql.commit_revision
        signed_git = git_store.load_snapshot()
        assert signed_git.commit_revision == receipt.export_revision
        assert signed_git.documents == project_latest_to_git(latest_sql.documents)
        mirror = plan_git_mirror(
            receipt=receipt,
            git_snapshot=signed_git,
            hq_dir=hq,
            workspace_root=workspace,
            proposed_host=proposed_host,
        )
        install_git_mirror(
            mirror,
            tmp_path / "private" / "mirror.json",
            sql_store=sql_store,
            git_store=git_store,
            receipt=receipt,
            original_host_bytes=original_host_bytes,
            proposed_host=proposed_host,
        )
        config.save_after_verified_hq_export(proposed_host, receipt, mirror)
        assert config.load_host() == proposed_host
        assert config.load_fleet()["hq"]["mode"] == "git"
        assert config.load()["hq"]["beadyard_id"] == owner
        assert [source.content for source in gitworkspace.workspace_sources(config.load())] == [
            latest_workspace,
            old_b,
        ]
        # The same path selector used by the normal Git workspace facade now
        # reads exactly the ordered signed latest export, including external roots.
        selected = selected_git_sources({}, root=workspace, hq_dir=hq)
        assert [(path.name, path.read_text()) for path in selected] == [
            (item.path, item.content)
            for item in signed_git.documents
            if item.path.startswith("workspace") and item.path.endswith(".toml")
        ]
        assert (hq / "fleet.yaml").read_text() == next(
            item.content for item in signed_git.documents if item.path == "fleet.yaml"
        )
        assert (hq / "beadyard.json").read_text() == raw_identity
        assert (
            sql_store.load_snapshot().beadyard_id == git_store.load_snapshot().beadyard_id == owner
        )
        assert git(b["remote"], "rev-parse", "refs/heads/bh-config") == receipt.export_revision
        print(
            "REAL_ROUNDTRIP_OK "
            + json.dumps(
                {
                    "owner": owner,
                    "schema_parent": schema_parent,
                    "seed_revision": initial_sql.committed_revision,
                    "latest_sql_revision": latest_sql.commit_revision,
                    "signed_git_revision": receipt.export_revision,
                    "workspace_files": [path.name for path in selected],
                    "publisher_denials": 3,
                    "publisher_denial_codes": denied_codes,
                    "git_publications": publish_calls,
                    "receipt_attempts": record_calls,
                    "host_selector": "git",
                },
                sort_keys=True,
            )
        )
