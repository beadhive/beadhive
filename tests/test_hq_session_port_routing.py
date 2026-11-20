"""Session renew and conformance evidence through the HQ port (bh-osom7, bh-65s71).

Route ``sql``, flag off: the direct SQL session calls, unchanged, and the port is never selected.
Route ``sql`` with ``hq.session_via_port`` on, and route ``api`` always: one ``HqPort`` call per
renew and per evidence post, on the adapter ``hq.route`` selects. On ``api`` the anchor is the
port's own (``read_frame_anchor``) and no SQL plane is read. A port failure is a failed attempt and
never falls back to the SQL path. Nothing sleeps or retries.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from beadhive import heartbeat_report as report
from beadhive import heartbeat_sender, hq_service_adapter, hq_sql_session
from beadhive.hq_framelease_contracts import HeartbeatError
from beadhive.hq_port import (
    EvidenceReport,
    FrameAnchor,
    HqDeadline,
    HqNoCurrentBeat,
    HqOutcomeUnknown,
    HqRefused,
    HqRouteError,
    HqUnavailable,
    SessionRoute,
)

BINDING = SimpleNamespace(
    principal="factory_p",
    frame_id="factory",
    holder_identity="host-a",
    instance_ref="instance-a",
    epoch=2,
    inbox_table="inbox_t",
)
PLANE = SimpleNamespace(
    settings={"runtime": {"operation_timeout": 5}},
    _runtime_authority=lambda: SimpleNamespace(load_frame_binding=lambda **_: BINDING),
)


#: The anchor the api route reads from the port: the same route as :data:`BINDING`.
ANCHOR = FrameAnchor(
    principal="factory_p",
    frame="factory",
    epoch=2,
    holder="host-a",
    instance="instance-a",
    carrier="inbox_t",
)
#: The api route's port deadline from the mocked clock (``API_FRAME_TIMEOUT_S`` = 30 s).
API_DEADLINE = 130.0


class FakePort:
    def __init__(self, *, renew=None, evidence=None):
        self.calls = []
        self.anchors = 0
        self._renew, self._evidence = renew, evidence

    def read_frame_anchor(self, *, expected_head, deadline):
        self.anchors += 1
        return ANCHOR

    def renew_session(self, anchor, *, deadline):
        self.calls.append(("renew", anchor, deadline))
        if isinstance(self._renew, BaseException):
            raise self._renew
        return self._renew or SessionRoute(anchor.principal, anchor.epoch, outcome="accepted")

    def publish_evidence(self, anchor, report_, *, deadline):
        self.calls.append(("evidence", anchor, report_, deadline))
        if isinstance(self._evidence, BaseException):
            raise self._evidence
        return SessionRoute(anchor.principal, anchor.epoch, outcome="accepted")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(report.time, "monotonic", lambda: 100.0)
    monkeypatch.delenv(report.SESSION_VIA_PORT_ENV, raising=False)


def _host(monkeypatch, *, flag=None, route=None):
    hq = {}
    if flag is not None:
        hq["session_via_port"] = flag
    if route is not None:
        hq["route"] = route
    monkeypatch.setattr(report.config, "load_host", lambda: {"hq": hq})


_SELECT = [None]


def _port(monkeypatch, port):
    """Install *port* as what the sender's selector returns; returns the selector's calls."""
    selected = []

    def select():
        selected.append(report.config.load_host())
        return port

    _SELECT[0] = select
    monkeypatch.setattr(heartbeat_sender, "_select_port", select)
    return selected


def _sql(monkeypatch, plane=PLANE):
    """The direct SQL path, recorded."""
    calls = []
    monkeypatch.setattr(report, "_sql_plane", lambda: plane)
    monkeypatch.setattr(report, "_session_opener", lambda p: (lambda: "opener", "db"))
    monkeypatch.setattr(hq_sql_session, "resolve_route", lambda *a, **k: calls.append("route"))
    monkeypatch.setattr(
        hq_sql_session, "publish_evidence", lambda *a, **k: calls.append("sql-evidence") or "row"
    )
    return calls


# ---- the flag --------------------------------------------------------------------------------


def test_flag_defaults_off(monkeypatch):
    _host(monkeypatch)
    assert report.session_via_port() is False
    _host(monkeypatch, flag=False)
    assert report.session_via_port() is False


def test_flag_reads_the_host_setting_and_the_env_overrides_it(monkeypatch):
    _host(monkeypatch, flag=True)
    assert report.session_via_port() is True
    monkeypatch.setenv(report.SESSION_VIA_PORT_ENV, "0")
    assert report.session_via_port() is False
    _host(monkeypatch, flag=False)
    monkeypatch.setenv(report.SESSION_VIA_PORT_ENV, "true")
    assert report.session_via_port() is True


def test_an_unreadable_host_config_leaves_the_flag_off(monkeypatch):
    def boom():
        raise OSError("no host config")

    monkeypatch.setattr(report.config, "load_host", boom)
    assert report.session_via_port() is False


# ---- flag off: the direct SQL path, unchanged -------------------------------------------------


def test_flag_off_renewer_is_the_sql_session_renewer_and_never_selects_a_port(monkeypatch):
    _host(monkeypatch, flag=False, route="sql")
    selected = _port(monkeypatch, FakePort())
    _sql(monkeypatch)
    renewer = report.session_renewer(_SELECT[0])
    assert type(renewer) is hq_sql_session.SessionRenewer
    assert selected == []


def test_flag_off_without_a_frame_runtime_has_no_renewer(monkeypatch):
    _host(monkeypatch, flag=False)
    monkeypatch.setattr(report, "_sql_plane", lambda: None)
    assert report.session_renewer(_SELECT[0]) is None
    assert report.publish_session_evidence(_SELECT[0]) is None


def test_flag_off_evidence_is_the_direct_sql_update(monkeypatch):
    _host(monkeypatch, flag=False, route="sql")
    selected = _port(monkeypatch, FakePort())
    calls = _sql(monkeypatch)
    sentinel = object()
    monkeypatch.setattr(report, "session_evidence", lambda plane, route: sentinel)
    monkeypatch.setattr(
        hq_sql_session, "publish_evidence", lambda c, r, **k: (calls.append(r), "row")[1]
    )

    class Conn:
        def cursor(self):
            return self

        __enter__ = lambda self: self  # noqa: E731
        __exit__ = lambda self, *a: False  # noqa: E731
        rollback = close = lambda self: None  # noqa: E731

    monkeypatch.setattr(report, "_session_opener", lambda p: (Conn, "db"))
    assert report.publish_session_evidence(_SELECT[0]) == "row"
    assert calls == ["route", sentinel]
    assert selected == []


# ---- flag on: renew through the port ---------------------------------------------------------


def test_flag_on_renew_is_one_port_call_on_this_frames_anchor(monkeypatch):
    _host(monkeypatch, flag=True, route="sql")
    port = FakePort()
    selected = _port(monkeypatch, port)
    sql = _sql(monkeypatch)
    renewer = report.session_renewer(_SELECT[0])
    route = renewer.tick()
    ((kind, anchor, deadline),) = port.calls
    assert (kind, anchor.principal, anchor.frame, anchor.epoch) == (
        "renew",
        "factory_p",
        "factory",
        2,
    )
    assert (anchor.holder, anchor.instance, anchor.carrier) == ("host-a", "instance-a", "inbox_t")
    assert deadline == 105.0  # the runtime operation timeout from the mocked clock
    assert route.session == hq_sql_session.session_table("factory_p", 2)
    assert renewer.renewals == 1
    assert selected == [{"hq": {"session_via_port": True, "route": "sql"}}]
    assert sql == []  # no SQL session call


@pytest.mark.parametrize(
    "failure",
    [
        HqUnavailable("down"),
        HqDeadline("late"),
        HqOutcomeUnknown(None),
        HqRefused("seq_not_advanced"),
    ],
)
def test_flag_on_renew_failure_is_a_failed_tick_never_success_or_sql_fallback(monkeypatch, failure):
    _host(monkeypatch, route="api")
    port = FakePort(renew=failure)
    _port(monkeypatch, port)
    sql = _sql(monkeypatch)
    renewer = report.session_renewer(_SELECT[0])
    with pytest.raises(type(failure)):
        renewer.tick()
    assert renewer.renewals == 0 and renewer.route is None and sql == []
    # the anchor is re-resolved after a failed tick
    with pytest.raises(type(failure)):
        renewer.tick()
    assert len(port.calls) == 2 and port.anchors == 2


@pytest.mark.parametrize("flag", [None, False, True])
def test_api_renew_is_one_port_call_on_the_ports_anchor_with_no_sql_plane(monkeypatch, flag):
    """bh-65s71: an api host has no SQL frame runtime; ``hq.session_via_port`` is ignored."""
    _host(monkeypatch, flag=flag, route="api")
    port = FakePort()
    selected = _port(monkeypatch, port)

    def no_sql_plane():
        raise AssertionError("route=api read the SQL plane")

    monkeypatch.setattr(report, "_sql_plane", no_sql_plane)
    route = report.session_renewer(_SELECT[0]).tick()
    ((kind, anchor, deadline),) = port.calls
    assert (kind, anchor, deadline) == ("renew", ANCHOR, API_DEADLINE)
    assert route.session == hq_sql_session.session_table("factory_p", 2)
    assert port.anchors == 1 and len(selected) == 1


def test_an_unreadable_api_anchor_is_a_failed_tick_that_sends_nothing(monkeypatch):
    _host(monkeypatch, route="api")
    port = FakePort()
    port.read_frame_anchor = lambda **_: (_ for _ in ()).throw(HqUnavailable("no grant"))
    _port(monkeypatch, port)
    renewer = report.session_renewer(_SELECT[0])
    with pytest.raises(HqUnavailable):
        renewer.tick()
    assert port.calls == []


def test_flag_on_sql_route_without_a_frame_runtime_is_the_same_skip(monkeypatch):
    _host(monkeypatch, flag=True, route="sql")
    _port(monkeypatch, FakePort())
    monkeypatch.setattr(report, "_sql_plane", lambda: None)
    assert report.session_renewer(_SELECT[0]) is None
    assert report.publish_session_evidence(_SELECT[0]) is None


def test_flag_on_without_a_port_selector_is_refused(monkeypatch):
    _host(monkeypatch, flag=True, route="api")
    monkeypatch.setattr(report, "_sql_plane", lambda: PLANE)
    with pytest.raises(HqRouteError):
        report.session_renewer()
    with pytest.raises(HqRouteError):
        report.publish_session_evidence()


def test_sender_selector_is_select_hq_port_over_the_host_config(monkeypatch):
    _host(monkeypatch, flag=True, route="api")
    chosen = []
    monkeypatch.setattr(
        hq_service_adapter, "select_hq_port", lambda cfg: chosen.append(cfg) or "port"
    )
    assert heartbeat_sender._select_port() == "port"
    assert chosen == [{"hq": {"session_via_port": True, "route": "api"}}]


def test_flag_on_with_an_invalid_route_is_refused_not_defaulted(monkeypatch):
    _host(monkeypatch, flag=True, route="bogus")
    monkeypatch.setattr(report, "_sql_plane", lambda: PLANE)
    with pytest.raises(HqRouteError):
        report.session_renewer(_SELECT[0])


def test_flag_on_a_legacy_incarnation_stays_a_skip(monkeypatch):
    _host(monkeypatch, flag=True, route="sql")
    refusal = HqRefused("runtime_refused")
    refusal.__cause__ = hq_sql_session.NotSwitched("legacy")
    _port(monkeypatch, FakePort(renew=refusal))
    monkeypatch.setattr(report, "_sql_plane", lambda: PLANE)
    with pytest.raises(hq_sql_session.NotSwitched):
        report.session_renewer(_SELECT[0]).tick()


# ---- the sender over the port ----------------------------------------------------------------


def test_sender_one_shot_prints_the_renewed_session_over_the_port(monkeypatch, capsys):
    _host(monkeypatch, route="api")
    _port(monkeypatch, FakePort())
    assert heartbeat_sender.main(["renew"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "renewed": hq_sql_session.session_table("factory_p", 2)
    }


def test_sender_one_shot_reports_a_port_failure_as_refused(monkeypatch, capsys):
    _host(monkeypatch, route="api")
    _port(monkeypatch, FakePort(renew=HqUnavailable("down")))
    assert heartbeat_sender.main(["renew"]) == 1
    assert "session renewal refused" in capsys.readouterr().err


def test_sender_loop_reports_each_failed_port_renewal_by_class(monkeypatch, capsys):
    _host(monkeypatch, route="api")
    port = FakePort(renew=HqOutcomeUnknown(None))
    _port(monkeypatch, port)
    ticks = iter([False, True])
    monkeypatch.setattr("threading.Event.wait", lambda self, timeout=None: next(ticks))
    assert heartbeat_sender._renew(loop=True, interval=60.0) == 0
    assert capsys.readouterr().err.count("session renewal failed (HqOutcomeUnknown); retrying") == 2
    assert len(port.calls) == 2


# ---- flag on: conformance evidence through the port -------------------------------------------


def _lease(report_dict):
    checks = report_dict["checks"]
    return SimpleNamespace(
        release=SimpleNamespace(
            model_dump=lambda: {"id": "0.21.3", "digest": "sha256:" + "a" * 64}
        ),
        conformance=SimpleNamespace(
            profile="factory-v1",
            status="conformant",
            checks=[SimpleNamespace(model_dump=lambda c=c: c) for c in checks],
        ),
        free_sessions=report_dict["free_sessions"],
        report_digest=report.report_digest(report_dict),
    )


REPORT = {
    "method": report.REPORT_METHOD,
    "checks": [{"id": "installed-release", "status": "pass", "evidence": "ok"}],
    "free_sessions": 0,
}


def test_flag_on_api_posts_the_exact_report_the_beat_digested(monkeypatch):
    _host(monkeypatch, flag=True, route="api")
    port = FakePort()
    _port(monkeypatch, port)
    sql = _sql(monkeypatch)
    lease = _lease(REPORT)
    monkeypatch.setattr(report, "generate", lambda plane, **kw: lease)
    result = report.publish_session_evidence(_SELECT[0])
    ((_, anchor, posted, deadline),) = port.calls
    assert result.outcome == "accepted" and anchor == ANCHOR and deadline == API_DEADLINE
    assert dict(posted.payload) == REPORT
    assert report.report_digest(dict(posted.payload)) == lease.report_digest
    assert sql == []


def test_flag_on_sql_route_posts_the_evidence_row_fields(monkeypatch):
    _host(monkeypatch, flag=True, route="sql")
    monkeypatch.setattr(report, "_sql_plane", lambda: PLANE)
    port = FakePort()
    _port(monkeypatch, port)
    lease = _lease(REPORT)
    monkeypatch.setattr(report, "generate", lambda plane, **kw: lease)
    report.publish_session_evidence(_SELECT[0])
    ((_, _, posted, _),) = port.calls
    assert dict(posted.payload) == {
        "epoch": 2,
        "release_id": "0.21.3",
        "release_digest": "sha256:" + "a" * 64,
        "profile": "factory-v1",
        "status": "conformant",
        "report_digest": lease.report_digest,
    }


def test_flag_on_sql_route_without_a_beat_writes_an_unavailable_row(monkeypatch):
    _host(monkeypatch, flag=True, route="sql")
    monkeypatch.setattr(report, "_sql_plane", lambda: PLANE)
    port = FakePort()
    _port(monkeypatch, port)

    def refuse(plane, **kw):
        raise HeartbeatError("release drift")

    monkeypatch.setattr(report, "generate", refuse)
    monkeypatch.setattr(
        report, "installed_release", lambda: {"id": "9", "digest": "sha256:" + "b" * 64}
    )
    report.publish_session_evidence(_SELECT[0])
    ((_, _, posted, _),) = port.calls
    assert dict(posted.payload)["status"] == "unavailable"
    assert dict(posted.payload)["release_id"] == "9"


def test_flag_on_api_without_a_beat_has_nothing_to_post(monkeypatch):
    _host(monkeypatch, flag=True, route="api")
    port = FakePort()
    _port(monkeypatch, port)

    def refuse(plane, **kw):
        raise HeartbeatError("release drift")

    monkeypatch.setattr(report, "generate", refuse)
    with pytest.raises(HqUnavailable):
        report.publish_session_evidence(_SELECT[0])
    assert port.calls == []


@pytest.mark.parametrize(
    "failure",
    [
        HqNoCurrentBeat("none"),
        HqRefused("report_digest_mismatch"),
        HqUnavailable("down"),
        HqDeadline("late"),
        HqOutcomeUnknown(None),
    ],
)
def test_flag_on_evidence_failure_is_raised_typed_and_not_retried_or_rerouted(monkeypatch, failure):
    _host(monkeypatch, flag=True, route="api")
    port = FakePort(evidence=failure)
    _port(monkeypatch, port)
    sql = _sql(monkeypatch)
    monkeypatch.setattr(report, "generate", lambda plane, **kw: _lease(REPORT))
    with pytest.raises(type(failure)):
        report.publish_session_evidence(_SELECT[0])
    assert len(port.calls) == 1 and sql == []


def test_sender_conformance_reports_a_refused_evidence_post(monkeypatch, capsys):
    _host(monkeypatch, route="api")
    _port(monkeypatch, FakePort(evidence=HqNoCurrentBeat("none")))
    monkeypatch.setattr(report, "generate", lambda plane, **kw: _lease(REPORT))
    monkeypatch.setattr(report, "refresh_conformance", lambda: object())
    monkeypatch.setattr(heartbeat_sender, "status", lambda: {})
    assert heartbeat_sender.main(["conformance"]) == 1
    assert "session evidence refused" in capsys.readouterr().err


def test_evidence_report_type_is_the_ports():
    assert EvidenceReport({"a": 1}).payload == {"a": 1}
