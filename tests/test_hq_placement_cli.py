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
        (["show", "ah", "--confirm"], "--confirm applies to place and release only"),
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
