"""Frame liveness on an ``hq.route: api`` host with no SQL frame runtime (bh-65s71).

The frame's anchor (principal, epoch, holder, instance, carrier) comes from ``GET
/eligibility/{frame}`` bound to the verified authority (gap G1), and the beat it signs is built by
the sender's own generator from that grant, the committed host manifest (``GET /config``) and,
under a release policy, the catalog row (gap G3). Renew, the conformance evidence post and the
beat verb run against the in-process fake hq-service with every SQL opener a tripwire.

The response verifier is a pass-through stand-in (verification itself is proven in
``tests/test_hq_service_verifier.py``). Every clock is fixed: nothing sleeps or retries.
"""

from __future__ import annotations

import json
import types
from datetime import UTC, datetime
from io import StringIO

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from ruamel.yaml import YAML
from tests.harness.fake_hq_service import FakeHqService

from beadhive import (
    heartbeat_conformance,
    heartbeat_sender,
    hq_control_plane,
    hq_frame_grant,
    hq_service_adapter,
    hq_sql_transport,
)
from beadhive import heartbeat_report as report
from beadhive.hq_frame_grant import ApiFrameAuthority, frame_grant
from beadhive.hq_framelease_contracts import ObservationAuthority
from beadhive.hq_port import FrameAnchor, HqRefused, HqUnavailable
from beadhive.hq_service_adapter import adapter_from_config
from beadhive.hq_sql_session import session_table
from beadhive.hq_sql_signatures import fingerprint, sql_principal
from beadhive.modules.config.domain.ports import FleetConfigDocument, ordered_documents_digest

NOW = 1_793_000_000.0  # the fixed wall clock: beat, conformance cache, service and seq store
MONO = 1000.0  # the fixed monotonic clock deadlines are taken from
URL = "https://hq.test"
FRAME, HOST, INSTANCE, EPOCH = "frame-a", "host-a", "inst-host-a", 3
YARD = "12345678-1234-4234-8234-123456789abc"
RELEASE = {"id": "beadhive-factory-v0.40.0", "digest": "sha256:" + "a" * 64}
HEAD, CONFIG_HEAD = "a" * 32, "c" * 32


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(NOW, tz or UTC)


def _keypair(tmp_path):
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "frame-id_ed25519"
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


def _manifest() -> str:
    stream = StringIO()
    YAML().dump(
        {
            "host_id": HOST,
            "label": "a",
            "os": "linux",
            "arch": "x86_64",
            "role": "executor",
            "identity": {"kind": "none"},
            "frame_id": FRAME,
            "beadyard_id": YARD,
            "instance_ref": INSTANCE,
            "release": RELEASE,
        },
        stream,
    )
    return stream.getvalue()


class Grant:
    """One consistent v0.1.0 read set for FRAME: the signed authority, eligibility and config."""

    def __init__(self, public: str, *, carrier: str = "service"):
        self.public = public
        self.authority = {
            "frame_id": FRAME,
            "holder_identity": HOST,
            "instance_ref": INSTANCE,
            "key_fingerprint": fingerprint(public),
            "epoch": EPOCH,
            "audience": YARD,
            "config_revision": "cfg-1",
            "candidate_expires_at": None,
            "beadyard_id": YARD,
        }
        self.principal = sql_principal(ObservationAuthority(**self.authority))
        self.active = {
            "authority": self.authority,
            "state": "active",
            "cordoned": False,
            "public_key": public,
            "desired": {"release": RELEASE, "profile": "factory-v1"},
        }
        self.carrier = carrier

    def authority_body(self) -> dict:
        return {
            "head": HEAD,
            "record": {
                "revision": 7,
                "config_backend": "svc:cfg",
                "config_generation": "cfg-gen",
                "config_head": CONFIG_HEAD,
                "state": {
                    "revision": 7,
                    "expires_at": 4e9,
                    "frames": {FRAME: {"active": self.active}},
                },
                "hive_policies": {},
            },
            "signature": "sig",
        }

    def eligibility_body(self) -> dict:
        return {
            "head": HEAD,
            "config_head": CONFIG_HEAD,
            "frame_id": FRAME,
            "active": self.active,
            "principal": self.principal,
            "epoch": EPOCH,
            "route": {"carrier": self.carrier, "signer_fingerprint": fingerprint(self.public)},
            "liveness": None,
            "release": None,
        }

    def eligibility(self) -> dict:
        """The adapter's ``read_eligibility`` result: the body plus its verified authority."""
        return {**self.eligibility_body(), "authority": self.authority_body()}

    def config_body(self) -> dict:
        documents = [("fleet.yaml", "schema_version: 1\n"), (f"hosts/{HOST}.yaml", _manifest())]
        return {
            "authority": self.authority_body(),
            "publication": "pub-1",
            "sequence": 1,
            "documents_sha256": ordered_documents_digest(
                [FleetConfigDocument(path, content) for path, content in documents]
            ),
            "documents": [
                {"path": path, "kind": "fleet", "ordinal": i, "sha256": "", "content": content}
                for i, (path, content) in enumerate(documents)
            ],
        }

    def serve(self, fake: FakeHqService) -> None:
        fake.reads["/authority"] = self.authority_body()
        fake.reads[f"/eligibility/{FRAME}"] = self.eligibility_body()
        fake.reads["/config"] = self.config_body()


# ---- the api host ------------------------------------------------------------------------


@pytest.fixture
def no_sql(monkeypatch):
    """Every SQL opener and the SQL plane raise: an api host must reach none."""
    opened = []

    def tripwire(*_args, **_kwargs):
        opened.append(True)
        raise AssertionError("route=api opened SQL")

    monkeypatch.setattr(hq_sql_transport, "connect", tripwire)
    monkeypatch.setattr(hq_sql_transport, "_strict_driver", tripwire)
    for name in ("hq_sql_config", "hq_sql_runtime", "hq_sql_operator", "hq_sql_receiver"):
        module = __import__(f"beadhive.{name}", fromlist=["connect"])
        monkeypatch.setattr(module, "connect", tripwire)
    monkeypatch.setattr(hq_control_plane.SqlControlPlane, "__init__", tripwire)
    monkeypatch.setattr(report, "_session_opener", tripwire)
    return opened


@pytest.fixture
def api_host(tmp_path, monkeypatch, no_sql):
    """A ``route: api`` host with ``host.frame_id`` and NO ``hq.sql`` block, a recorded frame
    signing key, a fresh conformance cache, and the fake hq-service serving its grant."""
    home = tmp_path / "bh-home"
    home.mkdir()
    monkeypatch.setenv("BH_HOME", str(home))
    for name in ("BH_HQ_ROUTE", "BH_HQ_SESSION_VIA_PORT", "BH_HQ_SESSION_DUAL_WRITE"):
        monkeypatch.delenv(name, raising=False)
    key, public = _keypair(tmp_path)
    host_config = {"hq": {"route": "api", "api": {"url": URL}}, "host": {"frame_id": FRAME}}
    monkeypatch.setattr(report.config, "load_host", lambda: host_config)
    monkeypatch.setattr(
        report.config, "load", lambda: {"host": {"frame_id": FRAME}, "managed_repos": [{}]}
    )
    monkeypatch.setattr(report.host, "host_id", lambda: HOST)
    monkeypatch.setattr(report.host, "signing_key", lambda: key)
    monkeypatch.setattr(report, "installed_release", lambda: dict(RELEASE))
    monkeypatch.setattr(report, "datetime", _Frozen)
    frozen = types.SimpleNamespace(monotonic=lambda: MONO)
    monkeypatch.setattr(hq_service_adapter, "time", frozen)
    monkeypatch.setattr(hq_frame_grant, "time", frozen)
    monkeypatch.setattr(report, "_port_deadline", lambda plane: MONO + 30)
    report._ACCEPTED.clear()

    grant = Grant(public)
    fake = FakeHqService(clock=lambda: NOW)
    fake.provision(
        grant.principal,
        frame_id=FRAME,
        holder=HOST,
        instance=INSTANCE,
        epoch=EPOCH,
        public_key=public,
    )
    grant.serve(fake)

    def select():
        return adapter_from_config(
            report.config.load_host(),
            verifier=lambda method, payload: payload,
            transport=fake.transport(),
            clock=lambda: NOW,
        )

    monkeypatch.setattr(heartbeat_sender, "_select_port", select)

    def refresh():
        def measure():
            return [
                {"id": name, "status": "pass"} for name in heartbeat_conformance.CACHED_CHECK_IDS
            ]

        return heartbeat_conformance.refresh(
            measure, path=report.conformance_cache_path(), clock=lambda: NOW
        )

    monkeypatch.setattr(report, "refresh_conformance", refresh)
    monkeypatch.setattr(heartbeat_sender, "status", lambda: {})
    refresh()
    yield types.SimpleNamespace(fake=fake, grant=grant, no_sql=no_sql)
    report._ACCEPTED.clear()


def _liveness(fake: FakeHqService) -> list[tuple[str, str]]:
    return [entry for entry in fake.seen if entry[0] in ("renew", "evidence")]


def test_renew_evidence_and_beat_need_no_sql_frame_runtime(api_host, capsys):
    """The acceptance: renew, evidence and the beat against the fake; no SQL opener is called."""
    fake = api_host.fake
    assert heartbeat_sender.main(["renew"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"renewed": session_table(api_host.grant.principal, EPOCH)}
    report._ACCEPTED.clear()  # the conformance job is its own process: it renews first
    assert heartbeat_sender.main(["conformance"]) == 0
    assert heartbeat_sender.main(["beat"]) == 0
    assert "beat skipped: hq.route api beats through session renew" in capsys.readouterr().err
    assert _liveness(fake) == [
        ("renew", "accepted"),
        ("renew", "accepted"),
        ("evidence", "accepted"),
    ]
    assert api_host.no_sql == []
    record = fake.principals[api_host.grant.principal]
    assert record.observation is not None and record.evidence  # the service holds both


def test_the_renewed_beat_carries_the_signed_grants_fields(api_host):
    fake = api_host.fake
    assert heartbeat_sender.main(["renew"]) == 0
    (renew,) = [r for r in fake.requests if r[1].endswith("/renew")]
    spec = renew[3]["spec"]
    assert (spec["frame_id"], spec["holderIdentity"], spec["instance_ref"], spec["epoch"]) == (
        FRAME,
        HOST,
        INSTANCE,
        EPOCH,
    )
    assert (spec["audience"], spec["beadyard_id"], spec["config_revision"]) == (YARD, YARD, "cfg-1")
    assert spec["key_id"] == fingerprint(api_host.grant.public)
    assert spec["state_seen"] == "active"
    assert spec["conformance"]["profile"] == "factory-v1"
    assert spec["conformance"]["status"] == "conformant"
    assert renew[1] == f"/sessions/{api_host.grant.principal}/{EPOCH}/renew"
    # the grant, the manifest and nothing else were read for it
    reads = {path for method, path, _q, _b in fake.requests if method == "GET"}
    assert reads == {"/authority", f"/eligibility/{FRAME}", "/config"}


def test_session_via_port_and_dual_write_are_ignored_on_an_api_host(api_host, monkeypatch):
    monkeypatch.setenv("BH_HQ_SESSION_VIA_PORT", "0")
    monkeypatch.setenv("BH_HQ_SESSION_DUAL_WRITE", "1")
    assert heartbeat_sender.main(["renew"]) == 0
    assert _liveness(api_host.fake) == [("renew", "accepted")]
    assert api_host.no_sql == []


def test_a_frame_with_no_active_grant_renews_nothing(api_host, capsys):
    fake = api_host.fake
    body = fake.reads[f"/eligibility/{FRAME}"]
    fake.reads[f"/eligibility/{FRAME}"] = {
        **body,
        "active": None,
        "principal": None,
        "epoch": None,
        "route": None,
    }
    fake.reads["/authority"]["record"]["state"]["frames"] = {FRAME: {"active": None}}
    assert heartbeat_sender.main(["renew"]) == 1
    assert "session renewal refused" in capsys.readouterr().err
    assert _liveness(fake) == [] and api_host.no_sql == []


def test_an_api_host_without_host_frame_id_has_no_anchor(api_host, monkeypatch):
    monkeypatch.setattr(
        report.config, "load_host", lambda: {"hq": {"route": "api", "api": {"url": URL}}}
    )
    with pytest.raises(HqUnavailable, match="host.frame_id"):
        report.session_renewer(heartbeat_sender._select_port).tick()
    assert _liveness(api_host.fake) == []


# ---- the anchor: frame_grant over the eligibility read -------------------------------------


@pytest.fixture
def grant(tmp_path):
    return Grant(_keypair(tmp_path)[1])


def test_the_anchor_is_the_signed_grants_incarnation(grant):
    parsed = frame_grant(grant.eligibility(), FRAME)
    assert parsed.anchor == FrameAnchor(
        principal=grant.principal,
        frame=FRAME,
        epoch=EPOCH,
        holder=HOST,
        instance=INSTANCE,
        carrier="service",
        head=HEAD,
    )
    assert parsed.record == grant.active


def _altered(grant: Grant, **changes) -> dict:
    return {**grant.eligibility(), **changes}


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"frame_id": "frame-b"}, HqUnavailable, "this frame's"),
        ({"principal": "frame_" + "0" * 20}, HqUnavailable, "incarnation"),
        ({"epoch": EPOCH + 1}, HqUnavailable, "incarnation"),
        ({"route": None}, HqRefused, "unprovisioned_principal"),
        (
            {"route": {"carrier": "inbox", "signer_fingerprint": "SHA256:other"}},
            HqRefused,
            "identity_mismatch",
        ),
        ({"route": {"carrier": "pigeon", "signer_fingerprint": "x"}}, HqUnavailable, "carrier"),
        ({"active": {"authority": {}, "state": "active"}}, HqUnavailable, "verified authority"),
    ],
)
def test_an_eligibility_body_that_is_not_the_signed_grant_is_refused(grant, changes, error, match):
    with pytest.raises(error, match=match):
        frame_grant(_altered(grant, **changes), FRAME)


def test_no_active_grant_is_a_no_current_grant_refusal(grant):
    body = _altered(grant, active=None, principal=None, epoch=None, route=None)
    body["authority"]["record"]["state"]["frames"] = {FRAME: {"active": None}}
    with pytest.raises(HqRefused, match="no_current_grant"):
        frame_grant(body, FRAME)


def test_the_adapter_anchor_reads_eligibility_for_its_own_frame(grant):
    fake = FakeHqService(clock=lambda: NOW)
    grant.serve(fake)
    adapter = adapter_from_config(
        {"hq": {"route": "api", "api": {"url": URL}}, "host": {"frame_id": FRAME}},
        verifier=lambda method, payload: payload,
        transport=fake.transport(),
    )
    deadline = 4e9  # far ahead of the real monotonic clock; nothing waits on it
    assert adapter.read_frame_anchor(expected_head=None, deadline=deadline).head == HEAD
    with pytest.raises(HqUnavailable, match="moved"):
        adapter.read_frame_anchor(expected_head="b" * 32, deadline=deadline)
    paths = [path for _m, path, _q, _b in fake.requests]
    assert paths == ["/authority", f"/eligibility/{FRAME}"] * 2


def test_the_catalog_row_is_read_through_the_port_at_the_verified_head():
    calls = []

    class Port:
        def read_release_catalog_row(self, head, catalog, measured, *, deadline):
            calls.append((head, catalog, measured))
            return {"status": "active"}

    plane = ApiFrameAuthority(Port(), FRAME)
    assert plane._runtime_authority().read_release_catalog_row(HEAD, "cat", RELEASE) == {
        "status": "active"
    }
    assert calls == [(HEAD, "cat", RELEASE)]


def test_port_errors_are_redacted_categories():
    assert report.refusal_category(HqUnavailable("db.internal password=x")) == "transport"
    assert report.refusal_category(HqRefused("no_current_grant")) == "grant"
