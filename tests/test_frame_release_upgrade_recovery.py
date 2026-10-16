"""Release-upgrade recovery paths found in the 0.24.3 fleet upgrade (bh-upgrade-p1 batch).

* bh-odevt: prepare-config cancels an unapplied preparation back to the admitted release.
* bh-ct91o: an explicit reviewed legacy-inbox -> session route transition.
* bh-t759y / bh-aprgo: missing inspection/read privileges are named, never masked.
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
        ([("db", "SELECT", "NO")], 2, 7),
        ([("db", "SELECT", "NO"), ("db", "TRIGGER", "NO"), ("mysql", "SELECT", "NO")], 0, 6),
        (
            [
                ("*", privilege, "YES")
                for privilege in ("SELECT", "UPDATE", "INSERT", "CREATE", "TRIGGER", "CREATE USER")
            ],
            0,
            0,
        ),
        # The binding 0.25.3 documented: ready, yet unable to delegate the frame's UPDATE.
        (
            [
                *[("db", privilege, "YES") for privilege in ("SELECT", "CREATE", "TRIGGER")],
                ("db", "INSERT", "YES"),
                ("cfg", "SELECT", "YES"),
                ("*", "CREATE USER", "NO"),
                ("mysql", "SELECT", "NO"),
            ],
            0,
            1,
        ),
        # UPDATE held but not grantable is still a gap.
        (
            [
                *[("db", privilege, "NO") for privilege in ("SELECT", "CREATE", "TRIGGER")],
                ("db", "SELECT", "YES"),
                ("db", "INSERT", "NO"),
                ("db", "UPDATE", "NO"),
                ("cfg", "SELECT", "YES"),
                ("*", "CREATE USER", "NO"),
                ("mysql", "SELECT", "NO"),
            ],
            0,
            1,
        ),
    ],
)
def test_session_inspection_and_provisioning_are_read_from_own_grants(grants, inspect, provision):
    cursor = ScriptedCursor(grants=grants)
    assert len(capabilities.session_inspection(cursor, "db")) == inspect
    assert len(capabilities.provisioning(cursor, "db", "cfg")) == provision
    assert capabilities.denied(_DoltError(1105, "command denied to user"))
    assert not capabilities.denied(_DoltError(1105, "table not found: x"))


def test_provision_denial_names_the_missing_delegation(fixture, monkeypatch):
    """A server denial mid-provision names the capability, never the generic outage."""
    import time

    from beadhive import hq_sql_session_provision
    from beadhive.hq_sql_operator import SqlOperatorError, SqlRuntimeOperator

    class Cursor(ScriptedCursor):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetchone(self):
            if "CURRENT_USER()" in self.sql and self.sql != "SELECT CURRENT_USER()":
                return "w@%", "runtime", "main", "2.3.5"
            if "DOLT_HASHOF" in self.sql:
                return ("runtime-head",)
            return super().fetchone()

    class Connection:
        def cursor(self):
            return Cursor(grants=[("runtime", "SELECT", "YES"), ("runtime", "CREATE", "NO")])

        def close(self):
            pass

    def denied_grant(*args, **kwargs):
        raise _DoltError(1105, "command denied to user 'w'@'%'")

    operator = SqlRuntimeOperator(
        {
            "authority_writer": {"user": "w", "database": "runtime"},
            "reader": {"database": "cfg"},
            "runtime_operator_public_key": fixture.public,
            "runtime_backend_identity": "runtime-backend",
            "runtime_generation": "runtime-generation",
        },
        clock=lambda: 1000,
    )
    monkeypatch.setattr(operator, "_open", lambda **kwargs: (Connection(), time.monotonic() + 30))
    monkeypatch.setattr(operator.authority, "verified_state_at", lambda *a, **k: None)
    monkeypatch.setattr(hq_sql_session_provision, "resume_incarnation", denied_grant)
    with pytest.raises(SqlOperatorError) as refused:
        operator.provision_release_route(
            ("frame_p", "frame", "holder", "instance", 4, "signer"),
            expected_revision="runtime-head",
            frame_address="127.0.0.1",
            password="secret",
        )
    message = str(refused.value)
    assert "provisioning denied" in message and "unavailable" not in message
    assert "UPDATE ON `runtime`.* WITH GRANT OPTION" in message
    assert "'w'" not in message and "secret" not in message


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


# --------------------------------------------------------------------------------------------
# bh-t759y: missing session inspection is named, never masked
# --------------------------------------------------------------------------------------------


def test_apply_on_session_route_refuses_named_missing_inspection(fixture):
    store = Store(fixture.snapshot)
    missing = {
        "route": "session",
        "runtime_read": [],
        "apply": ["incarnation trigger inspection (information_schema.triggers): TRIGGER ON x.*"],
        "provision": ["..."],
        "ready": {"apply": False, "provision": False},
        "docs": capabilities.DOCS,
    }
    operator = Operator(fixture, incumbent="session", preflight=missing)
    operator.publish = lambda *a, **k: pytest.fail("publication must not start")
    reviewed = upgrade.release_upgrade(
        plane(fixture, store, operator), "frame", "plan", request=fixture.request
    )
    with pytest.raises(ValueError, match="apply preflight.*TRIGGER.*unchanged") as refused:
        upgrade.release_upgrade(
            plane(fixture, store, operator),
            "frame",
            "apply",
            request=fixture.request,
            operator_key="key",
            confirm=True,
            plan_sha256=reviewed["plan_sha256"],
        )
    assert "publication unavailable" not in str(refused.value)


def test_publish_names_inspection_gap_instead_of_publication_unavailable(fixture, monkeypatch):
    """The production writer maps a hidden-trigger drift report to the named capability."""
    import time

    from beadhive.hq_sql_operator import SqlOperatorError, SqlRuntimeOperator

    state, digest = upgrade.prepare(
        fixture.state, "frame", "runtime-head", fixture.snapshot, fixture.request, now=1000
    )
    route = upgrade.route_for(state, "frame")
    session = session_table(route[0], route[4])
    evidence = session.replace("_session", "_evidence")

    class Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, parameters=None):
            self.sql, self.parameters = sql, parameters
            if "information_schema.triggers" in sql:
                raise _DoltError(1105, "command denied to user 'w'@'%'")

        def fetchone(self):
            if self.sql == "SELECT CURRENT_USER()":
                return ("operator@localhost",)
            if "CURRENT_USER()" in self.sql:
                return "operator@localhost", "runtime", "main", "2.3.5"
            if "DOLT_HASHOF" in self.sql:
                return ("runtime-head",)
            raise AssertionError(self.sql)

        def fetchall(self):
            if "information_schema.tables" in self.sql:
                return [(session,), (evidence,)]
            if "information_schema.user_privileges" in self.sql:
                return [("'operator'@'localhost'", "runtime", "SELECT", "NO")]
            if "dolt_status" in self.sql:
                return []
            if self.sql.startswith("DESCRIBE"):
                table = self.sql.split()[1]
                rows = [
                    ("id", "tinyint", "NO", "PRI", None, ""),
                    ("epoch", "bigint unsigned", "NO", "", None, ""),
                ]
                if table.endswith("_session"):
                    return [*rows, ("renewed_at", "datetime(6)", "YES", "", None, "")]
                return [
                    *rows,
                    *[
                        (n, k, "NO", "", None, "")
                        for n, k in (
                            ("release_id", "varchar(128)"),
                            ("release_digest", "varchar(80)"),
                            ("profile", "varchar(64)"),
                            ("status", "varchar(32)"),
                            ("report_digest", "varchar(80)"),
                        )
                    ],
                    ("measured_at", "datetime(6)", "YES", "", None, ""),
                ]
            if "check_constraints" in self.sql:
                return [("(`id` = 1)",)]
            if self.sql.startswith("SHOW INDEX"):
                return [("t", 0, "PRIMARY", 1, "id")]
            if "SELECT id,epoch" in self.sql:
                return [(1, route[4])]
            if "dolt_schemas" in self.sql:
                from beadhive.hq_sql_session import evidence_ddl, session_ddl

                name = self.parameters[0]
                ddl = session_ddl if "_session_" in name else evidence_ddl
                return [(ddl(route[0], route[4])[1],)]
            return []

    class Connection:
        commits = rollbacks = 0

        def cursor(self):
            return Cursor()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            pass

    connection = Connection()
    crossref = (
        fixture.snapshot.backend_identity,
        fixture.snapshot.generation,
        fixture.snapshot.commit_revision,
    )
    operator = SqlRuntimeOperator(
        {
            "authority_writer": {"user": "operator", "database": "runtime"},
            "runtime_operator_public_key": fixture.public,
            "runtime_backend_identity": "runtime-backend",
            "runtime_generation": "runtime-generation",
        },
        clock=lambda: 1000,
    )
    monkeypatch.setattr(operator, "_open", lambda **kwargs: (connection, time.monotonic() + 30))
    monkeypatch.setattr(
        operator.authority, "verified_state_at", lambda *a, **k: (fixture.state, crossref, {})
    )
    monkeypatch.setattr(
        operator.authority, "load_latest_config_at", lambda *a, **k: fixture.snapshot
    )
    with pytest.raises(SqlOperatorError) as refused:
        operator.publish(
            state,
            expected_revision="runtime-head",
            operator_key=str(fixture.key),
            provisioned_route=route,
            release_upgrade={"frame": "frame", "request": fixture.request, "plan_sha256": digest},
        )
    message = str(refused.value)
    assert "publication unavailable" not in message
    assert "TRIGGER ON `runtime`.*" in message and "SELECT ON `mysql`.*" in message
    assert "server-local" in message and "'w'" not in message
    assert connection.commits == 0 and connection.rollbacks == 1


# --------------------------------------------------------------------------------------------
# bh-ct91o: explicit legacy inbox -> session transition
# --------------------------------------------------------------------------------------------


def session_request(fixture):
    return {**copy.deepcopy(fixture.request), "route": "session"}


def test_legacy_inbox_plan_names_the_explicit_session_transition(fixture):
    store, operator = Store(fixture.snapshot), Operator(fixture, incumbent="inbox")
    inherited = upgrade.release_upgrade(
        plane(fixture, store, operator), "frame", "plan", request=fixture.request
    )
    assert inherited["route"] == "inbox" and inherited["route_transition"] == "none"
    assert "preflight" not in inherited
    with pytest.raises(ValueError, match="--target-route session"):
        upgrade.release_upgrade(
            plane(fixture, store, operator),
            "frame",
            "provision",
            request=fixture.request,
            confirm=True,
            plan_sha256=inherited["plan_sha256"],
            frame_address="10.0.0.2",
            password="secret",
        )
    reviewed = upgrade.release_upgrade(
        plane(fixture, store, operator), "frame", "plan", request=session_request(fixture)
    )
    assert reviewed["route"] == "session" and reviewed["route_transition"] == "inbox-to-session"
    assert reviewed["preflight"]["ready"]["provision"]
    # The transition is part of the reviewed digest: neither plan authorizes the other.
    assert reviewed["plan_sha256"] != inherited["plan_sha256"]
    # Identity and tenure-relevant authority fields are those of the inherited plan.
    assert reviewed["authority"] == inherited["authority"]


def test_session_transition_refuses_an_already_provisioned_inbox(fixture):
    store, operator = Store(fixture.snapshot), Operator(fixture, incumbent="inbox")
    state, _digest = upgrade.prepare(
        fixture.state, "frame", "runtime-head", fixture.snapshot, session_request(fixture), now=1000
    )
    principal, epoch = upgrade.route_for(state, "frame")[0], upgrade.route_for(state, "frame")[4]
    operator.provisioned = {inbox_table(principal, epoch)}
    with pytest.raises(ValueError, match="already provisioned as a inbox route"):
        upgrade.release_upgrade(
            plane(fixture, store, operator), "frame", "plan", request=session_request(fixture)
        )


def test_prepare_config_exposes_route_and_preflight_before_mutation(fixture):
    fixture.manifest["release"] = copy.deepcopy(fixture.record["desired"]["release"])
    base.replace_manifest(fixture)
    store = Store(fixture.snapshot)
    blocked = {
        "route": "session",
        "runtime_read": ["restore-floor diff (two-argument DOLT_DIFF_SUMMARY): SELECT ON x.*"],
        "apply": ["restore-floor diff"],
        "provision": ["restore-floor diff"],
        "ready": {"apply": False, "provision": False},
        "docs": capabilities.DOCS,
    }
    operator = Operator(fixture, incumbent="inbox", preflight=blocked)
    request = {**session_request(fixture), "config_head": store.snapshot.commit_revision}
    reviewed = upgrade.release_upgrade(
        plane(fixture, store, operator), "frame", "prepare-config", request=request
    )
    assert reviewed["route_transition"] == "inbox-to-session"
    assert reviewed["preflight"]["runtime_read"] and not store.published
    with pytest.raises(ValueError, match="DOLT_DIFF_SUMMARY.*canonical config is unchanged"):
        upgrade.release_upgrade(
            plane(fixture, store, operator),
            "frame",
            "prepare-config",
            request=request,
            confirm=True,
            plan_sha256=reviewed["plan_sha256"],
        )
    assert not store.published
    with pytest.raises(ValueError, match="invalid release-upgrade request"):
        upgrade.release_upgrade(
            plane(fixture, store, operator),
            "frame",
            "prepare-config",
            request={**request, "route": "inbox"},
        )


def test_session_transition_publication_refuses_a_legacy_inbox_table(fixture, monkeypatch):
    import time

    from beadhive.hq_sql_operator import SqlOperatorError, SqlRuntimeOperator

    request = session_request(fixture)
    state, digest = upgrade.prepare(
        fixture.state, "frame", "runtime-head", fixture.snapshot, request, now=1000
    )
    route = upgrade.route_for(state, "frame")
    inbox = inbox_table(route[0], route[4])

    class Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, parameters=None):
            self.sql = sql

        def fetchone(self):
            if "CURRENT_USER()" in self.sql:
                return "operator@localhost", "runtime", "main", "2.3.5"
            return ("runtime-head",)

        def fetchall(self):
            return [(inbox,)] if "information_schema.tables" in self.sql else []

    connection = SimpleNamespace(
        cursor=Cursor,
        commit=lambda: pytest.fail("no commit"),
        rollback=lambda: None,
        close=lambda: None,
    )
    crossref = (
        fixture.snapshot.backend_identity,
        fixture.snapshot.generation,
        fixture.snapshot.commit_revision,
    )
    operator = SqlRuntimeOperator(
        {
            "authority_writer": {"user": "operator", "database": "runtime"},
            "runtime_operator_public_key": fixture.public,
            "runtime_backend_identity": "runtime-backend",
            "runtime_generation": "runtime-generation",
        },
        clock=lambda: 1000,
    )
    monkeypatch.setattr(operator, "_open", lambda **kwargs: (connection, time.monotonic() + 30))
    monkeypatch.setattr(
        operator.authority, "verified_state_at", lambda *a, **k: (fixture.state, crossref, {})
    )
    monkeypatch.setattr(
        operator.authority, "load_latest_config_at", lambda *a, **k: fixture.snapshot
    )
    with pytest.raises(SqlOperatorError, match="session route transition requires"):
        operator.publish(
            state,
            expected_revision="runtime-head",
            operator_key=str(fixture.key),
            provisioned_route=route,
            release_upgrade={"frame": "frame", "request": request, "plan_sha256": digest},
        )
