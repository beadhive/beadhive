"""Cutover ordering of renew and evidence on the port path (bh-a3369).

Evidence is never posted before an accepted renew; on ``no_current_beat`` the sender renews and
posts once more; the post matches the beat the service accepted; the direct SQL writes are a
separate default-off dual-write flag. Fake ports and a mocked clock: nothing sleeps or retries
beyond the one bounded retry under test.
"""

from __future__ import annotations

import json
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import heartbeat_report as report
from beadhive import heartbeat_sender, hq_service_adapter, hq_sql_session
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_port import (
    FrameAnchor,
    HqNoCurrentBeat,
    HqRefused,
    HqUnavailable,
    SessionRoute,
)
from beadhive.hq_renew_seq import RenewSeqStore
from beadhive.hq_sql_signatures import fingerprint

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


def _report(tag):
    return {
        "method": report.REPORT_METHOD,
        "checks": [{"id": "installed-release", "status": "pass", "evidence": tag}],
        "free_sessions": 0,
    }


def _lease(report_dict):
    return SimpleNamespace(
        release=SimpleNamespace(
            model_dump=lambda: {"id": "0.21.3", "digest": "sha256:" + "a" * 64}
        ),
        conformance=SimpleNamespace(
            profile="factory-v1",
            status="conformant",
            checks=[SimpleNamespace(model_dump=lambda c=c: c) for c in report_dict["checks"]],
        ),
        free_sessions=report_dict["free_sessions"],
        report_digest=report.report_digest(report_dict),
    )


class Port:
    """Records every call in order; renew results and evidence verdicts are scripted."""

    def __init__(self, *, beats=(), evidence=()):
        self.calls = []
        self._beats, self._evidence = list(beats), list(evidence)

    def renew_session(self, anchor, *, deadline):
        self.calls.append("renew")
        step = self._beats.pop(0) if self._beats else None
        if isinstance(step, BaseException):
            raise step
        return SessionRoute(anchor.principal, anchor.epoch, outcome="accepted", beat=step)

    def publish_evidence(self, anchor, report_, *, deadline):
        self.calls.append(("evidence", dict(report_.payload)))
        step = self._evidence.pop(0) if self._evidence else None
        if isinstance(step, BaseException):
            raise step
        return SessionRoute(anchor.principal, anchor.epoch, outcome="accepted")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(report.time, "monotonic", lambda: 100.0)  # mocked clock: no sleeping
    monkeypatch.delenv(report.SESSION_VIA_PORT_ENV, raising=False)
    monkeypatch.delenv(report.SESSION_DUAL_WRITE_ENV, raising=False)
    report._ACCEPTED.clear()
    yield
    report._ACCEPTED.clear()


def _host(monkeypatch, *, route="api", dual=None):
    hq = {"session_via_port": True, "route": route}
    if dual is not None:
        hq["session_dual_write"] = dual
    monkeypatch.setattr(report.config, "load_host", lambda: {"hq": hq})


def _wire(monkeypatch, port, *, plane=PLANE):
    monkeypatch.setattr(report, "_sql_plane", lambda: plane)
    monkeypatch.setattr(heartbeat_sender, "_select_port", lambda: port)
    monkeypatch.setattr(report, "generate", lambda plane, **kw: _lease(_report("cache")))


def _publish():
    heartbeat_sender._publish_evidence()


def _names(port):
    return [c if isinstance(c, str) else c[0] for c in port.calls]


# ---- ordering --------------------------------------------------------------------------------


def test_the_first_evidence_is_preceded_by_an_accepted_renew(monkeypatch):
    _host(monkeypatch)
    port = Port()
    _wire(monkeypatch, port)
    _publish()
    assert _names(port) == ["renew", "evidence"]


def test_a_renew_already_accepted_in_this_process_is_not_repeated_per_evidence(monkeypatch):
    _host(monkeypatch)
    port = Port()
    _wire(monkeypatch, port)
    _publish()
    _publish()
    assert _names(port) == ["renew", "evidence", "evidence"]


def test_a_timer_renew_tick_counts_as_the_accepted_renew(monkeypatch):
    _host(monkeypatch)
    port = Port()
    _wire(monkeypatch, port)
    report.session_renewer(heartbeat_sender._select_port).tick()
    _publish()
    assert _names(port) == ["renew", "evidence"]


@pytest.mark.parametrize("failure", [HqUnavailable("down"), HqRefused("seq_not_advanced")])
def test_a_failed_renew_posts_no_evidence_and_is_reported(monkeypatch, capsys, failure):
    _host(monkeypatch)
    port = Port(beats=[failure])
    _wire(monkeypatch, port)
    monkeypatch.setattr(report, "refresh_conformance", lambda: object())
    monkeypatch.setattr(heartbeat_sender, "status", lambda: {})
    assert heartbeat_sender.main(["conformance"]) == 1
    assert "session evidence refused" in capsys.readouterr().err
    assert _names(port) == ["renew"]
    # nothing was recorded as accepted: the next post renews again
    port2 = Port()
    _wire(monkeypatch, port2)
    _publish()
    assert _names(port2) == ["renew", "evidence"]


def test_api_host_without_a_frame_runtime_sends_nothing(monkeypatch):
    _host(monkeypatch)
    port = Port()
    _wire(monkeypatch, port, plane=None)
    with pytest.raises(HqUnavailable):
        _publish()
    assert port.calls == []


# ---- no_current_beat: renew once, post once more ---------------------------------------------


def test_no_current_beat_renews_then_posts_exactly_once_more(monkeypatch):
    _host(monkeypatch)
    port = Port(evidence=[HqNoCurrentBeat("none")])
    _wire(monkeypatch, port)
    _publish()
    assert _names(port) == ["renew", "evidence", "renew", "evidence"]


def test_a_second_no_current_beat_is_raised_with_no_further_attempt(monkeypatch):
    _host(monkeypatch)
    port = Port(evidence=[HqNoCurrentBeat("a"), HqNoCurrentBeat("b")])
    _wire(monkeypatch, port)
    with pytest.raises(HqNoCurrentBeat):
        _publish()
    assert _names(port) == ["renew", "evidence", "renew", "evidence"]


def test_a_failed_retry_renew_posts_nothing_more(monkeypatch):
    _host(monkeypatch)
    port = Port(beats=[None, HqUnavailable("down")], evidence=[HqNoCurrentBeat("a")])
    _wire(monkeypatch, port)
    with pytest.raises(HqUnavailable):
        _publish()
    assert _names(port) == ["renew", "evidence", "renew"]
    assert report._ACCEPTED == {}


def test_other_evidence_refusals_are_not_retried(monkeypatch):
    _host(monkeypatch)
    port = Port(evidence=[HqRefused("evidence_digest_unmatched")])
    _wire(monkeypatch, port)
    with pytest.raises(HqRefused):
        _publish()
    assert _names(port) == ["renew", "evidence"]


# ---- digest coherence ------------------------------------------------------------------------


def test_the_post_is_the_report_the_accepted_beat_digested_when_the_cache_changes(monkeypatch):
    _host(monkeypatch)
    held = _lease(_report("beat-time"))
    port = Port(beats=[held])
    _wire(monkeypatch, port)
    cache = {"now": _lease(_report("beat-time"))}
    monkeypatch.setattr(report, "generate", lambda plane, **kw: cache["now"])
    renewer = report.session_renewer(heartbeat_sender._select_port)
    renewer.tick()
    cache["now"] = _lease(_report("refreshed-after-conformance"))  # the cache changed
    report.publish_session_evidence(heartbeat_sender._select_port, renew_first=True)
    ((_, posted),) = [c for c in port.calls if not isinstance(c, str)]
    assert posted == _report("beat-time")
    assert report.report_digest(posted) == held.report_digest


def test_a_retry_renew_posts_the_newly_accepted_beats_report(monkeypatch):
    _host(monkeypatch)
    first, second = _lease(_report("one")), _lease(_report("two"))
    port = Port(beats=[first, second], evidence=[HqNoCurrentBeat("lapsed")])
    _wire(monkeypatch, port)
    _publish()
    posted = [c[1] for c in port.calls if not isinstance(c, str)]
    assert posted == [_report("one"), _report("two")]


# ---- the adapter hands back the beat it actually sent ----------------------------------------

CORPUS = Path(__file__).parent / "fixtures/hq_service_contract/session_v1"
ANCHOR = FrameAnchor(
    principal="frame_principal_a", frame="frame-a", epoch=3, holder="host-a", instance="inst-host-a"
)


def test_a_resent_pending_beat_is_what_the_route_carries_not_the_current_cache(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(hq_service_adapter, "time", types.SimpleNamespace(monotonic=lambda: 1000.0))
    private = Ed25519PrivateKey.generate()
    key = tmp_path / "id_ed25519"
    key.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    key.chmod(0o600)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    spec = json.loads(
        json.loads((CORPUS / "renew-accepted-first-beat.json").read_text())["request"]["body"]
    )["spec"]
    spec.pop("signature")
    spec["key_id"] = fingerprint(public)
    now = {"t": 1_700_000_000.0}
    reads = []

    def beat_source(anchor):
        reads.append(1)
        lease = dict(spec, seq=1 + len(reads))
        lease["renewTime"] = datetime.fromtimestamp(now["t"], UTC).isoformat()
        digest = "sha256:" + format(len(reads), "x") * 64  # the cache changes on every read
        return HeartbeatLease.model_validate({**lease, "report_digest": digest})

    answers = [
        httpx.ConnectError("lost"),
        httpx.Response(200, json={"outcome": "replayed", "revision": "r", "accepted_until": 9e9}),
    ]

    def handler(request):
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    adapter = hq_service_adapter.HqServiceAdapter(
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://hq"),
        beat_source=beat_source,
        signing_key=str(key),
        seq_store=lambda: RenewSeqStore(tmp_path / "state"),
        clock=lambda: now["t"],
    )
    with pytest.raises(Exception):  # noqa: B017 - the first send is lost
        adapter.renew_session(ANCHOR, deadline=1030.0)
    now["t"] += 1  # still fresh: the same bytes are resent
    route = adapter.renew_session(ANCHOR, deadline=1030.0)
    assert len(reads) == 1  # the beat source was not asked again
    assert route.outcome == "replayed"
    assert route.beat.report_digest == "sha256:" + "1" * 64  # the held beat, not a newer one


# ---- dual-write ------------------------------------------------------------------------------


def _direct(monkeypatch):
    calls = []
    monkeypatch.setattr(report, "_session_opener", lambda p: (lambda: "opener", "db"))
    monkeypatch.setattr(
        hq_sql_session.SessionRenewer, "tick", lambda self: calls.append("sql-renew")
    )
    monkeypatch.setattr(report, "_direct_evidence", lambda plane: calls.append("sql-evidence"))
    return calls


def test_dual_write_defaults_off_and_follows_host_config_and_env(monkeypatch):
    monkeypatch.setattr(report.config, "load_host", lambda: {"hq": {}})
    assert report.session_dual_write() is False
    _host(monkeypatch, dual=True)
    assert report.session_dual_write() is True
    monkeypatch.setenv(report.SESSION_DUAL_WRITE_ENV, "0")
    assert report.session_dual_write() is False

    def boom():
        raise OSError("no host config")

    monkeypatch.delenv(report.SESSION_DUAL_WRITE_ENV)
    monkeypatch.setattr(report.config, "load_host", boom)
    assert report.session_dual_write() is False


def test_dual_write_off_never_touches_the_direct_sql_writes(monkeypatch):
    _host(monkeypatch)
    port = Port()
    _wire(monkeypatch, port)
    sql = _direct(monkeypatch)
    report.session_renewer(heartbeat_sender._select_port).tick()
    _publish()
    assert sql == []


def test_dual_write_on_writes_direct_after_the_port_accepted(monkeypatch):
    _host(monkeypatch, dual=True)
    port = Port()
    _wire(monkeypatch, port)
    sql = _direct(monkeypatch)
    report.session_renewer(heartbeat_sender._select_port).tick()
    _publish()
    assert sql == ["sql-renew", "sql-evidence"]


def test_dual_write_never_runs_when_the_port_write_failed(monkeypatch):
    _host(monkeypatch, dual=True)
    port = Port(beats=[HqUnavailable("down"), HqUnavailable("down")])
    _wire(monkeypatch, port)
    sql = _direct(monkeypatch)
    with pytest.raises(HqUnavailable):
        report.session_renewer(heartbeat_sender._select_port).tick()
    with pytest.raises(HqUnavailable):
        _publish()
    assert sql == []


def test_dual_write_is_skipped_on_the_sql_route_where_the_port_is_the_write(monkeypatch):
    _host(monkeypatch, route="sql", dual=True)
    port = Port()
    _wire(monkeypatch, port)
    sql = _direct(monkeypatch)
    report.session_renewer(heartbeat_sender._select_port).tick()
    _publish()
    assert sql == []


def test_flag_off_sender_passes_no_ordering_argument(monkeypatch):
    monkeypatch.setattr(report.config, "load_host", lambda: {"hq": {}})
    seen = []
    monkeypatch.setattr(report, "publish_session_evidence", lambda *a, **k: seen.append((a, k)))
    _publish()
    assert seen == [((), {})]
