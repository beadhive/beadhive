"""The SQL-plane retirement guard (bh-d9anb): HQ SQL openers warn off the hq-service host."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import hq_sql_guard
from beadhive.hq_release_catalog import ReleaseCatalogStore
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from beadhive.hq_sql_guard import SqlPlaneRetired
from beadhive.hq_sql_operator import SqlRuntimeOperator
from beadhive.hq_sql_placement import SqlPlacementDirector

from beadhive.hq_control_plane import SqlControlPlane  # isort: skip


def _control_plane():
    return SqlControlPlane({"liveness": "signed"})


OPENERS = {
    "SqlControlPlane": _control_plane,
    "SqlFleetConfigRevisionStore": lambda: SqlFleetConfigRevisionStore({}),
    "SqlRuntimeOperator": lambda: SqlRuntimeOperator({}),
    "ReleaseCatalogStore": lambda: ReleaseCatalogStore({}),
    "SqlPlacementDirector": lambda: SqlPlacementDirector({}),
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for name in (hq_sql_guard.GUARD_ENV, hq_sql_guard.SERVICE_HOST_ENV, "BH_HQ_ROUTE"):
        monkeypatch.delenv(name, raising=False)
    hq_sql_guard.reset()
    yield
    hq_sql_guard.reset()


def _host(monkeypatch, **hq):
    monkeypatch.setattr(hq_sql_guard, "_host_hq", lambda: dict(hq))


@pytest.mark.parametrize("name", OPENERS)
def test_route_api_opener_warns_naming_opener_and_call_site(name, monkeypatch, capsys):
    _host(monkeypatch, route="api")
    OPENERS[name]()
    err = capsys.readouterr().err
    assert f"HQ SQL opener {name} " in err
    assert "hq.route: api" in err
    assert "called from tests.test_hq_sql_guard" in err or "called from test_hq_sql_guard" in err
    assert "a later bh release will refuse this" in err


@pytest.mark.parametrize("name", OPENERS)
def test_unmarked_sql_host_with_nothing_set_is_silent(name, monkeypatch, capsys):
    _host(monkeypatch, route="sql")
    OPENERS[name]()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("name", OPENERS)
def test_sql_host_with_explicit_warn_warns_and_service_host_does_not(name, monkeypatch, capsys):
    _host(monkeypatch, route="sql", sql_guard="warn")
    OPENERS[name]()
    assert "not marked hq.service_host" in capsys.readouterr().err
    hq_sql_guard.reset()
    _host(monkeypatch, route="sql", sql_guard="warn", service_host=True)
    OPENERS[name]()
    assert capsys.readouterr().err == ""


def test_warns_once_per_opener_and_call_site(monkeypatch, capsys):
    _host(monkeypatch, route="api")
    for _ in range(3):
        _control_plane()
    assert capsys.readouterr().err.count("HQ SQL opener SqlControlPlane") == 1
    ReleaseCatalogStore({})
    assert "ReleaseCatalogStore" in capsys.readouterr().err


def test_placement_is_marked_host_only(monkeypatch, capsys):
    _host(monkeypatch, route="sql", sql_guard="warn")
    SqlPlacementDirector({})
    assert "is hq-service-host-only" in capsys.readouterr().err


def test_service_host_on_route_api_still_warns(monkeypatch, capsys):
    _host(monkeypatch, route="api", service_host=True)
    _control_plane()
    assert "hq.route: api" in capsys.readouterr().err


def test_off_is_silent_and_refuse_raises_before_connecting(monkeypatch, capsys):
    _host(monkeypatch, route="api", sql_guard="off")
    _control_plane()
    assert capsys.readouterr().err == ""
    _host(monkeypatch, route="api", sql_guard="refuse")
    with pytest.raises(SqlPlaneRetired, match="SqlControlPlane"):
        _control_plane()
    monkeypatch.setenv(hq_sql_guard.GUARD_ENV, "off")
    _control_plane()


def test_invalid_guard_value_reads_as_warn(monkeypatch, capsys):
    _host(monkeypatch, route="api", sql_guard="bogus")
    _control_plane()
    assert "warning:" in capsys.readouterr().err


def test_env_marks_the_service_host(monkeypatch, capsys):
    _host(monkeypatch, route="sql", sql_guard="warn")
    monkeypatch.setenv(hq_sql_guard.SERVICE_HOST_ENV, "1")
    _control_plane()
    assert capsys.readouterr().err == ""


def test_unreadable_host_config_is_a_silent_sql_host(monkeypatch, capsys):
    def boom():
        raise FileNotFoundError

    real = hq_sql_guard._sibling
    monkeypatch.setattr(
        hq_sql_guard,
        "_sibling",
        lambda name: SimpleNamespace(load_host=boom) if name == "config" else real(name),
    )
    _control_plane()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("hq", "level", "fragment"),
    [
        ({"route": "api"}, "ok", "no HQ SQL configured"),
        ({"route": "api", "sql": {"enabled": True}}, "warn", "lingers"),
        ({"route": "sql", "sql": {"enabled": True}}, "warn", "cut this host over"),
        ({"route": "sql", "service_host": True, "sql": {"enabled": True}}, "ok", "hq-service host"),
    ],
)
def test_doctor_data(monkeypatch, hq, level, fragment):
    _host(monkeypatch, **hq)
    data = hq_sql_guard.doctor_data()
    assert data["level"] == level and fragment in data["detail"]


def test_doctor_renders_route_lines(monkeypatch, capsys):
    from beadhive import doctor

    _host(monkeypatch, route="sql", sql={"enabled": True})
    doctor._render_hq_route(doctor._data_hq_route())
    out = capsys.readouterr().out
    assert "# HQ route" in out
    assert "⚠ WARN hq.route: sql" in out
    assert "hq.service_host: false" in out
    assert "hq.sql: present" in out
    assert "hq.sql_guard: unset" in out


def test_refuse_only_stays_silent_unless_refusing(monkeypatch, capsys):
    _host(monkeypatch, route="sql", sql_guard="warn")
    hq_sql_guard.guard_sql_open("prune-inbox", host_only=True, refuse_only=True)
    assert capsys.readouterr().err == ""
    _host(monkeypatch, route="sql", sql_guard="refuse")
    with pytest.raises(SqlPlaneRetired, match="prune-inbox"):
        hq_sql_guard.guard_sql_open("prune-inbox", host_only=True, refuse_only=True)
