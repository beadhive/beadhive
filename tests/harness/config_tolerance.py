"""SQL-free fixtures for the config-head tolerance check (bh-u67ve, bh-3h6al).

A signed head H0 with a frame-policy catalog, descendant heads H1 that either change only
frame-irrelevant ``fleet.yaml`` content or something frames enforce, and a real
:class:`SqlFleetConfigRevisionStore` whose connection/reads are faked so its
``authority_binding`` runs the shared tolerance check end to end.
"""

from __future__ import annotations

from contextlib import contextmanager

from beadhive import hq_sql_config
from beadhive.hq_hive_policy import project_hive_policies
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot

H0 = "a" * 32
H1 = "b" * 32
BACKEND = "sql:config-backend"
GENERATION = "config-generation"
CROSSREF = (BACKEND, GENERATION, H0)

FLEET = (
    "managed_repos:\n"
    "- provider: github\n  org: bee\n  repo: hive\n  prefix: bh\n"
    "  frame_policy:\n    config_revision: desired-7\n"
    "    requires: {max_sessions: 2}\n"
    "    evict_after_s: 900\n"
)
HOST = "frame_id: frame-1\nhost_id: host-1\n"
#: A per-hive override no frame enforces (the 2026-10-04 incident).
BYPASS_FLEET = FLEET.replace("prefix: bh\n", "prefix: bh\n  work: {validation_bypass: true}\n")
#: A frame-enforced policy edit.
POLICY_FLEET = FLEET.replace("max_sessions: 2", "max_sessions: 3")


def documents(fleet=FLEET, host=HOST):
    return (
        FleetConfigDocument("fleet.yaml", fleet),
        FleetConfigDocument("hosts/host-1.yaml", host),
    )


def snapshot(head, fleet=FLEET, host=HOST, *, now):
    return FleetConfigSnapshot(
        backend_identity=BACKEND,
        commit_revision=head,
        generation=GENERATION,
        fetched_at=now,
        valid_until=now + 30,
        documents=documents(fleet, host),
    )


def signed_policies(*, expires_at, now):
    return project_hive_policies(snapshot(H0, now=now), valid_until=expires_at, now=now)


class _Cursor:
    def __init__(self, ancestor):
        self.ancestor = ancestor

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (self.ancestor,)


class _Socket:
    def settimeout(self, value):
        pass


class _Connection:
    def __init__(self, ancestor):
        self.ancestor = ancestor
        self._sock = _Socket()

    @contextmanager
    def cursor(self):
        yield _Cursor(self.ancestor)

    def rollback(self):
        pass

    def close(self):
        pass


def binding_store(monkeypatch, current, *, now, ancestor=1):
    """A real config store whose latest head is `current` (H0 when exact)."""
    store = hq_sql_config.SqlFleetConfigRevisionStore(
        {
            "reader": {"read_timeout": 5, "write_timeout": 5},
            "backend_identity": BACKEND.removeprefix("sql:"),
            "generation": GENERATION,
            "cache_ttl": 30,
        },
        broker=object(),
        clock=lambda: now,
    )
    monkeypatch.setattr(store, "_open", lambda role, deadline=None: (_Connection(ancestor), 1e18))
    monkeypatch.setattr(store, "_identity", lambda cursor, role: None)
    monkeypatch.setattr(store, "_head", lambda cursor: current.commit_revision)
    monkeypatch.setattr(store, "_snapshot", lambda cursor, head, deadline=None: current)
    monkeypatch.setattr(store, "committed_snapshot_at", lambda cursor, head: snapshot(H0, now=now))
    return store
