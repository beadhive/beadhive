"""Portable HQ identity is explicit, canonical, and independent of backend names."""

from __future__ import annotations

import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from beadhive import beadyard_identity as identity
from beadhive import beadyard_identity_file as identity_file
from beadhive import hq_beadyard
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot


def test_explicit_new_hq_ids_are_random_uuid4_and_do_not_depend_on_names():
    first = identity.parse_document(identity.new_document())
    second = identity.parse_document(identity.new_document())
    assert first != second
    assert uuid.UUID(first).version == uuid.UUID(second).version == 4
    assert first == str(uuid.UUID(first))


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        "[]",
        "not-json",
        '{"beadyard_id":"00000000-0000-0000-0000-000000000000"}',
        '{"beadyard_id":"12345678-1234-1234-1234-123456789012"}',
        '{"beadyard_id":"wrong"}',
        '{"beadyard_id":null}',
        '{"beadyard_id":"12345678-1234-4234-8234-123456789012","other":true}',
        '{"beadyard_id":"12345678-1234-4234-8234-123456789012",'
        '"beadyard_id":"12345678-1234-4234-8234-123456789012"}',
    ],
)
def test_identity_document_rejects_invalid_or_ambiguous_content(content):
    with pytest.raises(identity.BeadyardIdentityError):
        identity.parse_document(content)


def test_existing_identity_reads_never_generate_or_replace_it(monkeypatch):
    value = str(uuid.uuid4())
    document = FleetConfigDocument(
        identity.DOCUMENT_PATH, json.dumps({"beadyard_id": value}) + "\n"
    )
    monkeypatch.setattr(identity.uuid, "uuid4", lambda: pytest.fail("read generated identity"))
    assert identity.identity_in_documents((document,)) == value
    assert identity.identity_in_documents((), required=False) is None
    with pytest.raises(identity.BeadyardIdentityError, match="explicit adoption"):
        identity.identity_in_documents(())


def test_duplicate_and_foreign_identity_documents_are_denied():
    first = identity.new_document()
    second = identity.new_document()
    documents = (
        FleetConfigDocument(identity.DOCUMENT_PATH, first),
        FleetConfigDocument(identity.DOCUMENT_PATH, second),
    )
    with pytest.raises(identity.BeadyardIdentityError, match="duplicate"):
        identity.identity_in_documents(documents)
    source = identity.parse_document(first)
    destination = identity.parse_document(second)
    with pytest.raises(identity.BeadyardIdentityError, match="another beadyard"):
        identity.require_same_identity(source, destination)
    with pytest.raises(identity.BeadyardIdentityError, match="missing"):
        identity.require_same_identity(source, None)
    assert identity.require_same_identity(source, source) == source


def test_git_hq_creation_is_durable_exclusive_and_idempotent(tmp_path, monkeypatch):
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    assert identity_file.read_identity(hq_dir) is None
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: identity_file.create_identity(hq_dir), range(24)))
    assert len(set(values)) == 1
    assert identity_file.read_identity(hq_dir) == values[0]
    assert list(hq_dir.iterdir()) == [hq_dir / identity.DOCUMENT_PATH]
    monkeypatch.setattr(
        identity_file, "new_document", lambda: pytest.fail("idempotent setup minted")
    )
    assert identity_file.create_identity(hq_dir) == values[0]


def test_git_hq_read_rejects_malformed_and_symlink_identity(tmp_path):
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    path = hq_dir / identity.DOCUMENT_PATH
    path.write_text("{}")
    with pytest.raises(identity.BeadyardIdentityError):
        identity_file.read_identity(hq_dir)
    path.unlink()
    path.symlink_to(tmp_path / "foreign")
    with pytest.raises(identity.BeadyardIdentityError):
        identity_file.read_identity(hq_dir)


def test_git_hq_independent_instances_remain_distinct_after_copy(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    restored = tmp_path / "restored"
    for directory in (first, second, restored):
        directory.mkdir()
    first_id = identity_file.create_identity(first)
    assert identity_file.create_identity(second) != first_id
    (restored / identity.DOCUMENT_PATH).write_bytes((first / identity.DOCUMENT_PATH).read_bytes())
    assert identity_file.read_identity(restored) == first_id


def test_snapshot_identity_is_derived_from_one_raw_document():
    document = FleetConfigDocument(identity.DOCUMENT_PATH, identity.new_document())
    base = dict(
        backend_identity="git:source",
        commit_revision="commit",
        generation="generation",
        fetched_at=1,
        valid_until=2,
    )
    assert FleetConfigSnapshot(**base, documents=()).beadyard_id is None
    snapshot = FleetConfigSnapshot(**base, documents=(document,))
    assert snapshot.beadyard_id == identity.parse_document(document.content)


def test_bound_publication_cannot_mutate_or_remove_identity():
    first = FleetConfigDocument(identity.DOCUMENT_PATH, identity.new_document())
    second = FleetConfigDocument(identity.DOCUMENT_PATH, identity.new_document())
    original = (FleetConfigDocument("fleet.yaml", "{}"), first)
    assert identity.validate_publication_identity(original, original) == identity.parse_document(
        first.content
    )
    for proposed in ((FleetConfigDocument("fleet.yaml", "{}"),), (second,)):
        with pytest.raises(identity.BeadyardIdentityError, match="cannot change or disappear"):
            identity.validate_publication_identity(original, proposed)


def test_legacy_publication_stays_legacy_until_explicit_adoption():
    legacy = (FleetConfigDocument("fleet.yaml", "{}"),)
    bound = legacy + (FleetConfigDocument(identity.DOCUMENT_PATH, identity.new_document()),)
    assert identity.validate_publication_identity(legacy, legacy) is None
    with pytest.raises(identity.BeadyardIdentityError, match="explicit adoption"):
        identity.validate_publication_identity(legacy, bound)
    assert identity.validate_publication_identity(
        legacy, bound, explicit_adoption=True
    ) == identity.parse_document(bound[-1].content)
    with pytest.raises(identity.BeadyardIdentityError, match="conflicts with local"):
        identity.validate_publication_identity((), bound, local_id=str(uuid.uuid4()))


def test_git_legacy_adoption_is_signed_original_head_cas_and_retryable(tmp_path, monkeypatch):
    from beadhive import config

    root, remote, key = tmp_path / "hq", tmp_path / "origin.git", tmp_path / "operator"
    root.mkdir()

    def git(*args, data=None):
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True,
            input=data,
        )
        return result.stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Operator")
    git("config", "user.email", "operator@example.invalid")
    (root / "fleet.yaml").write_text("schema_version: 1\n")
    git("add", "fleet.yaml")
    git("commit", "-qm", "legacy HQ")
    original = git("rev-parse", "main")
    git("init", "--bare", "-q", "-b", "main", str(remote))
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "origin", "main")
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    monkeypatch.setattr(config, "load_host", lambda: {"hq": {}})
    assert hq_beadyard.inspect(root).state == "legacy"
    original_commit = hq_beadyard._commit_git_adoption
    original_index = hq_beadyard._ensure_identity_index

    def interrupted(*_args):
        raise hq_beadyard.BeadyardOperationError("interrupted after durable identity")

    monkeypatch.setattr(hq_beadyard, "_commit_git_adoption", interrupted)
    with pytest.raises(hq_beadyard.BeadyardOperationError, match="interrupted"):
        hq_beadyard.adopt_legacy(hq_dir=root, expected_revision=original, operator_key=key)
    pending = hq_beadyard.inspect(root)
    assert pending.state == "pending"
    assert pending.beadyard_id == identity_file.read_identity(root)
    foreign_key = tmp_path / "foreign-operator"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(foreign_key)],
        check=True,
    )

    def forged_adoption(signer, *, unrelated):
        blob = git("hash-object", "-w", "beadyard.json")
        rows = git("ls-tree", original) + f"\n100644 blob {blob}\tbeadyard.json\n"
        if unrelated:
            extra = git("hash-object", "-w", "--stdin", data="unrelated edit")
            rows += f"100644 blob {extra}\tzz-extra.txt\n"
        tree = git("mktree", data=rows)
        return git(
            "-c", "gpg.format=ssh", "-c", f"user.signingkey={signer}",
            "commit-tree", "-S", tree, "-p", original,
            data="Adopt canonical beadyard identity\n",
        )

    for signer, unrelated in ((key, True), (foreign_key, False)):
        forged = forged_adoption(signer, unrelated=unrelated)
        git("update-ref", "refs/heads/main", forged, original)
        with pytest.raises(
            hq_beadyard.BeadyardOperationError, match="adoption.*(descendant|trusted)"
        ):
            hq_beadyard.adopt_legacy(hq_dir=root, expected_revision=original, operator_key=key)
        git("update-ref", "refs/heads/main", original, forged)
    monkeypatch.setattr(hq_beadyard, "_commit_git_adoption", original_commit)
    monkeypatch.setattr(
        hq_beadyard,
        "_ensure_identity_index",
        lambda *_args: (_ for _ in ()).throw(
            hq_beadyard.BeadyardOperationError("interrupted after main CAS")
        ),
    )
    with pytest.raises(hq_beadyard.BeadyardOperationError, match="after main CAS"):
        hq_beadyard.adopt_legacy(hq_dir=root, expected_revision=original, operator_key=key)
    adopted_commit = git("rev-parse", "main")
    assert adopted_commit != original
    assert hq_beadyard.inspect(root).state == "pending"
    monkeypatch.setattr(hq_beadyard, "_ensure_identity_index", original_index)
    git("add", "beadyard.json")
    (root / "later.txt").write_text("ordinary subsequent main edit\n")
    git("add", "later.txt")
    git("commit", "-qm", "Later main edit")
    (root / "staged.txt").write_text("unrelated staged change\n")
    git("add", "staged.txt")
    original_finish = hq_beadyard._finish_git_adoption

    def lost_final_reply(root):
        original_finish(root)
        raise hq_beadyard.BeadyardOperationError("lost reply after durable completion")

    monkeypatch.setattr(hq_beadyard, "_finish_git_adoption", lost_final_reply)
    with pytest.raises(hq_beadyard.BeadyardOperationError, match="lost reply"):
        hq_beadyard.adopt_legacy(hq_dir=root, expected_revision=original, operator_key=key)
    assert hq_beadyard._completed_original(root) is not None
    assert hq_beadyard._pending_original(root) is None
    monkeypatch.setattr(hq_beadyard, "_finish_git_adoption", original_finish)
    bound = hq_beadyard.adopt_legacy(
        hq_dir=root, expected_revision=original, operator_key=key
    )
    assert bound.state == "bound"
    assert bound.beadyard_id == pending.beadyard_id
    assert bound.revision != original
    assert git("rev-parse", "main") == bound.revision
    assert identity.parse_document(git("show", "main:beadyard.json")) == bound.beadyard_id
    assert "gpgsig" in git("cat-file", "-p", adopted_commit)
    assert git("status", "--porcelain") == "A  staged.txt"
    git("commit", "-qm", "Preserve staged edit")
    assert git("status", "--porcelain") == ""
    assert git("ls-remote", "origin", "refs/heads/main").split()[0] == original
    assert hq_beadyard.adopt_legacy(
        hq_dir=root, expected_revision=original, operator_key=key
    ).beadyard_id == bound.beadyard_id
    with pytest.raises(hq_beadyard.BeadyardOperationError, match="original revision"):
        hq_beadyard.adopt_legacy(hq_dir=root, expected_revision=bound.revision, operator_key=key)
    with pytest.raises(hq_beadyard.BeadyardOperationError, match="operator signing key"):
        hq_beadyard.adopt_legacy(hq_dir=root, expected_revision=original, operator_key=foreign_key)
    hq_beadyard._git_adoption_receipt(root).chmod(0o666)
    with pytest.raises(hq_beadyard.BeadyardOperationError, match="intent is invalid"):
        hq_beadyard.adopt_legacy(hq_dir=root, expected_revision=original, operator_key=key)
