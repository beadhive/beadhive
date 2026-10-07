"""bh-u67ve: an unrelated fleet-config publish must not fence SQL frames.

The pure predicate (:func:`config_head_tolerated`) decides what a moved config head may
change; :meth:`SqlRuntimeAuthority.bound_config_at` adds ancestry and the floor-free H0 read.
The live Dolt path is covered in ``tests/test_hq_sql_runtime_int.py``.
"""

from dataclasses import replace

import pytest

from beadhive import hq_sql_config
from beadhive import hq_sql_runtime as runtime
from beadhive.hq_hive_policy import config_head_tolerated, project_hive_policies
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot

H0 = "a" * 32
H1 = "b" * 32
NOW = 1000.0
EXPIRES = 5000.0

FLEET = (
    "managed_repos:\n"
    "- provider: github\n  org: bee\n  repo: hive\n  prefix: bh\n"
    "  frame_policy:\n    config_revision: desired-7\n"
    "    requires: {max_sessions: 2}\n"
    "    evict_after_s: 900\n"
    "- provider: github\n  org: bee\n  repo: legacy\n  prefix: lg\n"
)
HOST = "frame_id: frame-1\nhost_id: host-1\n"
SIGNERS = "operator ssh-ed25519 AAAA\n"
BEADYARD = '{"beadyard_id":"11111111-1111-4111-8111-111111111111"}\n'


def _snapshot(head, *, fleet=FLEET, host=HOST, signers=SIGNERS, beadyard=BEADYARD):
    documents = [FleetConfigDocument("allowed_signers", signers)]
    if beadyard is not None:
        documents.insert(0, FleetConfigDocument("beadyard.json", beadyard))
    documents += [
        FleetConfigDocument("fleet.yaml", fleet),
        FleetConfigDocument("hosts/host-1.yaml", host),
    ]
    return FleetConfigSnapshot(
        backend_identity="sql:config-backend",
        commit_revision=head,
        generation="config-generation",
        fetched_at=NOW,
        valid_until=NOW + 30,
        documents=tuple(documents),
    )


BOUND = _snapshot(H0)
SIGNED = project_hive_policies(BOUND, valid_until=EXPIRES, now=NOW)


def _tolerated(current, policies=SIGNED):
    return config_head_tolerated(BOUND, current, policies, valid_until=EXPIRES, now=NOW)


@pytest.mark.parametrize(
    "fleet",
    [
        FLEET + "hives:\n  bh:\n    work:\n      validation_bypass: true\n",
        FLEET + "work:\n  max_commits: 9\n",
        FLEET,  # an identical republish
    ],
    ids=["validation_bypass", "unrelated-section", "identical"],
)
def test_frame_irrelevant_fleet_edit_is_tolerated(fleet):
    assert _tolerated(_snapshot(H1, fleet=fleet))


@pytest.mark.parametrize(
    "change",
    [
        {"fleet": FLEET.replace("max_sessions: 2", "max_sessions: 3")},
        {"fleet": FLEET.replace("desired-7", "desired-8")},
        {"fleet": FLEET.replace("evict_after_s: 900", "evict_after_s: 60")},
        {"fleet": FLEET.replace("prefix: bh", "prefix: bx")},
        {"fleet": FLEET.replace("prefix: lg", "prefix: lh")},
        {"fleet": FLEET.replace("repo: legacy\n", "repo: legacy\n  kind: fork\n")},
        {"fleet": FLEET.replace("repo: hive", "repo: other")},
        {"fleet": FLEET + "- provider: github\n  org: bee\n  repo: new\n  prefix: nw\n"},
        {"fleet": "managed_repos: [\n"},
        {"host": HOST + "label: changed\n"},
        {"signers": SIGNERS + "intruder ssh-ed25519 BBBB\n"},
        {"beadyard": '{"beadyard_id":"22222222-2222-4222-8222-222222222222"}\n'},
        {"beadyard": None},
    ],
    ids=[
        "requires",
        "config_revision",
        "evict_after_s",
        "policy-prefix",
        "plain-prefix",
        "kind",
        "repo",
        "added-hive",
        "unparseable",
        "hosts",
        "allowed_signers",
        "beadyard_id",
        "beadyard-removed",
    ],
)
def test_frame_enforced_change_still_fences(change):
    assert not _tolerated(_snapshot(H1, **change))


def test_identity_backend_and_signed_projection_mismatches_fence():
    current = _snapshot(H1)
    assert not _tolerated(replace(current, generation="other-generation"))
    assert not _tolerated(replace(current, backend_identity="sql:other"))
    assert not _tolerated(current, policies={})
    assert not _tolerated(
        current, policies=project_hive_policies(current, valid_until=EXPIRES, now=NOW)
    )
    assert not config_head_tolerated(BOUND, object(), SIGNED, valid_until=EXPIRES, now=NOW)


class _Cursor:
    def __init__(self, ancestor):
        self.ancestor = ancestor
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return (self.ancestor,)


SETTINGS = {
    "reader": {"database": "config_db"},
    "runtime": {"database": "runtime_db"},
}
CROSSREF = ("sql:config-backend", "config-generation", H0)


def _authority(monkeypatch, current, bound=BOUND):
    authority = runtime.SqlRuntimeAuthority(SETTINGS, broker=object(), clock=lambda: NOW)
    monkeypatch.setattr(authority, "load_latest_config_at", lambda *a, **k: current)
    reads = []

    def committed(self, cursor, head):
        reads.append(head)
        if isinstance(bound, Exception):
            raise bound
        return bound

    monkeypatch.setattr(
        hq_sql_config.SqlFleetConfigRevisionStore, "committed_snapshot_at", committed
    )
    return authority, reads


def _bound(authority, cursor, **kwargs):
    return authority.bound_config_at(
        cursor, CROSSREF, **{"policies": SIGNED, "valid_until": EXPIRES, **kwargs}
    )


def test_tolerated_head_returns_the_h0_snapshot_and_pins_the_head_read(monkeypatch):
    current = _snapshot(H1, fleet=FLEET + "work:\n  max_commits: 9\n")
    authority, reads = _authority(monkeypatch, current)
    cursor = _Cursor(1)
    snapshot, head = _bound(authority, cursor)
    assert snapshot is BOUND and head == H1 and reads == [H0]
    assert ("SELECT HAS_ANCESTOR(%s,%s)", (H1, H0)) in cursor.sql
    # The pinned cursor is handed back on the runtime database.
    assert cursor.sql[-1] == ("USE `runtime_db`", None)
    assert authority.load_config_at(cursor, CROSSREF, policies=SIGNED, valid_until=EXPIRES) is (
        BOUND
    )


def test_exact_head_needs_no_ancestry_or_h0_read(monkeypatch):
    authority, reads = _authority(monkeypatch, BOUND)
    cursor = _Cursor(0)
    assert _bound(authority, cursor) == (BOUND, H0)
    assert reads == [] and cursor.sql == []


NOT_BOUND = "HQ config and authority publications are not bound"


@pytest.mark.parametrize(
    "case",
    ["non-descendant", "h0-unverifiable", "policy-change", "no-policies", "crossref-mismatch"],
)
def test_untolerated_head_fails_closed_with_the_existing_message(monkeypatch, case):
    current = _snapshot(H1)
    ancestor, bound, kwargs = 1, BOUND, {}
    if case == "non-descendant":
        ancestor = 0
    elif case == "h0-unverifiable":
        bound = hq_sql_config.SqlConfigError("HQ config document hash mismatch")
    elif case == "policy-change":
        current = _snapshot(H1, fleet=FLEET.replace("max_sessions: 2", "max_sessions: 3"))
    elif case == "no-policies":
        kwargs = {"policies": None}
    elif case == "crossref-mismatch":
        bound = replace(BOUND, generation="other-generation")
    authority, _reads = _authority(monkeypatch, current, bound)
    cursor = _Cursor(ancestor)
    with pytest.raises(runtime.SqlRuntimeError, match=NOT_BOUND):
        _bound(authority, cursor, **kwargs)
    with pytest.raises(runtime.SqlRuntimeError, match=NOT_BOUND):
        authority.load_config_at(cursor, CROSSREF)


def test_committed_snapshot_at_is_floor_free_pinned_and_memoized(monkeypatch, tmp_path):
    store = hq_sql_config.SqlFleetConfigRevisionStore(
        {
            "backend_identity": "config-backend",
            "generation": "config-generation",
            "cache_ttl": 30,
            "floor_path": str(tmp_path / "floor.json"),
        },
        broker=object(),
        clock=lambda: NOW,
    )
    calls = []

    def committed(cursor, head, **kwargs):
        calls.append(head)
        return "config-backend", "config-generation", 3, BOUND.documents, 3

    monkeypatch.setattr(store, "_committed", committed)
    monkeypatch.setattr(
        store, "_check_floor", lambda *a, **k: pytest.fail("H0 must not touch the floor")
    )
    monkeypatch.setattr(hq_sql_config, "_COMMITTED_CACHE", {})
    first = store.committed_snapshot_at(object(), H0)
    second = store.committed_snapshot_at(object(), H0)
    assert first.commit_revision == H0 and first.documents == BOUND.documents
    assert first.backend_identity == "sql:config-backend" and second == first
    assert calls == [H0]
    with pytest.raises(hq_sql_config.SqlConfigError):
        store.committed_snapshot_at(object(), "not-a-head")
    monkeypatch.setattr(
        store, "_committed", lambda *a, **k: ("other", "config-generation", 3, (), 3)
    )
    with pytest.raises(hq_sql_config.SqlConfigError, match="pin"):
        store.committed_snapshot_at(object(), H1)
