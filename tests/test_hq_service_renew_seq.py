"""Client-owned renew seq (bh-1bsvh): strictly increasing across renews, whatever the SQL row.

hq-service refuses ``seq <= floor`` (``seq_not_advanced``) and its floor is not the SQL row, so
the adapter keeps ``last`` locally. Time is an injected clock; nothing sleeps or retries.
"""

from __future__ import annotations

import json
import os
import stat
import types
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import heartbeat_report, hq_renew_seq, hq_service_adapter
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_port import FrameAnchor, HqDeadline, HqRefused, HqUnavailable
from beadhive.hq_renew_seq import RenewSeqStore, SeqState
from beadhive.hq_service_adapter import HqServiceAdapter, adapter_from_config
from beadhive.hq_sql_signatures import fingerprint

CORPUS = Path(__file__).parent / "fixtures/hq_service_contract/session_v1"
ANCHOR = FrameAnchor(
    principal="frame_principal_a", frame="frame-a", epoch=3, holder="host-a", instance="inst-host-a"
)
MONO = 1000.0
T0 = 1_700_000_000.0


@pytest.fixture(autouse=True)
def fixed_monotonic(monkeypatch):
    monkeypatch.setattr(hq_service_adapter, "time", types.SimpleNamespace(monotonic=lambda: MONO))


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def key(tmp_path):
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "id_ed25519"
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return str(path), public


@pytest.fixture
def store(tmp_path):
    return RenewSeqStore(tmp_path / "state")


class Harness:
    """An adapter whose beat source plays the SQL generator: a fixed row seq and clock renewTime."""

    def __init__(self, key, store, clock, *, row_seq=1, responder=None):
        self.clock, self.row_seq, self.reads = clock, row_seq, 0
        self.sent: list[dict] = []
        self.responder = responder or self._accept
        public = key[1]
        spec = json.loads(
            json.loads((CORPUS / "renew-accepted-first-beat.json").read_text())["request"]["body"]
        )["spec"]
        spec.pop("signature")
        spec["key_id"] = fingerprint(public)
        self.spec = spec

        def beat_source(anchor):
            self.reads += 1
            lease = dict(self.spec)
            lease["seq"] = self.row_seq
            lease["renewTime"] = datetime.fromtimestamp(self.clock.now, UTC).isoformat()
            return HeartbeatLease.model_validate(lease)

        def handler(request: httpx.Request) -> httpx.Response:
            self.sent.append(json.loads(request.content))
            return self.responder(request)

        self.adapter = HqServiceAdapter(
            httpx.Client(transport=httpx.MockTransport(handler), base_url="http://hq"),
            beat_source=beat_source,
            signing_key=key[0],
            seq_store=lambda: store,
            clock=clock,
        )

    @staticmethod
    def _accept(_request):
        return httpx.Response(
            200, json={"outcome": "accepted", "revision": "r1", "accepted_until": 9e9}
        )

    def renew(self):
        return self.adapter.renew_session(ANCHOR, deadline=MONO + 30)

    @property
    def seqs(self):
        return [m["spec"]["seq"] for m in self.sent]


def _refuse(code, status=409):
    return lambda _r: httpx.Response(
        status, json={"error": {"code": code, "message": code, "retryable": False}}
    )


def _fail(_request):
    raise httpx.ConnectError("boom")


def test_two_successive_renews_advance_seq_with_no_sql_beat_between(key, store):
    h = Harness(key, store, Clock(), row_seq=1)
    store.save("frame-a", "host-a", 3, SeqState(last_seq=41, resync=False))
    h.renew()
    h.clock.now += 60
    h.renew()  # the SQL row did not move: still 1
    h.clock.now += 60
    h.renew()
    assert h.seqs == [42, 43, 44]
    assert len({m["spec"]["renewTime"] for m in h.sent}) == 3


def test_seq_takes_the_larger_of_sql_row_and_local_last(key, store):
    h = Harness(key, store, Clock(), row_seq=100)
    store.save("frame-a", "host-a", 3, SeqState(last_seq=41, resync=False))
    h.renew()
    h.row_seq = 5
    h.clock.now += 60
    h.renew()
    assert h.seqs == [100, 101]


def test_lost_state_rebases_above_the_wall_clock_then_counts_up(key, store):
    h = Harness(key, store, Clock(), row_seq=1)
    h.renew()  # no state file at all
    h.clock.now += 60
    h.renew()
    assert h.seqs == [int(T0) + 1, int(T0) + 2]


@pytest.mark.parametrize("garbage", ["", "not json", '{"v":1}', '{"v":2,"last_seq":3}', "[]"])
def test_unreadable_state_is_lost_state(garbage, key, store):
    store.path("frame-a", "host-a", 3).parent.mkdir(parents=True)
    store.path("frame-a", "host-a", 3).write_text(garbage)
    h = Harness(key, store, Clock(), row_seq=1)
    h.renew()
    assert h.seqs == [int(T0) + 1]


def test_seq_not_advanced_is_surfaced_once_and_rebases_the_next_fresh_beat(key, store):
    h = Harness(key, store, Clock(), row_seq=1, responder=_refuse("seq_not_advanced"))
    store.save("frame-a", "host-a", 3, SeqState(last_seq=7, resync=False))
    with pytest.raises(HqRefused) as raised:
        h.renew()
    assert raised.value.code == "seq_not_advanced" and h.seqs == [8]  # one send, no retry loop
    state = store.load("frame-a", "host-a", 3)
    assert state.resync and state.last_seq == 8 and state.pending is None
    h.responder = Harness._accept
    h.clock.now += 60
    h.renew()  # a refused beat is never resent: a fresh beat, rebased above the clock
    assert h.seqs == [8, int(h.clock.now) + 1]
    assert not store.load("frame-a", "host-a", 3).resync


def test_transport_failure_retry_resends_the_same_seq_and_bytes(key, store):
    h = Harness(key, store, Clock(), row_seq=1, responder=_fail)
    store.save("frame-a", "host-a", 3, SeqState(last_seq=9, resync=False))
    with pytest.raises(HqUnavailable):
        h.renew()
    h.responder = Harness._accept
    h.clock.now += 10  # still fresh: well inside half the lease
    reads = h.reads
    h.renew()
    assert h.sent[0] == h.sent[1] and h.seqs == [10, 10]
    assert h.reads == reads  # the resend needs no beat source (no SQL) read
    h.clock.now += 60
    h.renew()  # answered: the next beat is new and takes the next seq
    assert h.seqs[-1] == 11 and h.sent[-1] != h.sent[0]


def test_a_stale_pending_beat_is_not_resent(key, store):
    h = Harness(key, store, Clock(), row_seq=1, responder=_fail)
    store.save("frame-a", "host-a", 3, SeqState(last_seq=9, resync=False))
    with pytest.raises(HqUnavailable):
        h.renew()
    h.responder = Harness._accept
    h.clock.now += 3600  # past the service's freshness bound: replay would be beat_not_fresh
    h.renew()
    assert h.seqs == [10, 11]


def test_definite_refusal_clears_pending_and_keeps_the_floor_counter(key, store):
    h = Harness(key, store, Clock(), row_seq=1, responder=_refuse("beat_not_fresh"))
    store.save("frame-a", "host-a", 3, SeqState(last_seq=9, resync=False))
    with pytest.raises(HqRefused):
        h.renew()
    state = store.load("frame-a", "host-a", 3)
    assert state.pending is None and state.last_seq == 10 and not state.resync


def test_no_sql_plane_cannot_build_a_beat_but_a_pending_one_still_resends(
    key, store, monkeypatch, tmp_path
):
    """The grant-bound fields need the SQL authority (see ``_frame_beat_source``): without it a
    fresh renew is unavailable and sends nothing; a saved pending beat needs no SQL read."""
    monkeypatch.setattr(heartbeat_report, "sql_plane", lambda: None)
    calls: list = []
    adapter = HqServiceAdapter(
        httpx.Client(
            transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)),
            base_url="http://hq",
        ),
        beat_source=hq_service_adapter._frame_beat_source,
        signing_key=key[0],
        seq_store=lambda: store,
        clock=Clock(),
    )
    with pytest.raises(HqUnavailable):
        adapter.renew_session(ANCHOR, deadline=MONO + 30)
    assert calls == []
    h = Harness(key, store, Clock(), responder=_fail)
    with pytest.raises(HqUnavailable):
        h.renew()
    adapter._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (
                calls.append(r)
                or httpx.Response(
                    200, json={"outcome": "replayed", "revision": "r1", "accepted_until": 9e9}
                )
            )
        ),
        base_url="http://hq",
    )
    assert adapter.renew_session(ANCHOR, deadline=MONO + 30).outcome == "replayed"
    assert json.loads(calls[0].content) == h.sent[0]


def test_beat_source_is_not_read_once_the_deadline_is_spent(key, store):
    h = Harness(key, store, Clock())
    with pytest.raises(HqDeadline):
        h.adapter.renew_session(ANCHOR, deadline=MONO)
    assert h.reads == 0 and h.sent == []


def test_state_is_per_frame_holder_epoch(store):
    paths = {
        store.path("frame-a", "host-a", 3),
        store.path("frame-a", "host-a", 4),
        store.path("frame-a", "host-b", 3),
        store.path("frame-b", "host-a", 3),
    }
    assert len(paths) == 4


def test_state_write_is_atomic_private_and_key_free(key, store, monkeypatch):
    h = Harness(key, store, Clock(), row_seq=1)
    h.renew()
    path = store.path("frame-a", "host-a", 3)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left behind
    text = path.read_text()
    assert Path(key[0]).read_text().splitlines()[1] not in text and key[0] not in text
    # A failure while writing leaves the previous state intact and no temp file.
    before = text
    monkeypatch.setattr(os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        store.save("frame-a", "host-a", 3, SeqState(last_seq=99, resync=False))
    assert path.read_text() == before
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_default_state_lives_under_the_bh_home_heartbeat_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path / "home"))
    adapter = adapter_from_config({"hq": {"api": {"url": "https://hq.example"}}})
    store = adapter._seq_store()
    assert store._directory == tmp_path / "home" / "heartbeat"


def test_next_seq_rule():
    assert hq_renew_seq.next_seq(SeqState(5, False), 1, T0) == 6
    assert hq_renew_seq.next_seq(SeqState(5, False), 9, T0) == 9
    assert hq_renew_seq.next_seq(SeqState(5, True), 9, T0) == int(T0) + 1
