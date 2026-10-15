"""Release-upgrade recovery paths found in the 0.24.3 fleet upgrade (bh-upgrade-p1 batch).

* bh-odevt: prepare-config cancels an unapplied preparation back to the admitted release.
* bh-aprgo: missing runtime read privileges are named, never masked.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import test_frame_release_upgrade as base
from beadhive import frame_release_upgrade as upgrade
from beadhive import hq_sql_capabilities as capabilities
from beadhive.hq_sql_runtime_schema import inbox_table, session_table

fixture = base.fixture


# --------------------------------------------------------------------------------------------
# A public-verb harness: release_upgrade() over an in-memory config store and operator.
# --------------------------------------------------------------------------------------------


class Store:
    """In-memory config store with the publisher's original-head CAS."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.published = []
        self.fail = None  # "before" (no write) or "after" (written, ack lost)

    def load_snapshot(self):
        return self.snapshot

    def publish_snapshot(self, documents, *, expected_revision):
        if expected_revision != self.snapshot.commit_revision:
            raise ValueError("config CAS conflict")
        if self.fail == "before":
            raise OSError("connection reset")
        head = f"{len(self.published) + 1:032x}"
        self.snapshot = replace(self.snapshot, commit_revision=head, documents=tuple(documents))
        self.published.append(documents)
        if self.fail == "after":
            raise OSError("acknowledgment lost")
        return self.snapshot


class Operator:
    def __init__(self, fixture, *, incumbent="inbox", provisioned=(), preflight=None):
        self.fixture = fixture
        self.head = "runtime-head"
        self.fences = []
        self.fence_fails = False
        self.incumbent = incumbent
        self.provisioned = set(provisioned)
        self.preflights = []
        self._preflight = preflight or {
            "route": "session",
            "runtime_read": [],
            "apply": [],
            "provision": [],
            "ready": {"apply": True, "provision": True},
            "docs": "docs/HQ.md#runtime-sql-privileges",
        }
        self.authority = SimpleNamespace(fresh_runtime_head_fence=self._fence)

    def _fence(self, head, **kwargs):
        self.fences.append(head)
        if self.fence_fails or head != self.head:
            raise ValueError("runtime HEAD changed")

    def load(self, **kwargs):
        return self.head, self.fixture.state, (), {}

    def describe_release_route(self, principal, epoch, frame, *, target_route=None, **kwargs):
        inbox, session = inbox_table(principal, epoch), session_table(principal, epoch)
        if inbox in self.provisioned:
            table = inbox
        elif session in self.provisioned:
            table = session
        else:
            table = session if "session" in (self.incumbent, target_route) else inbox
        return {
            "principal": principal,
            "incumbent_route": self.incumbent,
            "inbox_table": table,
            "route": "session" if table == session else "inbox",
            "provisioned": table in self.provisioned,
        }

    def preflight(self, *, route, **kwargs):
        self.preflights.append(route)
        return copy.deepcopy(self._preflight)


def plane(fixture, store, operator):
    return SimpleNamespace(
        _operator=lambda: operator,
        _operator_deadline=lambda: 100000000000,
        config_store=lambda: store,
        clock=lambda: 1000,
    )


def manifest_release(store, fixture):
    path = f"hosts/{fixture.record['authority']['holder_identity']}.yaml"
    (document,) = [row for row in store.snapshot.documents if row.path == path]
    return json.loads(document.content)["release"]


def rollback_request(fixture, store):
    request = copy.deepcopy(fixture.request)
    request["release"] = copy.deepcopy(fixture.record["desired"]["release"])
    request["config_head"] = store.snapshot.commit_revision
    return request


# --------------------------------------------------------------------------------------------
# bh-odevt: cancel an unapplied preparation
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["pending", "active"])
def test_failed_provision_or_apply_after_preparation_rolls_back_to_admitted(fixture, shape):
    if shape == "active":
        base.make_active(fixture)
    # The fixture's canonical manifest is already prepared for the new release, while the
    # authority still admits the old one: the state a failed provision/apply leaves behind.
    store, operator = Store(fixture.snapshot), Operator(fixture)
    before = copy.deepcopy(fixture.state)
    admitted = fixture.record["desired"]["release"]
    assert manifest_release(store, fixture) == fixture.request["release"] != admitted
    request = rollback_request(fixture, store)
    reviewed = upgrade.release_upgrade(
        plane(fixture, store, operator), "frame", "prepare-config", request=request
    )
    assert reviewed["rollback"] is True and reviewed["already_prepared"] is False
    assert reviewed["canonical_release_before"] == fixture.request["release"]
    assert reviewed["release"] == admitted and not store.published
    applied = upgrade.release_upgrade(
        plane(fixture, store, operator),
        "frame",
        "prepare-config",
        request=request,
        confirm=True,
        plan_sha256=reviewed["plan_sha256"],
    )
    assert applied["applied"] is True and applied["config_head"] == store.snapshot.commit_revision
    assert manifest_release(store, fixture) == admitted
    # Only the release of the one canonical manifest changed; every other document and the
    # protected authority (identity, epoch, floors) are untouched. No rebind, no publisher API.
    original = {row.path: row.content for row in fixture.snapshot.documents}
    after = {row.path: row.content for row in store.snapshot.documents}
    assert list(after) == list(original)
    changed = [path for path in after if after[path] != original[path]]
    assert changed == [f"hosts/{fixture.record['authority']['holder_identity']}.yaml"]
    restored = json.loads(after[changed[0]])
    prepared = json.loads(original[changed[0]])
    assert {**prepared, "release": admitted} == restored
    assert fixture.state == before
    assert operator.fences == ["runtime-head", "runtime-head"]


def test_already_restored_rollback_is_an_idempotent_no_op(fixture):
    fixture.manifest["release"] = copy.deepcopy(fixture.record["desired"]["release"])
    base.replace_manifest(fixture)
    store, operator = Store(fixture.snapshot), Operator(fixture)
    request = rollback_request(fixture, store)
    reviewed = upgrade.release_upgrade(
        plane(fixture, store, operator), "frame", "prepare-config", request=request
    )
    assert reviewed["rollback"] and reviewed["already_prepared"]
    applied = upgrade.release_upgrade(
        plane(fixture, store, operator),
        "frame",
        "prepare-config",
        request=request,
        confirm=True,
        plan_sha256=reviewed["plan_sha256"],
    )
    assert applied["applied"] and applied["config_head"] == request["config_head"]
    assert not store.published


def test_ambiguous_rollback_publication_is_read_back_and_rereviewed(fixture):
    store, operator = Store(fixture.snapshot), Operator(fixture)
    request = rollback_request(fixture, store)
    reviewed = upgrade.release_upgrade(
        plane(fixture, store, operator), "frame", "prepare-config", request=request
    )
    store.fail = "after"
    with pytest.raises(ValueError, match="not acknowledged.*observed committed config HEAD"):
        upgrade.release_upgrade(
            plane(fixture, store, operator),
            "frame",
            "prepare-config",
            request=request,
            confirm=True,
            plan_sha256=reviewed["plan_sha256"],
        )
    store.fail = None
    # Replaying the stale digest against the moved config head refuses (CAS), never re-writes.
    with pytest.raises(ValueError, match="CAS mismatch"):
        upgrade.release_upgrade(
            plane(fixture, store, operator),
            "frame",
            "prepare-config",
            request=request,
            confirm=True,
            plan_sha256=reviewed["plan_sha256"],
        )
    # Re-review at the read-back head: the rollback committed, so it is an exact no-op.
    request = rollback_request(fixture, store)
    again = upgrade.release_upgrade(
        plane(fixture, store, operator), "frame", "prepare-config", request=request
    )
    assert again["rollback"] and again["already_prepared"]
    assert len(store.published) == 1


@pytest.mark.parametrize("change", ["config", "runtime_before", "runtime_after", "epoch", "id"])
def test_rollback_refuses_concurrent_authority_or_config_changes(fixture, change):
    store, operator = Store(fixture.snapshot), Operator(fixture)
    request = rollback_request(fixture, store)
    reviewed = upgrade.release_upgrade(
        plane(fixture, store, operator), "frame", "prepare-config", request=request
    )
    kwargs = {"confirm": True, "plan_sha256": reviewed["plan_sha256"]}
    if change == "config":
        store.snapshot = replace(store.snapshot, commit_revision="d" * 32)
        with pytest.raises(ValueError, match="CAS mismatch"):
            upgrade.release_upgrade(
                plane(fixture, store, operator),
                "frame",
                "prepare-config",
                request=request,
                **kwargs,
            )
        assert not store.published
    elif change == "runtime_before":
        operator.fence_fails = True
        with pytest.raises(ValueError, match="runtime HEAD changed"):
            upgrade.release_upgrade(
                plane(fixture, store, operator),
                "frame",
                "prepare-config",
                request=request,
                **kwargs,
            )
        assert not store.published
    elif change == "runtime_after":
        real = operator._fence

        def moves_after_publication(head, **kw):
            if store.published:
                raise ValueError("runtime HEAD changed")
            return real(head, **kw)

        operator.authority.fresh_runtime_head_fence = moves_after_publication
        with pytest.raises(ValueError, match="authority changed before acceptance"):
            upgrade.release_upgrade(
                plane(fixture, store, operator),
                "frame",
                "prepare-config",
                request=request,
                **kwargs,
            )
    elif change == "epoch":
        request["epoch"] += 1
        with pytest.raises(ValueError, match="CAS mismatch"):
            upgrade.release_upgrade(
                plane(fixture, store, operator), "frame", "prepare-config", request=request
            )
    else:
        # Same admitted digest under another release id is ambiguous, never a rollback.
        request["release"]["id"] = "forged"
        with pytest.raises(ValueError, match="CAS mismatch"):
            upgrade.release_upgrade(
                plane(fixture, store, operator), "frame", "prepare-config", request=request
            )


def test_rollback_is_not_a_rotation_plan(fixture):
    store, operator = Store(fixture.snapshot), Operator(fixture)
    with pytest.raises(ValueError, match="original CAS"):
        upgrade.release_upgrade(
            plane(fixture, store, operator),
            "frame",
            "plan",
            request=rollback_request(fixture, store),
        )


# --------------------------------------------------------------------------------------------
# bh-aprgo: runtime read capabilities over a scripted Dolt cursor
# --------------------------------------------------------------------------------------------


class ScriptedCursor:
    """Denies statements matching ``deny`` the way Dolt 2.3.5 does (1105 + message)."""

    def __init__(self, *, deny=(), grants=()):
        # ``grants``: (schema, privilege, grantable) rows of this account ("w"@"%"). Dolt
        # serves every account's rows to everyone, so a foreign admin row is always present.
        self.deny = deny
        self.grants = [("'w'@'%'", *row) for row in grants] + [
            ("'admin'@'localhost'", "*", "ALL PRIVILEGES", "YES")
        ]

    def execute(self, sql, params=None):
        self.sql = sql
        for needle in self.deny:
            if needle in sql:
                raise _DoltError(1105, "Access denied for user 'acct'@'host' to table 'x'")

    def fetchall(self):
        if "information_schema.user_privileges" in self.sql:
            return self.grants
        return []

    def fetchone(self):
        return ("w@%",) if self.sql == "SELECT CURRENT_USER()" else None

    @property
    def description(self):
        return [("type",), ("name",), ("fragment",), ("extra",), ("sql_mode",)]


class _DoltError(Exception):
    pass


def test_table_level_grants_report_the_database_wide_diff_requirement():
    cursor = ScriptedCursor(deny=("DOLT_DIFF_SUMMARY", "dolt_ignore"))
    found = capabilities.runtime_read(cursor, "beadhive_hq_runtime", "h")
    text = "; ".join(str(item) for item in found)
    assert "DOLT_DIFF_SUMMARY" in text and "dolt_ignore" in text
    assert "SELECT ON `beadhive_hq_runtime`.*" in text
    assert "acct" not in text and "host" not in text  # redacted
    assert capabilities.runtime_read(ScriptedCursor(), "beadhive_hq_runtime", "h") == []


def test_non_privilege_probe_failure_is_unknown_not_missing():
    class Broken(ScriptedCursor):
        def execute(self, sql, params=None):
            raise _DoltError(2013, "Lost connection")

    with pytest.raises(capabilities.CapabilityProbeError):
        capabilities.runtime_read(Broken(), "db", "h")


@pytest.mark.parametrize(
    ("grants", "inspect", "provision"),
    [
        ([("db", "SELECT", "NO")], 2, 6),
        ([("db", "SELECT", "NO"), ("db", "TRIGGER", "NO"), ("mysql", "SELECT", "NO")], 0, 5),
        (
            [
                ("*", privilege, "YES")
                for privilege in ("SELECT", "INSERT", "CREATE", "TRIGGER", "CREATE USER")
            ],
            0,
            0,
        ),
    ],
)
def test_session_inspection_and_provisioning_are_read_from_own_grants(grants, inspect, provision):
    cursor = ScriptedCursor(grants=grants)
    assert len(capabilities.session_inspection(cursor, "db")) == inspect
    assert len(capabilities.provisioning(cursor, "db", "cfg")) == provision
    assert capabilities.denied(_DoltError(1105, "command denied to user"))
    assert not capabilities.denied(_DoltError(1105, "table not found: x"))


def test_report_gates_actions_only_for_session_routes():
    restricted = ScriptedCursor(deny=("DOLT_DIFF_SUMMARY",), grants=[("db", "SELECT", "NO")])
    inbox = capabilities.report(
        restricted, database="db", config_database="cfg", head="h", route="inbox"
    )
    assert inbox["ready"] == {"apply": True, "provision": False} and inbox["runtime_read"]
    session = capabilities.report(
        restricted, database="db", config_database="cfg", head="h", route="session"
    )
    assert session["ready"] == {"apply": False, "provision": False}
    assert any("TRIGGER" in item for item in session["apply"])
