"""``bh hq placement`` (bh-16347.5): the hidden operator/director verb over the bh-a94qw library.

The director is a stub here (its SQL is covered by ``test_hq_sql_placement.py`` and, against a
throwaway Dolt server, ``test_director_failover_int.py``); these tests pin the operator shape:
dry run unless ``--confirm``, refuse and never clamp, seed renders provisioning SQL only, JSON
out, exit 1 on refusal, hidden from help.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from beadhive import hq_operator_settings, hq_placement_ops
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_sql_placement import (
    PLACEMENT_TABLE,
    PlacementError,
    PlacementRecord,
    Survey,
    lease_body,
    parse_row,
    placement_witness,
)

NOW = float(int(time.time()))
REV = "a" * 64


def _record(prefix="ah", *, host="host-a", epoch=4, revision=REV, released=False):
    if released:
        lease = HostLease("", "", epoch, now_stamp(NOW), now_stamp(NOW))
    else:
        lease = HostLease(host, "frame-a", epoch, now_stamp(NOW), now_stamp(NOW + 3600))
    return PlacementRecord(
        prefix=prefix,
        revision=revision,
        authority={} if released else {"frame_id": "frame-a", "holder_identity": host},
        lease=lease,
        request_id="r",
        request_sha256="w",
        director=True,
    )


class StubDirector:
    def __init__(self, record=None, *, placeable=True):
        self.record, self._placeable = record, placeable
        self.calls = []

    def read(self, prefix):
        self.calls.append(("read", prefix))
        return self.record

    def survey(self, observe=None):
        self.calls.append(("survey",))
        placements = {self.record.prefix: self.record} if self.record else {}
        return Survey({"frames": {}}, {}, placements, {})

    def placeable(self, state, policies, prefix, frame):
        if not self._placeable:
            raise PlacementError(f"PLACEMENT: frame {frame} has no active, uncordoned grant")
        return {"frame_id": frame}

    def place(self, prefix, **kwargs):
        self.calls.append(("place", prefix, kwargs))
        return _record(prefix, epoch=kwargs.get("epoch") or 5, revision="b" * 64)

    def release(self, prefix, **kwargs):
        self.calls.append(("release", prefix, kwargs))
        return _record(prefix, released=True, revision="c" * 64)


# =============================================================================================
# The operations
# =============================================================================================


def test_show_reports_the_row_or_unseeded():
    assert hq_placement_ops.show(StubDirector(), "ah") == {"prefix": "ah", "seeded": False}
    view = hq_placement_ops.show(StubDirector(_record()), "ah")
    assert view["seeded"] and view["epoch"] == 4 and view["frame_id"] == "frame-a"
    assert view["revision"] == REV and not view["released"] and view["source"] == "director"


def test_seed_renders_a_provisioning_insert_that_parses_back():
    out = hq_placement_ops.seed("ah", epoch=21, at=NOW)
    assert out["executed"] is False and "provisioning" in out["run_as"]
    statement = out["statement"]
    assert statement.startswith(f"INSERT INTO {PLACEMENT_TABLE} ") and statement.endswith(");")
    values = statement.split("VALUES (", 1)[1].rstrip(");").split(",")
    prefix, revision, body, request_id, witness = (v.strip("'") for v in values)
    body = bytes.fromhex(body.removeprefix("X'"))
    assert prefix == "ah" and witness == placement_witness("ah", revision, body)
    row = parse_row("ah", (revision, body, request_id, witness))
    assert row.director and row.lease.is_tombstone and row.lease.epoch == 21


def test_seed_needs_an_explicit_epoch_and_refuses_a_seeded_hive():
    with pytest.raises(PlacementError, match="--epoch"):
        hq_placement_ops.seed("ah", epoch=None)
    with pytest.raises(PlacementError, match="positive integer"):
        hq_placement_ops.seed("ah", epoch=0)
    with pytest.raises(PlacementError, match="already seeded"):
        hq_placement_ops.seed("ah", epoch=3, director=StubDirector(_record()))
    assert hq_placement_ops.seed("ah", epoch=3, director=StubDirector())["epoch"] == 3


def test_place_is_a_dry_run_unless_confirmed():
    director = StubDirector(_record(epoch=4))
    out = hq_placement_ops.place(director, "ah", frame="frame-b", expected=REV)
    assert out == {
        "dry_run": True,
        "current": hq_placement_ops.record_view(director.record),
        "would_place": {"prefix": "ah", "frame_id": "frame-b", "epoch": 5, "tenure_s": 86400.0},
    }
    assert not [c for c in director.calls if c[0] == "place"]
    out = hq_placement_ops.place(
        director, "ah", frame="frame-b", expected=REV, epoch=9, tenure_s=600, confirm=True
    )
    assert out["dry_run"] is False and out["placed"]["epoch"] == 9
    assert director.calls[-1] == (
        "place",
        "ah",
        {"frame_id": "frame-b", "expected_revision": REV, "epoch": 9, "tenure_s": 600},
    )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"frame": ""}, "requires --frame"),
        ({"expected": ""}, "--expected-revision is required"),
        ({"expected": "b" * 64}, "placement moved"),
        ({"epoch": 4}, "must raise the epoch"),
        ({"epoch": 0}, "positive integer"),
        ({"tenure_s": 86401}, "--tenure must be within"),
        ({"tenure_s": 0}, "--tenure must be within"),
    ],
)
def test_place_preview_refuses_what_the_cas_would_and_never_clamps(kwargs, match):
    args = {"frame": "frame-b", "expected": REV, **kwargs}
    with pytest.raises(PlacementError, match=match):
        hq_placement_ops.place(StubDirector(_record(epoch=4)), "ah", **args)


def test_place_preview_refuses_an_unplaceable_frame_and_an_unseeded_hive():
    with pytest.raises(PlacementError, match="PLACEMENT: frame frame-b"):
        hq_placement_ops.place(
            StubDirector(_record(), placeable=False), "ah", frame="frame-b", expected=REV
        )
    with pytest.raises(PlacementError, match="seed it first"):
        hq_placement_ops.place(StubDirector(), "ah", frame="frame-b", expected=REV)


def test_release_is_a_dry_run_unless_confirmed_and_refuses_a_released_hive():
    director = StubDirector(_record(epoch=6))
    out = hq_placement_ops.release(director, "ah", expected=REV)
    assert out["dry_run"] and out["would_release"] == {"prefix": "ah", "epoch": 6}
    out = hq_placement_ops.release(director, "ah", expected=REV, confirm=True)
    assert out["released"]["released"] and director.calls[-1][0] == "release"
    with pytest.raises(PlacementError, match="already released"):
        hq_placement_ops.release(StubDirector(_record(released=True)), "ah", expected=REV)


class GrantCursor:
    """``mysql.user``, the principal registry, SHOW GRANTS and triggers, in memory."""

    def __init__(self, grants, registry, triggers=()):
        self.grants, self.registry, self.triggers = grants, registry, list(triggers)
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("SELECT user,host FROM mysql.user"):
            (user,) = params
            self.result = [
                tuple(a.strip("'").split("'@'")) for a in self.grants if f"'{user}'@" in a
            ]
        elif sql.startswith("SELECT principal,epoch,inbox_table"):
            self.result = list(self.registry)
        elif sql.startswith("SHOW GRANTS FOR "):
            self.result = [(line,) for line in self.grants[sql.removeprefix("SHOW GRANTS FOR ")]]
        elif "information_schema.triggers" in sql:
            self.result = list(self.triggers)
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return list(self.result)


def _conn(cursor):
    return SimpleNamespace(
        cursor=lambda: cursor, rollback=lambda: None, close=lambda: None, closed=False
    )


DB = "beadhive_hq_runtime"
SETTINGS = {
    "placement_writer": {"user": "director", "database": DB},
    "authority_writer": {"user": "operator", "database": DB},
}


def test_check_covers_the_director_and_every_registry_frame():
    grants = {
        "'director'@'%'": [
            "GRANT USAGE ON *.* TO `director`@`%`",
            f"GRANT SELECT, UPDATE ON `{DB}`.`hq_live_hive_leases` TO `director`@`%`",
        ],
        "'frame_a'@'10.0.0.5'": [
            f"GRANT SELECT ON `{DB}`.`hq_live_hive_leases` TO `frame_a`@`10.0.0.5`",
            f"GRANT SELECT, INSERT ON `{DB}`.`hq_live_inbox_frame_a_1` TO `frame_a`@`10.0.0.5`",
            f"GRANT UPDATE ON `{DB}`.`frame_frame_a_1_session` TO `frame_a`@`10.0.0.5`",
        ],
    }
    registry = [("frame_a", 1, "hq_live_inbox_frame_a_1")]
    seen = []
    out = hq_placement_ops.check(
        SETTINGS,
        connect=lambda binding: (
            seen.append(binding["user"]) or _conn(GrantCursor(grants, registry))
        ),
    )
    assert seen == ["operator"]  # the operator account reads other accounts' grants
    assert out == {
        "conformant": True,
        "director_accounts": ["'director'@'%'"],
        "frame_accounts": ["'frame_a'@'10.0.0.5'"],
        "problems": [],
    }
    grants["'frame_a'@'10.0.0.5'"].append(
        f"GRANT UPDATE ON `{DB}`.`hq_live_hive_leases` TO `frame_a`@`10.0.0.5`"
    )
    out = hq_placement_ops.check(
        SETTINGS, connect=lambda binding: _conn(GrantCursor(grants, registry))
    )
    assert not out["conformant"]
    assert out["problems"] == [
        "'frame_a'@'10.0.0.5': frame holds ['UPDATE'] on beadhive_hq_runtime.hq_live_hive_leases"
    ]


def test_check_refuses_without_a_director_binding_and_hides_driver_text():
    with pytest.raises(PlacementError, match="placement_writer"):
        hq_placement_ops.check({"authority_writer": {"user": "operator"}})

    class Broken(GrantCursor):
        def execute(self, sql, params=None):
            raise RuntimeError("access denied for password=hunter2")

    with pytest.raises(PlacementError, match="unavailable") as excinfo:
        hq_placement_ops.check(SETTINGS, connect=lambda b: _conn(Broken({}, [])))
    assert "hunter2" not in str(excinfo.value)


# =============================================================================================
# The CLI
# =============================================================================================


@pytest.fixture
def run(monkeypatch):
    from beadhive.cli import app

    monkeypatch.delenv(hq_operator_settings.ENV, raising=False)
    state = {"director": StubDirector(_record()), "paths": []}

    def placement_director(path=None, **_):
        state["paths"].append(path)
        return state["director"]

    monkeypatch.setattr(hq_operator_settings, "placement_director", placement_director)

    def invoke(*args):
        return CliRunner().invoke(app, ["hq", "placement", *args])

    invoke.state = state
    return invoke


def test_cli_is_hidden_and_documented_by_its_runbook(run):
    from beadhive.cli import app

    listing = CliRunner().invoke(app, ["hq", "--help"]).output
    assert "placement" not in listing and "authority" in listing
    assert "hq-placement-runbook" in run("--help").output


def test_cli_show_and_place_print_json_and_route_the_settings_path(run):
    result = run("show", "ah", "--operator-settings", "/etc/bh/director.yaml")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["epoch"] == 4
    assert run.state["paths"] == ["/etc/bh/director.yaml"]
    result = run("place", "ah", "--frame", "frame-b", "--expected-revision", REV, "--tenure", "12h")
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["dry_run"] and out["would_place"]["tenure_s"] == 43200.0
    result = run("place", "ah", "--frame", "frame-b", "--expected-revision", REV, "--confirm")
    assert result.exit_code == 0 and json.loads(result.output)["placed"]["epoch"] == 5


def test_cli_seed_without_settings_renders_sql_only(run):
    result = run("seed", "ah", "--epoch", "7")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["statement"].startswith("INSERT INTO hq_live_hive_leases")
    assert run.state["paths"] == []  # no settings: nothing to read, no connection


@pytest.mark.parametrize(
    ("args", "match"),
    [
        (["frob", "ah"], "unknown placement action"),
        (["show"], "requires a hive PREFIX"),
        (["check", "ah"], "check takes no PREFIX"),
        (["show", "ah", "--frame", "f"], "--frame applies to place only"),
        (["show", "ah", "--confirm"], "--confirm applies to place, release and policy only"),
        (["show", "ah", "--failover-after", "executor=60m"], "place and policy only"),
        (["policy", "ah", "--executor-floor", "45m"], "without a PREFIX"),
        (["policy", "--confirm"], "needs --failover-after or --executor-floor"),
        (["policy", "--failover-after", "viewer=60m"], "unknown failover setting"),
        (["policy", "--executor-floor", "-1"], "invalid duration"),
        (["release", "ah", "--epoch", "3"], "--epoch applies to seed and place only"),
        (["check"], "check needs the operator settings file"),
        (
            ["place", "ah", "--frame", "frame-b", "--expected-revision", REV, "--epoch", "2"],
            "raise",
        ),
        (["place", "ah", "--frame", "f", "--expected-revision", REV, "--tenure", "2d"], "tenure"),
    ],
)
def test_cli_refuses_with_exit_1(run, args, match):
    result = run(*args)
    assert result.exit_code == 1
    assert "placement refused:" in result.output and match in result.output


def test_cli_check_exits_1_on_findings(run, monkeypatch, tmp_path):
    path = tmp_path / "director.json"
    path.write_text("{}")
    monkeypatch.setattr(hq_operator_settings, "load_placement_settings", lambda p: SETTINGS)
    monkeypatch.setattr(
        hq_placement_ops, "check", lambda s: {"conformant": False, "problems": ["x"]}
    )
    result = run("check", "--operator-settings", str(path))
    assert result.exit_code == 1 and json.loads(result.output)["problems"] == ["x"]
    monkeypatch.setattr(hq_placement_ops, "check", lambda s: {"conformant": True, "problems": []})
    monkeypatch.setenv(hq_operator_settings.ENV, str(path))
    assert run("check").exit_code == 0


def test_seed_statement_body_is_the_receiver_carrier():
    out = hq_placement_ops.seed("bh", epoch=2, at=NOW)
    body_hex = out["statement"].split("X'", 1)[1].split("'", 1)[0]
    lease = HostLease("", "", 2, now_stamp(NOW), now_stamp(NOW))
    assert bytes.fromhex(body_hex) == lease_body({}, lease)


def test_check_lets_an_m9_frame_update_its_own_evidence_row_only():
    """bh-owqdg: the conformance job UPDATEs ``frame_<p>_<e>_evidence``; another frame's is not."""
    grants = {
        "'director'@'%'": [
            f"GRANT SELECT, UPDATE ON `{DB}`.`hq_live_hive_leases` TO `director`@`%`",
        ],
        "'frame_a'@'10.0.0.5'": [
            f"GRANT SELECT, UPDATE ON `{DB}`.`frame_frame_a_1_session` TO `frame_a`@`10.0.0.5`",
            f"GRANT SELECT, UPDATE ON `{DB}`.`frame_frame_a_1_evidence` TO `frame_a`@`10.0.0.5`",
        ],
    }
    # A session-only incarnation: the registry names its session table (no inbox).
    registry = [("frame_a", 1, "frame_frame_a_1_session")]
    out = hq_placement_ops.check(
        SETTINGS, connect=lambda binding: _conn(GrantCursor(grants, registry))
    )
    assert out["conformant"], out["problems"]
    grants["'frame_a'@'10.0.0.5'"].append(
        f"GRANT UPDATE ON `{DB}`.`frame_frame_b_1_evidence` TO `frame_a`@`10.0.0.5`"
    )
    out = hq_placement_ops.check(
        SETTINGS, connect=lambda binding: _conn(GrantCursor(grants, registry))
    )
    assert out["problems"] == [
        "'frame_a'@'10.0.0.5': frame holds ['UPDATE'] on "
        "beadhive_hq_runtime.frame_frame_b_1_evidence"
    ]


def test_observer_session_table_is_the_provisioned_name():
    from beadhive import failover_observer, hq_sql_runtime_schema

    assert failover_observer.session_table("frame_a", 3) == (
        hq_sql_runtime_schema.session_table("frame_a", 3)
    )
    with pytest.raises(ValueError):
        failover_observer.session_table("Frame-A", 3)


# =============================================================================================
# Failover policy (bh-4biq8): place --failover-after and the policy action
# =============================================================================================


class PolicyDirector(StubDirector):
    """A stub director over the real policy validation (a fake policy cursor)."""

    def __init__(self, record=None, rows=(), *, provisioned=True):
        super().__init__(record)
        from test_failover_policy import Cursor

        self.cursor = Cursor(rows, tables=True if provisioned else ())
        self.settings = {"placement_writer": {"user": "director", "database": "hq"}}

    def failover_policy(self, scope=None, changes=None):
        from beadhive import failover_policy as fp

        self.calls.append(("failover_policy", scope, changes))
        if changes:
            return fp.plan_changes(self.cursor, scope or fp.FLEET_SCOPE, changes)
        return fp.read_policy(self.cursor)

    def set_failover_policy(self, scope, changes):
        from beadhive import failover_policy as fp

        self.calls.append(("set_failover_policy", scope, changes))
        return fp.apply_changes(self.cursor, scope, changes)

    def place(self, prefix, **kwargs):
        from beadhive import failover_policy as fp

        if kwargs.get("failover_after"):
            fp.apply_changes(self.cursor, prefix, kwargs["failover_after"])
        return super().place(prefix, **kwargs)


def test_place_sets_failover_after_in_its_cas_and_previews_it_on_a_dry_run(run):
    director = run.state["director"] = PolicyDirector(_record())
    result = run(
        "place", "ah", "--frame", "frame-b", "--expected-revision", REV,
        "--failover-after", "executor=75m,transient=40m",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["dry_run"] and out["would_set_failover_after"]["effective"]["executor"] == 4500
    assert director.cursor.rows == {}  # a dry run writes nothing
    result = run(
        "place", "ah", "--frame", "frame-b", "--expected-revision", REV,
        "--failover-after", "executor=75m", "--confirm",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["failover_after"]["effective"]["executor"] == 4500
    place = next(c for c in director.calls if c[0] == "place")
    assert place[2]["failover_after"] == {"executor": 4500}


def test_place_refuses_a_failover_after_below_the_floor_and_writes_nothing(run):
    director = run.state["director"] = PolicyDirector(_record())
    for confirm in ((), ("--confirm",)):
        result = run(
            "place", "ah", "--frame", "frame-b", "--expected-revision", REV,
            "--failover-after", "executor=30m", *confirm,
        )  # fmt: skip
        assert result.exit_code == 1
        assert "below the executor floor 2700 s (45 min)" in result.output
    assert director.cursor.rows == {}


def test_policy_shows_sets_and_refuses_the_fleet_floor(run):
    director = run.state["director"] = PolicyDirector()
    shown = json.loads(run("policy").output)
    assert shown["scope"] == "*" and shown["policy"]["executor_floor_s"] == 2700
    assert shown["policy"]["effective"] == {"executor": 3600.0, "transient": 1800.0, "viewer": None}
    preview = json.loads(run("policy", "--executor-floor", "30m").output)
    assert preview["dry_run"] and preview["policy"]["executor_floor_s"] == 1800
    assert "35.4 min" in preview["policy"]["warnings"][0]
    assert director.cursor.rows == {}
    result = run("policy", "--executor-floor", "40m", "--confirm")
    assert result.exit_code == 0, result.output
    assert director.cursor.rows == {("*", "executor_floor"): 2400}
    refused = run("policy", "--executor-floor", "10m", "--confirm")
    assert refused.exit_code == 1 and "hard bound 900 s (15 min)" in refused.output
    hive = json.loads(run("policy", "ah", "--failover-after", "transient=20m", "--confirm").output)
    assert hive["policy"]["effective"]["transient"] == 1200
    assert director.cursor.rows[("ah", "transient")] == 1200


def test_policy_on_an_unprovisioned_hq_renders_the_ddl_and_grant(run):
    run.state["director"] = PolicyDirector(provisioned=False)
    out = json.loads(run("policy").output)
    assert out["policy"]["provisioned"] is False
    statements = out["provision"]["statements"]
    assert statements[0].startswith("CREATE TABLE hq_live_failover_policy")
    assert statements[1].startswith("GRANT SELECT, INSERT, UPDATE, DELETE ON hq.")
    refused = run("policy", "ah", "--failover-after", "executor=70m", "--confirm")
    assert refused.exit_code == 1 and "not provisioned" in refused.output
