"""Per-role, per-hive ``failover_after`` and the executor floor in HQ data (bh-4biq8, M8c).

ADR §4 with the operator's follow-up decision: executor 60 min, transient 30 min, viewer never
placed; overrides per role and per hive in HQ data, never host.yaml or fleet config; a
configurable executor floor (45 min) beside them; every value validated on load against the
``bh-cvk70`` E20 hard bounds and REFUSED, never clamped. SQL is faked here;
``test_hq_sql_placement_int.py`` runs the table, grants and the in-CAS write against a scratch
Dolt server.
"""

from __future__ import annotations

import fnmatch
import re
from types import SimpleNamespace

import pytest

from beadhive import failover_policy as fp
from beadhive.director_failover import SqlFailoverPorts, build_loop
from beadhive.failover_observer import FailoverDirector, FailoverMonitor
from beadhive.hq_sql_placement import PlacementError, Survey, check_grants
from beadhive.kernel.daemon.contracts.config import DaemonFailoverConfig

DOLT = fp.Bounds(session_ttl_s=300)  # the dolt-server shape: 5 min session TTL, sync 0


def _load(*rows, bounds=DOLT):
    return fp.load_policy(rows, bounds)


def _reasons(policy):
    return [r.describe() for r in policy.refusals]


# =============================================================================================
# Defaults and the per-hive, per-role lookup
# =============================================================================================


def test_defaults_executor_60_transient_30_viewer_never_floor_45():
    policy = _load()
    assert policy.failover_after("ah", "executor") == 3600.0
    assert policy.failover_after("ah", "transient") == 1800.0
    assert policy.failover_after("ah", "viewer") is None
    assert policy.failover_after("ah", "some-new-role") == 3600.0  # the longest, never early
    assert policy.executor_floor_s == fp.DEFAULT_EXECUTOR_FLOOR_S == 2700
    assert policy.refusals == () and policy.warnings == ()
    assert fp.effective(policy) == {"executor": 3600.0, "transient": 1800.0, "viewer": None}


def test_overrides_are_per_hive_and_per_role_with_fleet_defaults_under_them():
    policy = _load(
        ("ah", "executor", 4500),
        ("bh", "transient", 2400),
        ("*", "transient", 2000),
        ("*", "executor", 5400),
    )
    assert policy.failover_after("ah", "executor") == 4500.0
    assert policy.failover_after("ah", "transient") == 2000.0  # the fleet row
    assert policy.failover_after("bh", "transient") == 2400.0
    assert policy.failover_after("bh", "executor") == 5400.0  # the fleet row
    assert policy.failover_after("zz", "viewer") is None
    assert policy.refusals == ()


# =============================================================================================
# The executor floor: at, above and below; refused, never clamped
# =============================================================================================


@pytest.mark.parametrize(("seconds", "accepted"), [(2700, True), (2701, True), (2699, False)])
def test_executor_override_at_above_and_below_the_floor(seconds, accepted):
    policy = _load(("ah", "executor", seconds))
    if accepted:
        assert policy.failover_after("ah", "executor") == float(seconds)
        assert policy.refusals == ()
    else:
        (refusal,) = policy.refusals
        assert refusal.seconds == seconds  # reported as given: never clamped
        assert "below the executor floor 2700 s (45 min)" in refusal.reason
        assert policy.failover_after("ah", "executor") == 3600.0  # the default applies


def test_the_floor_is_configurable_and_governs_executor_overrides_only():
    policy = _load(
        ("*", "executor_floor", 3000), ("ah", "executor", 2900), ("ah", "transient", 900)
    )
    assert policy.executor_floor_s == 3000
    assert _reasons(policy) == [
        "hive ah executor=2900 refused: below the executor floor 3000 s (50 min)"
    ]
    assert policy.failover_after("ah", "transient") == 900.0  # transient ignores the floor


def test_a_floor_below_35_4_min_warns_with_the_measured_stretch():
    policy = _load(("*", "executor_floor", 1800), ("ah", "executor", 1800))
    assert policy.executor_floor_s == 1800 and policy.refusals == ()
    assert policy.failover_after("ah", "executor") == 1800.0
    (warning,) = policy.warnings
    assert "2124 s (35.4 min) false-stale stretch" in warning
    assert _load(("*", "executor_floor", 2124)).warnings == ()  # at the stretch: no warning


def test_warning_band_is_hard_bound_up_to_the_stretch():
    # dolt-server: the hard bound is max(15 min, 2 x 5 min) = 900 s.
    at_bound = _load(("*", "executor_floor", 900))
    assert at_bound.executor_floor_s == 900 and at_bound.warnings and not at_bound.refusals
    below = _load(("*", "executor_floor", 899))
    assert below.executor_floor_s == 2700  # refused: the default floor applies
    assert "bd lease TTL + reclaim grace hard bound 900 s (15 min)" in _reasons(below)[0]


def test_a_floor_below_the_session_hard_bound_is_refused():
    git_like = fp.Bounds(session_ttl_s=600, sync_interval_s=300)  # 2 x (10 + 5) min = 30 min
    policy = _load(("*", "executor_floor", 1700), bounds=git_like)
    assert policy.executor_floor_s == 2700
    assert "2 x (session TTL + sync interval) hard bound 1800 s (30 min)" in _reasons(policy)[0]


def test_a_floor_above_the_executor_default_is_refused():
    policy = _load(("*", "executor_floor", 3700))
    assert policy.executor_floor_s == 2700
    assert "above the executor failover_after default 3600 s" in _reasons(policy)[0]
    raised = _load(("*", "executor_floor", 3700), ("*", "executor", 4000))
    assert raised.executor_floor_s == 3700 and raised.refusals == ()


# =============================================================================================
# The hard bounds
# =============================================================================================


def test_below_the_bd_reclaim_hard_bound_is_refused():
    policy = _load(("ah", "transient", 899), ("bh", "transient", 900))
    assert _reasons(policy) == [
        "hive ah transient=899 refused: below the bd lease TTL + reclaim grace hard bound "
        "900 s (15 min)"
    ]
    assert policy.failover_after("ah", "transient") == 1800.0
    assert policy.failover_after("bh", "transient") == 900.0


def test_below_the_session_hard_bound_is_refused():
    bounds = fp.Bounds(session_ttl_s=600)  # 2 x 10 min = 20 min
    policy = _load(("ah", "transient", 1199), ("bh", "transient", 1200), bounds=bounds)
    (refusal,) = policy.refusals
    assert refusal.scope == "ah" and "hard bound 1200 s (20 min)" in refusal.reason
    assert policy.failover_after("bh", "transient") == 1200.0


def test_a_code_default_under_a_hard_bound_is_reported_not_silently_used():
    policy = _load(bounds=fp.Bounds(session_ttl_s=1000))  # 2 x 1000 s > transient's 1800 s
    assert any("code-default transient" in w for w in policy.warnings)


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (("ah", "viewer", 3600), "a viewer is never placed"),
        (("ah", "executor_floor", 3000), "fleet-wide"),
        (("AH;", "executor", 3600), "neither a hive prefix"),
        (("ah", "dispatcher", 3600), "unknown setting"),
        (("ah", "executor", -5), "positive whole number"),
        (("ah", "executor", 3600.5), "positive whole number"),
        (("ah", "executor", True), "positive whole number"),
        (("ah", "executor"), "row shape"),
    ],
)
def test_malformed_rows_are_refused_by_name(row, reason):
    policy = _load(row)
    (refusal,) = policy.refusals
    assert reason in refusal.reason
    assert policy.failover_after("ah", "executor") == 3600.0


# =============================================================================================
# Not a host.yaml or fleet-config key; no config head move; no authority unbind
# =============================================================================================


def test_no_host_yaml_or_fleet_config_key_exists_for_these_values():
    from beadhive.config_schema import BeadhiveConfig

    keys: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            keys.update((node.get("properties") or {}).keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(BeadhiveConfig.model_json_schema())
    assert "operator_settings" in keys  # the walk sees the daemon's failover section
    for key in keys:
        assert not re.search(r"failover_after|executor_floor|failover_policy", key), key
    assert set(DaemonFailoverConfig.model_fields) == {
        "enabled",
        "interval_seconds",
        "max_primary_spread",
        "operator_settings",
    }


def test_the_policy_is_uncommitted_hq_data_beside_placement():
    from beadhive.hq_sql_placement import PROTECTED_TABLES
    from beadhive.hq_sql_runtime_schema import COMMITTED_SCHEMA

    # Under the runtime database's committed `hq_live_*` ignore rule: never a Dolt commit.
    assert "INSERT INTO dolt_ignore VALUES ('hq_live_*', TRUE)" in COMMITTED_SCHEMA
    assert fnmatch.fnmatch(fp.POLICY_TABLE, "hq_live_*")
    assert fp.POLICY_TABLE in PROTECTED_TABLES  # frames hold no write right on it


class Cursor:
    """A fake DB-API cursor over the two tables the policy reads, recording every statement."""

    def __init__(self, rows=(), *, tables=True, ttl=300):
        self.rows = {(s, k): v for s, k, v in rows}
        self.tables = (
            {fp.POLICY_TABLE, "hq_liveness_policy"} if tables is True else set(tables or ())
        )
        self.ttl, self.statements, self.result = ttl, [], []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if "information_schema.tables" in sql:
            self.result = [(t,) for t in sorted(self.tables) if t in params]
        elif sql.startswith("SELECT session_ttl_s"):
            self.result = [(self.ttl, 900)]
        elif sql.startswith("SELECT scope,setting,seconds"):
            self.result = [(s, k, v) for (s, k), v in sorted(self.rows.items())]
        elif sql.startswith("INSERT INTO hq_live_failover_policy"):
            self.rows[(params[0], params[1])] = params[2]
        elif sql.startswith("DELETE FROM hq_live_failover_policy"):
            self.rows.pop(params, None)
        else:
            raise AssertionError(sql)
        return 1

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0] if self.result else None


def test_changing_the_policy_never_commits_or_touches_authority_or_config():
    cursor = Cursor()
    after = fp.apply_changes(cursor, "ah", {"executor": 4500, "transient": 2400})
    assert after.failover_after("ah", "executor") == 4500.0
    assert cursor.rows == {("ah", "executor"): 4500, ("ah", "transient"): 2400}
    fp.apply_changes(cursor, fp.FLEET_SCOPE, {"executor_floor": 3000})
    fp.apply_changes(cursor, "ah", {"transient": None})
    assert cursor.rows == {("ah", "executor"): 4500, ("*", "executor_floor"): 3000}
    for sql in cursor.statements:
        assert not re.search(r"DOLT_COMMIT|DOLT_ADD|hq_authority|config", sql, re.IGNORECASE), sql


# =============================================================================================
# Writes: validated exactly as on load, refused (nothing written), never clamped
# =============================================================================================


def test_a_refused_change_writes_nothing():
    cursor = Cursor([("ah", "executor", 4500)])
    with pytest.raises(fp.FailoverPolicyError, match="below the executor floor"):
        fp.apply_changes(cursor, "bh", {"executor": 2000})
    with pytest.raises(fp.FailoverPolicyError, match="hard bound"):
        fp.apply_changes(cursor, fp.FLEET_SCOPE, {"executor_floor": 600})
    # Raising the floor above an existing override would newly refuse it: refused.
    with pytest.raises(fp.FailoverPolicyError, match="hive ah executor=4500"):
        fp.apply_changes(cursor, fp.FLEET_SCOPE, {"executor_floor": 4600, "executor": 5000})
    with pytest.raises(fp.FailoverPolicyError, match="fleet-wide"):
        fp.apply_changes(cursor, "ah", {"executor_floor": 3000})
    assert cursor.rows == {("ah", "executor"): 4500}
    assert not any(s.startswith(("INSERT", "DELETE")) for s in cursor.statements)


def test_plan_changes_previews_without_writing_and_unprovisioned_is_refused():
    cursor = Cursor()
    preview = fp.plan_changes(cursor, "ah", {"executor": 4000})
    assert preview.failover_after("ah", "executor") == 4000.0 and cursor.rows == {}
    with pytest.raises(fp.FailoverPolicyError, match="not provisioned"):
        fp.apply_changes(Cursor(tables=()), "ah", {"executor": 4000})


def test_read_policy_uses_the_live_session_ttl_and_tolerates_absent_tables():
    policy = fp.read_policy(Cursor([("ah", "transient", 1000)], ttl=600))
    assert policy.bounds.session_bound_s == 1200
    assert "hard bound 1200 s" in _reasons(policy)[0]
    bare = fp.read_policy(Cursor(tables=()))
    assert not bare.provisioned and bare.failover_after("ah", "executor") == 3600.0
    assert bare.bounds.session_ttl_s == 300  # the default session TTL


def test_parse_changes_takes_durations_and_refuses_the_rest():
    assert fp.parse_changes("executor=75m, transient=2400") == {
        "executor": 4500,
        "transient": 2400,
    }
    assert fp.parse_changes("executor=default") == {"executor": None}
    floor = fp.parse_changes("executor_floor=45m", allowed=(fp.FLOOR_SETTING,))
    assert floor == {"executor_floor": 2700}
    for bad, match in (
        ("executor", "ROLE=DURATION"),
        ("viewer=60m", "unknown failover setting"),
        ("executor_floor=45m", "unknown failover setting"),
        ("executor=60m,executor=70m", "given twice"),
        ("executor=-5", "invalid duration"),
        ("executor=1.5", "whole seconds"),
        ("", "no failover setting"),
    ):
        with pytest.raises(fp.FailoverPolicyError, match=match):
            fp.parse_changes(bad)


def test_policy_errors_are_value_errors_the_cli_reports():
    assert issubclass(fp.FailoverPolicyError, ValueError)
    assert issubclass(PlacementError, ValueError)


# =============================================================================================
# Grants: the director may write the policy table; frames may not
# =============================================================================================

DB = "beadhive_hq_runtime"


def test_the_director_may_write_the_policy_table_and_nothing_else_new():
    base = [f"GRANT SELECT, UPDATE ON `{DB}`.`hq_live_hive_leases` TO `director`@`%`"]
    policy = f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{DB}`.`{fp.POLICY_TABLE}` TO `director`@`%`"
    assert check_grants([*base, policy], role="director", database=DB) == []
    for extra in (
        f"GRANT INSERT ON `{DB}`.`hq_live_hive_leases` TO `director`@`%`",
        f"GRANT DROP ON `{DB}`.`{fp.POLICY_TABLE}` TO `director`@`%`",
        f"GRANT INSERT ON `other`.`{fp.POLICY_TABLE}` TO `director`@`%`",
    ):
        assert check_grants([*base, policy, extra], role="director", database=DB), extra
    frame = f"GRANT SELECT, UPDATE ON `{DB}`.`{fp.POLICY_TABLE}` TO `frame_a`@`%`"
    assert check_grants([frame], role="frame", database=DB, writable=[fp.POLICY_TABLE])


def test_provision_statements_render_the_table_and_the_directors_grant():
    statements = fp.provision_statements({"placement_writer": {"user": "director", "database": DB}})
    assert statements[0] == fp.FAILOVER_POLICY_SCHEMA[0]
    assert statements[1] == (
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB}.{fp.POLICY_TABLE} TO 'director'@'<host>'"
    )
    assert fp.provision_statements({}) == fp.FAILOVER_POLICY_SCHEMA


# =============================================================================================
# The director loop reads it per hive and role
# =============================================================================================


def _row(frame, revision):
    return SimpleNamespace(frame_id=frame, revision=revision)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_director_fails_each_hive_over_at_its_own_failover_after():
    clock, stale = Clock(), {"frame-a": 0.0}
    policy = _load(("ah", "executor", 2700), ("bh", "executor", 5400))
    placed = []
    director = FailoverDirector(
        hq_mode="dolt-server",
        monitor=FailoverMonitor(lambda key: policy.failover_after(key[0], "executor"), clock=clock),
        staleness=lambda: dict(stale),
        placements=lambda: {"ah": _row("frame-a", "r1"), "bh": _row("frame-a", "r2")},
        successor=lambda prefix, dead: "frame-b",
        place=lambda prefix, frame, expected: placed.append((prefix, clock.now)),
    )
    for _ in range(120):
        clock.now += 60
        stale["frame-a"] += 60
        director.tick()
    first = {}
    for prefix, at in placed:
        first.setdefault(prefix, at)
    assert list(first) == ["ah", "bh"]
    assert 2700 < first["ah"] <= 2700 + 120  # the hive's 45 min, not the fixed 60
    assert 5400 < first["bh"] <= 5400 + 120


class StubDirector:
    def __init__(self, policy, observed, placements):
        self.policy, self.observed, self.placements = policy, observed, placements
        self.placed = []

    def survey(self, observe=None):
        state = {
            "frames": {
                f: {
                    "active": {
                        "authority": {"holder_identity": f, "config_revision": "c"},
                        "state": "active",
                    }
                }
                for f in self.observed
            }
        }
        policies = {p: {"config_revision": "c"} for p in self.placements}
        return Survey(state, policies, self.placements, {}, dict(self.observed), self.policy)

    @staticmethod
    def placeable(state, policies, prefix, frame):
        from beadhive.hq_sql_placement import SqlPlacementDirector

        return SqlPlacementDirector.placeable(state, policies, prefix, frame)

    def place(self, prefix, *, frame_id, expected_revision, cause=None):
        assert cause == "failover"  # the loop's placements carry the failover cause
        self.placed.append((prefix, frame_id))
        return SimpleNamespace(prefix=prefix, frame_id=frame_id)


def test_the_loop_reads_the_policy_from_the_survey_and_logs_refusals_once(caplog):
    policy = _load(("ah", "executor", 2000), ("bh", "executor", 2800))  # ah refused: floor
    director = StubDirector(
        policy,
        {"frame-a": 2900.0, "frame-b": 1.0},
        {"ah": _row("frame-a", "r1"), "bh": _row("frame-a", "r2")},
    )
    ports = SqlFailoverPorts(director)
    with caplog.at_level("WARNING"):
        ports.staleness()
        ports.staleness()
    refused = [r for r in caplog.records if "refused" in r.getMessage()]
    assert len(refused) == 1 and "below the executor floor" in refused[0].getMessage()
    assert ports.after(("ah", "frame-a")) == 3600.0  # refused: the default, never 2000
    assert ports.after(("bh", "frame-a")) == 2800.0
    ports.role_for = lambda frame: "transient"
    assert ports.after(("bh", "frame-a")) == 1800.0
    ports.role_for = lambda frame: "viewer"
    assert ports.after(("bh", "frame-a")) is None

    clock = Clock()
    loop = build_loop(
        DaemonFailoverConfig(enabled=True, interval_seconds=60),
        hq_mode="dolt-server",
        director=director,
        clock=clock,
    )
    results = []
    for _ in range(80):
        clock.now += 60
        director.observed["frame-a"] += 60
        results += loop.tick()
    assert [(r.prefix, r.outcome) for r in results][:1] == [("bh", "placed")]
    assert director.placed[0] == ("bh", "frame-b")


# =============================================================================================
# The placement CAS carries the policy in its own transaction
# =============================================================================================


def _cas_connection(policy_cursor):
    from test_hq_sql_placement import FakeCursor, _seeded

    connection = _seeded()

    class Both(FakeCursor):
        def execute(self, sql, params=None):
            if re.search(r"information_schema|hq_live_failover_policy|session_ttl_s", sql):
                self.c.statements.append((sql, params))
                matched = policy_cursor.execute(sql, params)
                self.result = policy_cursor.result
                return matched
            return super().execute(sql, params)

    connection.cursor = lambda: Both(connection)
    return connection


def test_place_cas_sets_the_policy_before_its_update_in_one_commit():
    from beadhive.hq_sql_placement import parse_row, place_cas
    from test_hq_sql_placement import AUTHORITY, _lease

    policy = Cursor()
    connection = _cas_connection(policy)
    place_cas(
        connection,
        prefix="ah",
        lease=_lease(epoch=2),
        authority=AUTHORITY,
        expected_revision=connection.rows["ah"][0],
        failover_after={"executor": 4500},
    )
    writes = [s.split()[:3] for s, _ in connection.statements if s.startswith(("INSERT", "UPDATE"))]
    assert writes == [
        ["INSERT", "INTO", "hq_live_failover_policy"],
        ["UPDATE", "hq_live_hive_leases", "SET"],
    ]
    assert connection.committed == 1 and policy.rows == {("ah", "executor"): 4500}
    # The placement row itself is untouched in shape: the strict five-field receiver carrier.
    assert parse_row("ah", connection.rows["ah"]).lease == _lease(epoch=2)


def test_place_cas_carries_cause_and_failover_after_in_one_transaction():
    from beadhive.hq_sql_placement import CAUSE_FAILOVER, parse_row, place_cas
    from test_hq_sql_placement import AUTHORITY, _lease

    policy = Cursor()
    connection = _cas_connection(policy)
    place_cas(
        connection,
        prefix="ah",
        lease=_lease(epoch=2),
        authority=AUTHORITY,
        expected_revision=connection.rows["ah"][0],
        cause=CAUSE_FAILOVER,
        failover_after={"transient": 2400},
    )
    assert connection.committed == 1 and policy.rows == {("ah", "transient"): 2400}
    stored = parse_row("ah", connection.rows["ah"])
    assert stored.director and stored.cause == CAUSE_FAILOVER


def test_place_cas_refuses_a_bad_policy_before_any_update():
    from beadhive.hq_sql_placement import place_cas
    from test_hq_sql_placement import AUTHORITY, _lease

    policy = Cursor()
    connection = _cas_connection(policy)
    before = list(connection.rows["ah"])
    with pytest.raises(PlacementError, match="below the executor floor"):
        place_cas(
            connection,
            prefix="ah",
            lease=_lease(epoch=2),
            authority=AUTHORITY,
            expected_revision=before[0],
            failover_after={"executor": 1000},
        )
    assert connection.rows["ah"] == before and connection.committed == 0
    assert not any(s.startswith(("UPDATE", "INSERT")) for s, _ in connection.statements)


# =============================================================================================
# bh doctor reports refusals on the host that runs the loop
# =============================================================================================


def test_doctor_reports_refusals_only_where_the_loop_runs(monkeypatch, capsys):
    from beadhive import doctor, hq_operator_settings

    assert doctor._data_failover_policy({}) is None
    assert doctor._data_failover_policy({"host": {"daemon": {"failover": {}}}}) is None
    policy = _load(("ah", "executor", 2000), ("*", "executor_floor", 1800))
    seen = []

    def placement_director(path=None, **_):
        seen.append(path)
        return SimpleNamespace(
            failover_policy=lambda: policy, survey=lambda: SimpleNamespace(state={}, placements={})
        )

    monkeypatch.setattr(hq_operator_settings, "placement_director", placement_director)
    cfg = {"host": {"daemon": {"failover": {"enabled": True, "operator_settings": "/etc/d.yaml"}}}}
    data = doctor._data_failover_policy(cfg)
    assert seen == ["/etc/d.yaml"] and data["state"] == "ok"  # 2000 >= the 1800 floor
    policy = _load(("ah", "executor", 1700), ("*", "executor_floor", 1800))
    data = doctor._data_failover_policy(cfg)
    assert data["state"] == "refused" and data["effective"]["executor"] == 3600.0
    doctor._render_failover_policy(data)
    out = capsys.readouterr().out
    assert "hive ah executor=1700 refused: below the executor floor" in out
    assert "its default applies" in out and "35.4 min" in out

    def unavailable(path=None, **_):
        raise PlacementError("verified placement writer connection unavailable")

    monkeypatch.setattr(hq_operator_settings, "placement_director", unavailable)
    assert doctor._data_failover_policy(cfg)["state"] == "unavailable"
