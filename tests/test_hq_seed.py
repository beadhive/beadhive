"""Source/intent custody around the public first-publication port."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from beadhive.beadyard_identity import new_document, parse_document
from beadhive.hq_seed import (
    SeedError,
    apply,
    export_latest_to_signed_git,
    install_git_mirror,
    load_rollback_receipt,
    plan,
    plan_git_mirror,
    prepare,
    record_rollback_receipt,
    recover_git_mirror_plan,
)
from beadhive.hq_sql_config import InitialDestination, InitialSeedReceipt, _digest
from beadhive.hq_transition import (
    TransitionError,
    WriterSuspensionEvidence,
    project_latest_to_git,
    receipt_for_export,
    verify_writer_suspension,
)
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot


def _git(root: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class _Destination:
    def __init__(self):
        self.parent = "schema-parent"
        self.published = None
        self.calls = 0

    def inspect_initial_destination(self):
        return InitialDestination(self.parent, self.published is None, "fixture", "generation")

    def initialize_snapshot(self, documents, *, expected_schema_parent, publication_id):
        self.calls += 1
        assert expected_schema_parent == self.parent
        if self.published is not None:
            raise AssertionError("second physical seed attempt")
        self.published = InitialSeedReceipt(
            publication_id,
            self.parent,
            "seed-commit",
            "seed-commit",
            "fixture",
            "generation",
            _digest(documents),
            len(documents),
            parse_document(documents[0].content),
        )
        return self.published

    def recover_initial_snapshot(
        self,
        *,
        publication_id,
        expected_schema_parent,
        documents_sha256,
        document_count,
        beadyard_id,
    ):
        receipt = self.published
        if receipt is None:
            return None
        assert (
            receipt.publication_id,
            receipt.expected_schema_parent,
            receipt.documents_sha256,
            receipt.document_count,
            receipt.beadyard_id,
        ) == (
            publication_id,
            expected_schema_parent,
            documents_sha256,
            document_count,
            beadyard_id,
        )
        return receipt


def _source(tmp_path):
    hq = tmp_path / "hq"
    hq.mkdir()
    _git(hq, "init", "-q")
    _git(hq, "config", "user.name", "Fixture")
    _git(hq, "config", "user.email", "fixture@example.invalid")
    _git(hq, "branch", "-M", "main")
    (hq / "beadyard.json").write_text(new_document())
    (hq / "fleet.yaml").write_text("hq:\n  mode: git\n")
    _git(hq, "add", "beadyard.json", "fleet.yaml")
    _git(hq, "commit", "-qm", "source")
    return hq


def test_seed_plan_preserves_source_bytes_and_durable_original_intent(tmp_path):
    hq = _source(tmp_path)
    target = _Destination()

    def fresh():
        return plan(
            hq_dir=hq, fleet_path=hq / "fleet.yaml", workspace_sources=(), destination=target
        )

    original = fresh()
    assert original.issues == ()
    assert original.source_git_head == _git(hq, "rev-parse", "main")
    assert original.documents[0].content == (hq / "beadyard.json").read_text()
    assert original.documents[1].content == "hq:\n  mode: dolt-server\n"
    assert (
        original.sources[1].sha256 == hashlib.sha256((hq / "fleet.yaml").read_bytes()).hexdigest()
    )
    journal = tmp_path / "custody" / "seed.json"
    first = prepare(original, journal)
    assert journal.stat().st_mode & 0o077 == 0
    assert first["publication_id"] == prepare(original, journal)["publication_id"]
    receipt = apply(original, journal, target, fresh_plan=fresh)
    assert receipt is not None and receipt.publication_id == first["publication_id"]
    assert target.calls == 1
    assert apply(original, journal, target, fresh_plan=fresh) == receipt
    assert target.calls == 1
    assert "fixture-secret" not in journal.read_text()


def test_seed_rechecks_actual_file_set_status_and_content_before_dml(tmp_path):
    hq = _source(tmp_path)
    target = _Destination()

    def fresh():
        return plan(
            hq_dir=hq, fleet_path=hq / "fleet.yaml", workspace_sources=(), destination=target
        )

    original = fresh()
    journal = tmp_path / "custody" / "seed.json"
    prepare(original, journal)
    (hq / "hosts").mkdir()
    (hq / "hosts" / "new.yaml").write_text("host_id: new\n")
    with pytest.raises(SeedError, match="source changed"):
        apply(original, journal, target, fresh_plan=fresh)
    assert target.calls == 0


def test_concurrent_prepare_keeps_one_logical_uuid_and_confirmed_record(tmp_path):
    hq = _source(tmp_path)
    target = _Destination()

    def fresh():
        return plan(
            hq_dir=hq, fleet_path=hq / "fleet.yaml", workspace_sources=(), destination=target
        )

    original = fresh()
    journal = tmp_path / "custody" / "seed.json"
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _: prepare(original, journal), range(2)))
    assert records[0]["publication_id"] == records[1]["publication_id"]
    receipt = apply(original, journal, target, fresh_plan=fresh)
    assert receipt is not None
    with ThreadPoolExecutor(max_workers=2) as pool:
        repeated = list(
            pool.map(lambda _: apply(original, journal, target, fresh_plan=fresh), range(2))
        )
    assert repeated == [receipt, receipt]
    assert target.calls == 1
    assert '"state":"confirmed"' in journal.read_text()


def test_seed_dry_run_names_legacy_identity_and_host_partition_conflicts(tmp_path):
    hq = _source(tmp_path)
    (hq / "beadyard.json").unlink()
    (hq / "fleet.yaml").write_text("hq:\n  mode: git\nwork:\n  identity:\n    name: local\n")
    result = plan(
        hq_dir=hq, fleet_path=hq / "fleet.yaml", workspace_sources=(), destination=_Destination()
    )
    assert any("explicit adoption" in issue for issue in result.issues)
    assert any("work.identity.name" in issue for issue in result.issues)
    with pytest.raises(SeedError, match="unresolved"):
        prepare(result, tmp_path / "custody" / "seed.json")


def test_named_host_relocation_keeps_canonical_effective_settings(tmp_path):
    hq = _source(tmp_path)
    (hq / "fleet.yaml").write_text(
        "# preserved\nhq:\n  mode: git\nwork:\n  identity:\n"
        "    name: Local Operator\n    email: local@example.invalid\n"
    )
    host = {"work": {"identity": {"name": "Local Operator", "email": "local@example.invalid"}}}
    result = plan(
        hq_dir=hq,
        fleet_path=hq / "fleet.yaml",
        workspace_sources=(),
        destination=_Destination(),
        host_snapshot=host,
    )
    assert result.issues == ()
    assert result.semantic_parity
    assert result.host_relocation_paths == ("work.identity.email", "work.identity.name")
    assert result.documents[1].content.startswith("# preserved\n")
    assert "Local Operator" not in result.documents[1].content
    assert "local@example.invalid" not in result.documents[1].content
    assert "mode: dolt-server" in result.documents[1].content


def test_latest_export_receipt_requires_exact_signed_projection_and_bound_suspension(tmp_path):
    identity = new_document()
    sql_documents = (
        FleetConfigDocument("beadyard.json", identity),
        FleetConfigDocument("fleet.yaml", "# retained\nhq:\n  mode: dolt-server\n"),
    )
    git_documents = project_latest_to_git(sql_documents)
    now = time.time()
    sql = FleetConfigSnapshot("sql:fixture", "sql-3", "generation", now, now + 30, sql_documents)
    git = FleetConfigSnapshot("git:fixture", "git-7", "generation", now, now + 30, git_documents)
    suspension = WriterSuspensionEvidence(
        "sql:fixture",
        "generation",
        "sql-3",
        "publisher",
        "a" * 64,
        now,
        now + 30,
        Path("/tmp/artifact.json"),
        Path("/tmp/artifact.sig"),
    )
    receipt = receipt_for_export(
        sql,
        git,
        original_host_bytes=b"current sql host",
        proposed_host={"hq": {"mode": "git"}},
        suspension=suspension,
        export_expected_parent="git-6",
        export_publication_id="6b51e419-601e-40b6-9829-f258b59947da",
        now=now + 1,
    )
    assert receipt.source_revision == "sql-3"
    assert receipt.export_revision == "git-7"
    assert receipt.beadyard_id == parse_document(identity)
    journal = tmp_path / "private" / "rollback.json"
    assert record_rollback_receipt(journal, receipt) == receipt
    assert load_rollback_receipt(journal) == receipt
    assert journal.stat().st_mode & 0o077 == 0
    with pytest.raises(SeedError, match="conflicts"):
        record_rollback_receipt(
            journal,
            receipt.__class__(**{**receipt.__dict__, "source_revision": "sql-other"}),
        )
    with pytest.raises(TransitionError, match="differs"):
        receipt_for_export(
            sql,
            FleetConfigSnapshot("git:fixture", "git-8", "generation", now, now + 30, sql_documents),
            original_host_bytes=b"current sql host",
            proposed_host={"hq": {"mode": "git"}},
            suspension=suspension,
            export_expected_parent="git-6",
            export_publication_id="6b51e419-601e-40b6-9829-f258b59947da",
            now=now + 1,
        )
    with pytest.raises(TransitionError, match="suspension"):
        receipt_for_export(
            sql,
            git,
            original_host_bytes=b"current sql host",
            proposed_host={"hq": {"mode": "git"}},
            suspension=WriterSuspensionEvidence(
                "sql:fixture",
                "generation",
                "sql-2",
                "publisher",
                "a" * 64,
                now,
                now + 30,
                Path("/tmp/artifact.json"),
                Path("/tmp/artifact.sig"),
            ),
            export_expected_parent="git-6",
            export_publication_id="6b51e419-601e-40b6-9829-f258b59947da",
            now=now + 1,
        )


def test_writer_suspension_requires_actual_operator_signature_and_exact_artifact(tmp_path):
    key = tmp_path / "operator"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    allowed = tmp_path / "allowed"
    allowed.write_text("operator " + key.with_suffix(".pub").read_text())
    now = int(time.time())
    artifact = tmp_path / "suspension.json"
    body = {
        "backend_identity": "sql:fixture",
        "generation": "generation",
        "original_head": "head-3",
        "principal": "config-publisher",
        "observed_at": now,
        "valid_until": now + 60,
        "drained": True,
        "status_clean": True,
        "denied": ["dolt_add", "dolt_commit", "config_table_dml"],
    }
    artifact.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
    artifact.chmod(0o600)
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(key),
            "-n",
            "beadhive-hq-writer-suspension",
            str(artifact),
        ],
        check=True,
        capture_output=True,
    )
    signature = artifact.with_suffix(".json.sig")
    signature.chmod(0o600)
    evidence = WriterSuspensionEvidence(
        "sql:fixture",
        "generation",
        "head-3",
        "config-publisher",
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
        now,
        now + 60,
        artifact,
        signature,
    )
    verify_writer_suspension(evidence, operator_signers=str(allowed), ssh_keygen="ssh-keygen")
    artifact.write_bytes(artifact.read_bytes() + b" ")
    with pytest.raises(TransitionError, match="artifact changed"):
        verify_writer_suspension(evidence, operator_signers=str(allowed), ssh_keygen="ssh-keygen")


def test_helper_latest_sql_edit_export_and_interrupted_external_mirror(tmp_path, monkeypatch):
    import beadhive.hq_seed as seed_module

    key = tmp_path / "operator"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    allowed = tmp_path / "allowed"
    allowed.write_text("operator " + key.with_suffix(".pub").read_text())
    now = int(time.time())
    source = FleetConfigSnapshot(
        "sql:fixture",
        "sql-latest",
        "generation",
        now,
        now + 60,
        (
            FleetConfigDocument("beadyard.json", new_document()),
            FleetConfigDocument("fleet.yaml", "hq:\n  mode: dolt-server\n"),
            FleetConfigDocument(
                "workspace-a.toml",
                '[[provider]]\nprovider = "github"\nname = "latest"\npath = "github"\n',
            ),
            FleetConfigDocument(
                "workspace.toml",
                '[[provider]]\nprovider = "gitlab"\nname = "second"\npath = "gitlab"\n',
            ),
        ),
    )
    old = FleetConfigSnapshot(
        "git:fixture",
        "git-old",
        "generation",
        now,
        now + 60,
        (
            source.documents[0],
            FleetConfigDocument("fleet.yaml", "hq:\n  mode: git\n"),
            FleetConfigDocument(
                "workspace-a.toml",
                '[[provider]]\nprovider = "github"\nname = "stale"\npath = "github"\n',
            ),
            source.documents[3],
        ),
    )

    class SqlStore:
        def load_snapshot(self):
            return source

    class GitStore:
        def __init__(self):
            self.snapshot = old
            self.plane = self
            self.hq_dir = tmp_path
            self.publication_id = None

        def _policy(self):
            return {
                "client": {"role": "operator"},
                "operator_signers": str(allowed),
                "executables": {"ssh_keygen": {"path": "ssh-keygen"}},
            }

        def load_snapshot(self, *, revision=None):
            if revision is not None:
                assert revision == self.snapshot.commit_revision
            return self.snapshot

        def publish_snapshot(self, documents, *, expected_revision, publication_id):
            assert expected_revision == self.snapshot.commit_revision == "git-old"
            self.publication_id = publication_id
            self.snapshot = FleetConfigSnapshot(
                "git:fixture", "git-export", "generation", now, now + 60, tuple(documents)
            )
            return self.snapshot

        def git(self, _directory, verb, *args):
            if verb == "rev-list":
                return "git-export git-old"
            assert verb == "show"
            return (
                f"Fleet configuration revision 2\n\nHQ export publication ID: {self.publication_id}"
            )

    artifact = tmp_path / "suspension.json"
    body = {
        "backend_identity": "sql:fixture",
        "generation": "generation",
        "original_head": "sql-latest",
        "principal": "publisher",
        "observed_at": now,
        "valid_until": now + 60,
        "drained": True,
        "status_clean": True,
        "denied": ["dolt_add", "dolt_commit", "config_table_dml"],
    }
    artifact.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
    artifact.chmod(0o600)
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(key),
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
        "sql:fixture",
        "generation",
        "sql-latest",
        "publisher",
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
        now,
        now + 60,
        artifact,
        signature,
    )
    sql_store, git_store = SqlStore(), GitStore()
    proposed_host = {"hq": {"mode": "git"}}
    receipt = export_latest_to_signed_git(
        sql_store=sql_store,
        git_store=git_store,
        expected_sql_revision="sql-latest",
        expected_git_revision="git-old",
        original_host_bytes=b"sql host",
        proposed_host=proposed_host,
        suspension=suspension,
        intent_path=tmp_path / "private" / "export.json",
    )
    assert receipt.source_revision == "sql-latest"
    assert receipt.export_revision == "git-export"
    hq = tmp_path / "hq"
    root = tmp_path / "workspace"
    hq.mkdir()
    root.mkdir()
    (hq / "fleet.yaml").write_text("hq:\n  mode: git\n")
    (hq / "beadyard.json").write_text(source.documents[0].content)
    (root / "workspace-a.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "stale"\npath = "github"\n'
    )
    (root / "workspace.toml").write_text(
        '[[provider]]\nprovider = "gitlab"\nname = "old-second"\npath = "gitlab"\n'
    )
    plan = plan_git_mirror(
        receipt=receipt,
        git_snapshot=git_store.snapshot,
        hq_dir=hq,
        workspace_root=root,
        proposed_host=proposed_host,
    )
    journal = tmp_path / "custody" / "mirror.json"
    real_replace = seed_module.os.replace
    interrupted = False

    def break_second_replace(source_path, destination_path):
        nonlocal interrupted
        if Path(destination_path) == root / "workspace.toml" and not interrupted:
            interrupted = True
            raise OSError("simulated mirror process interruption")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(seed_module.os, "replace", break_second_replace)
    with pytest.raises(OSError, match="simulated mirror"):
        install_git_mirror(
            plan,
            journal,
            sql_store=sql_store,
            git_store=git_store,
            receipt=receipt,
            original_host_bytes=b"sql host",
            proposed_host=proposed_host,
        )
    monkeypatch.setattr(seed_module.os, "replace", real_replace)
    assert (root / "workspace-a.toml").read_text() == source.documents[2].content
    assert "old-second" in (root / "workspace.toml").read_text()
    resumed = recover_git_mirror_plan(
        journal,
        receipt=receipt,
        git_snapshot=git_store.snapshot,
        hq_dir=hq,
        workspace_root=root,
        proposed_host=proposed_host,
    )
    assert resumed == plan
    install_git_mirror(
        resumed,
        journal,
        sql_store=sql_store,
        git_store=git_store,
        receipt=receipt,
        original_host_bytes=b"sql host",
        proposed_host=proposed_host,
    )
    assert (root / "workspace.toml").read_text() == source.documents[3].content
    assert (hq / "fleet.yaml").read_text() == git_store.snapshot.documents[1].content
    (root / "workspace-a.toml").write_text("unreviewed edit\n")
    with pytest.raises(SeedError, match="source changed"):
        install_git_mirror(
            resumed,
            journal,
            sql_store=sql_store,
            git_store=git_store,
            receipt=receipt,
            original_host_bytes=b"sql host",
            proposed_host=proposed_host,
        )
