"""Portable HQ identity is explicit, canonical, and independent of backend names."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from beadhive import beadyard_identity as identity
from beadhive import beadyard_identity_file as identity_file
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
