"""HQ config publication and its ambiguous-commit recovery over the HQ storage port (bh-x7xtc).

Storage is the port's in-memory fake. Root's publisher preflight and readback are SQL reads
(still on a reader cursor, see ``beadhive.hq_sql_config``), so these tests replace exactly those
two root seams and drive the port decision in between: ``Applied`` / ``Conflict`` / ``Unknown``
on ``publish`` and ``Confirmed`` / ``NotConfirmed`` on ``lookup_publication``. The strict policy
under test is ADR section 2, rule 4: ``Unknown`` is ``PublicationUnknown`` with the ORIGINAL
UUID and parent, and ``NotConfirmed`` never reissues, re-mints or retries a refreshed parent.

The Dolt encoding of the same publication (the ``hq_config_*`` rows, the explicit ``DOLT_ADD``
list) is the adapter's, proven on real Dolt in
``packages/beadhive-hq-storage-dolt/tests/test_hq_storage_dolt_adapter.py``; the existing
end-to-end behaviour stays in ``tests/test_hq_sql_config_int.py``.
"""

from __future__ import annotations

import uuid

import pytest
from tests.harness.clock import FakeClock

from beadhive.hq_sql_config import (
    TABLES,
    PublicationRecovery,
    PublicationUnknown,
    SqlConfigError,
    SqlFleetConfigRevisionStore,
    confirmed_publication,
    publication_changes,
    publish_documents,
)
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot
from beadhive_hq_storage import ChangeSet, InMemoryHqStorage, Revision

DOCUMENTS = (
    FleetConfigDocument("fleet.yaml", "hq:\n  mode: dolt-server\n"),
    FleetConfigDocument(
        "workspace.toml",
        "[[provider]]\nprovider = 'github'\nname = 'first'\npath = 'github'\n",
    ),
)
NEXT = (FleetConfigDocument("fleet.yaml", "hq:\n  mode: dolt-server\nmanaged_repos: []\n"),)


class _Recording:
    """The fake behind a call log, so a test can prove nothing was reissued."""

    def __init__(self, fake: InMemoryHqStorage) -> None:
        self.fake = fake
        self.calls: list[tuple[str, str, str]] = []

    def publish(self, changes: ChangeSet, *, expected_head, operation, author, message):
        self.calls.append(("publish", operation, expected_head))
        return self.fake.publish(
            changes,
            expected_head=expected_head,
            operation=operation,
            author=author,
            message=message,
        )

    def lookup_publication(self, operation, *, expected_head):
        self.calls.append(("lookup_publication", operation, expected_head))
        return self.fake.lookup_publication(operation, expected_head=expected_head)

    def __getattr__(self, name: str):
        return getattr(self.fake, name)


def _documents_at(fake: InMemoryHqStorage, revision: str) -> tuple[FleetConfigDocument, ...]:
    return tuple(
        FleetConfigDocument(name.split("/", 1)[1], raw.decode())
        for name, raw in fake.read_at(Revision(revision))
        if name.startswith("hq_config_documents/")
    )


@pytest.fixture
def world(monkeypatch):
    fake = InMemoryHqStorage(FakeClock())
    history = _Recording(fake)
    store = SqlFleetConfigRevisionStore({}, broker=object(), history=history)
    preflights: list[str] = []

    def preflight(documents, *, expected_revision, allow_invalid_previous, explicit_adoption):
        preflights.append(expected_revision)
        if fake.head() != expected_revision:
            raise SqlConfigError("expected configuration revision changed")
        return 1

    def load_snapshot(*, revision=None, deadline=None):
        head = str(fake.head())
        if revision is not None and revision != head:
            raise SqlConfigError("requested configuration revision is no longer current")
        return FleetConfigSnapshot("sql:fixture", head, "g", 0.0, 10.0, _documents_at(fake, head))

    monkeypatch.setattr(store, "_publication_preflight", preflight)
    monkeypatch.setattr(store, "load_snapshot", load_snapshot)
    return store, fake, history, preflights


def test_the_change_set_is_the_explicit_three_table_allowlist() -> None:
    changes = publication_changes(DOCUMENTS, schema_version=2)
    assert changes.tables == frozenset(TABLES)
    assert changes.documents == (
        ("hq_config_meta/schema_version", b"2"),
        ("hq_config_documents/fleet.yaml", DOCUMENTS[0].content.encode()),
        ("hq_config_documents/workspace.toml", DOCUMENTS[1].content.encode()),
    )


def test_an_applied_publication_returns_the_exact_committed_snapshot(world) -> None:
    store, fake, history, preflights = world
    parent = str(fake.head())
    published = store.publish_snapshot(DOCUMENTS, expected_revision=parent)
    assert published.commit_revision == str(fake.head()) != parent
    assert published.documents == DOCUMENTS
    assert preflights == [parent]
    ((verb, operation, expected),) = history.calls
    assert (verb, expected) == ("publish", parent)
    assert uuid.UUID(operation).version == 4


def test_a_caller_publication_id_is_the_port_operation(world) -> None:
    store, fake, history, _ = world
    publication_id = str(uuid.uuid4())
    store.publish_snapshot(
        DOCUMENTS, expected_revision=str(fake.head()), publication_id=publication_id
    )
    assert history.calls[0][1] == publication_id


def test_a_lost_race_is_the_publication_cas_conflict_and_nothing_moves(world) -> None:
    store, fake, history, _ = world
    parent = str(fake.head())
    fake.publish(
        publication_changes(NEXT, schema_version=1),
        expected_head=Revision(parent),
        operation=str(uuid.uuid4()),
        author="racer",
        message="won",
    )
    winner = str(fake.head())
    # The preflight saw the original parent; the port's own head check loses the race.
    with pytest.raises(SqlConfigError, match="configuration publication CAS conflict"):
        publish_documents(
            history,
            DOCUMENTS,
            schema_version=1,
            expected_revision=parent,
            publication_id=str(uuid.uuid4()),
        )
    assert str(fake.head()) == winner


@pytest.mark.parametrize("reason", ["deadline", "transport", "commit"])
@pytest.mark.parametrize("applied", [True, False])
def test_unknown_is_publication_unknown_with_the_original_intent_and_no_reissue(
    world, reason, applied
) -> None:
    store, fake, history, _ = world
    parent = str(fake.head())
    publication_id = str(uuid.uuid4())
    fake.faults.arm_unknown(reason, applied=applied, on="publish")
    with pytest.raises(PublicationUnknown) as uncertain:
        store.publish_snapshot(DOCUMENTS, expected_revision=parent, publication_id=publication_id)
    unknown = uncertain.value
    assert (unknown.publication_id, unknown.expected_revision, unknown.reason) == (
        publication_id,
        parent,
        reason,
    )
    assert [call[0] for call in history.calls] == ["publish"]  # never reissued
    assert (str(fake.head()) != parent) is applied


def test_a_confirmed_lookup_recovers_the_exact_original_publication(world, monkeypatch) -> None:
    store, fake, history, _ = world
    parent = str(fake.head())
    publication_id = str(uuid.uuid4())
    fake.faults.arm_unknown("commit", applied=True, on="publish")
    with pytest.raises(PublicationUnknown):
        store.publish_snapshot(DOCUMENTS, expected_revision=parent, publication_id=publication_id)
    head = str(fake.head())
    monkeypatch.setattr(
        store,
        "_publication_witness",
        lambda pid, expected: (head, ((2, expected, "d" * 64),), True),
    )
    recovered = store.recover_publication(publication_id, expected_revision=parent)
    assert recovered == PublicationRecovery(publication_id, parent, head, 2, "d" * 64)
    assert history.calls[-1] == ("lookup_publication", publication_id, parent)
    assert [call[0] for call in history.calls].count("publish") == 1


def test_not_confirmed_is_none_and_never_reissues_or_retries_a_refreshed_parent(
    world, monkeypatch
) -> None:
    store, fake, history, _ = world
    parent = str(fake.head())
    publication_id = str(uuid.uuid4())
    fake.faults.arm_unknown("transport", applied=False, on="publish")
    with pytest.raises(PublicationUnknown):
        store.publish_snapshot(DOCUMENTS, expected_revision=parent, publication_id=publication_id)
    monkeypatch.setattr(store, "_publication_witness", lambda pid, expected: (parent, (), False))
    assert store.recover_publication(publication_id, expected_revision=parent) is None
    # A later writer moves the head; NotConfirmed still authorises nothing.
    fake.publish(
        publication_changes(NEXT, schema_version=1),
        expected_head=Revision(parent),
        operation=str(uuid.uuid4()),
        author="later",
        message="later",
    )
    assert store.recover_publication(publication_id, expected_revision=parent) is None
    assert (
        confirmed_publication(history, publication_id, expected_revision=str(fake.head())) is None
    )
    assert [call[0] for call in history.calls] == ["publish"] + ["lookup_publication"] * 3
    assert {call[1] for call in history.calls} == {publication_id}


def test_a_witness_against_another_parent_is_refused(world, monkeypatch) -> None:
    store, fake, _, _ = world
    publication_id = str(uuid.uuid4())
    monkeypatch.setattr(
        store,
        "_publication_witness",
        lambda pid, expected: ("h", ((2, "other-parent", "d" * 64),), False),
    )
    with pytest.raises(SqlConfigError, match="identity or parent mismatch"):
        store.recover_publication(publication_id, expected_revision=str(fake.head()))


def test_a_witness_the_head_does_not_descend_from_is_refused(world, monkeypatch) -> None:
    store, fake, _, _ = world
    parent = str(fake.head())
    monkeypatch.setattr(
        store,
        "_publication_witness",
        lambda pid, expected: ("h", ((2, expected, "d" * 64),), False),
    )
    with pytest.raises(SqlConfigError, match="not an ancestor"):
        store.recover_publication(str(uuid.uuid4()), expected_revision=parent)


def test_a_witness_row_the_port_has_not_confirmed_is_still_no_outcome(world, monkeypatch) -> None:
    store, fake, history, _ = world
    parent = str(fake.head())
    monkeypatch.setattr(
        store,
        "_publication_witness",
        lambda pid, expected: ("h", ((2, expected, "d" * 64),), True),
    )
    assert store.recover_publication(str(uuid.uuid4()), expected_revision=parent) is None
    assert [call[0] for call in history.calls] == ["lookup_publication"]


def test_a_failed_readback_after_applied_is_publication_unknown(world, monkeypatch) -> None:
    store, fake, history, _ = world
    parent = str(fake.head())

    def lose_readback(*, revision=None, deadline=None):
        raise SqlConfigError("simulated lost post-COMMIT acknowledgment")

    monkeypatch.setattr(store, "load_snapshot", lose_readback)
    with pytest.raises(PublicationUnknown) as uncertain:
        store.publish_snapshot(DOCUMENTS, expected_revision=parent)
    assert uncertain.value.expected_revision == parent and uncertain.value.reason is None
    assert str(fake.head()) != parent  # it did apply; recovery is by the original UUID
    assert [call[0] for call in history.calls] == ["publish"]


def test_unavailable_storage_is_a_refusal_before_anything_was_sent(world) -> None:
    store, fake, _, _ = world
    parent = str(fake.head())
    fake.faults.arm_unavailable(on="publish")
    with pytest.raises(SqlConfigError, match="configuration publication failed") as refused:
        store.publish_snapshot(DOCUMENTS, expected_revision=parent)
    assert not isinstance(refused.value, PublicationUnknown)
    assert str(fake.head()) == parent


def test_validation_and_preflight_refusals_never_reach_the_port(world) -> None:
    store, fake, history, _ = world
    with pytest.raises(SqlConfigError, match="schema_version"):
        store.publish_snapshot(
            (FleetConfigDocument("fleet.yaml", "schema_version: true\n"),),
            expected_revision=str(fake.head()),
        )
    with pytest.raises(SqlConfigError, match="expected configuration revision changed"):
        store.publish_snapshot(DOCUMENTS, expected_revision="not-the-head")
    with pytest.raises(SqlConfigError, match="immutable publication ID invalid"):
        store.publish_snapshot(
            DOCUMENTS, expected_revision=str(fake.head()), publication_id="not-a-uuid"
        )
    assert history.calls == []


def test_lookup_unavailable_is_recovery_unavailable(world) -> None:
    _, fake, history, _ = world

    class _Down:
        def lookup_publication(self, operation, *, expected_head):
            from beadhive_hq_storage import StorageUnavailable

            raise StorageUnavailable("down")

    with pytest.raises(SqlConfigError, match="recovery unavailable"):
        confirmed_publication(_Down(), str(uuid.uuid4()), expected_revision=str(fake.head()))
