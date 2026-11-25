"""Eligibility, config and hive-lease reads and writes on ``hq.route: api`` (bh-h9dn1), end to end.

A frame host with ``hq.route: api`` and no ``hq.sql`` block answers ``bh host eligible``, the
intake gate (``require_eligible`` / ``require_intake``), hive-lease claim/renew/release/packup
(the reads, and the signed ``POST /hive-leases/{prefix}`` write) and every ``bh config`` read
through the HQ port, against the in-process fake hq-service. Every SQL opener, the SQL plane and
the SQL port adapter are tripwires: none is constructed.

The response verifier is the recording pass-through stand-in of
``tests/test_hq_api_route_plane.py``; what is under test is the plane's binding of each api
read to the signed authority record (operator decision 2026-11-19) and its folding of the
eligibility fields into the frame predicates.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.harness.fake_hq_service import FakeHqService
from typer.testing import CliRunner

from beadhive import (
    config,
    frame_eligibility,
    host_lease,
    hosts,
    hq_control_plane,
    hq_sql_adapter,
    hq_sql_transport,
)
from beadhive.hq_framelease_contracts import ObservationAuthority
from beadhive.hq_port import HqRefused, HqUnavailable
from beadhive.hq_service_adapter import adapter_from_config
from beadhive.hq_sql_signatures import fingerprint, sql_principal
from beadhive.modules.config.domain.ports import FleetConfigDocument, ordered_documents_digest

URL = "https://hq.test"
HEAD = "a" * 32
CONFIG_HEAD = "c" * 32
#: This frame's signing key: it signs the hive-lease requests the fake verifies.
_PRIVATE = Ed25519PrivateKey.generate()
PRIVATE_KEY = _PRIVATE.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.OpenSSH,
    serialization.NoEncryption(),
)
PUBLIC_KEY = (
    _PRIVATE.public_key()
    .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    .decode()
)
KEY = fingerprint(PUBLIC_KEY)
EXPIRES_AT = 4_000_000_000.0
RELEASE = {"id": "rel-1", "digest": "sha256:" + "1" * 64}
CAPS = {
    "isolation": "container",
    "trust_zone": "self-hosted",
    "arch": "x86_64",
    "harnesses": ["claude"],
    "max_sessions": 2,
}
FLEET_YAML = (
    "schema_version: 1\n"
    "delimiter: '/'\n"
    "providers: [github]\n"
    "managed_repos:\n"
    "  - {provider: github, org: acme, repo: bh, prefix: bh}\n"
    "hq:\n"
    "  mode: dolt-server\n"
)


def host_yaml(floor: str = "") -> str:
    floor_line = f"    floor_path: {floor}\n" if floor else ""
    return f"host:\n  frame_id: frame-1\nhq:\n  route: api\n  api:\n    url: {URL}\n{floor_line}"


def manifest(**changes) -> hosts.HostManifest:
    return hosts.HostManifest(
        **{
            "host_id": "host-1",
            "frame_id": "frame-1",
            "label": "frame",
            "instance_ref": "vm-1",
            "os": "linux",
            "arch": "x86_64",
            "role": "executor",
            "state": "active",
            "release": RELEASE,
            "capabilities": CAPS,
            "identity": {"kind": "none"},
            **changes,
        }
    )


def grant() -> dict:
    """hq-service v0.1.0 ``EligibilityRead.active``: the frame's signed active grant."""
    return {
        "authority": {
            "frame_id": "frame-1",
            "holder_identity": "host-1",
            "instance_ref": "vm-1",
            "epoch": 3,
            "key_fingerprint": KEY,
            "audience": "hq",
            "config_revision": "cfg-1",
            "candidate_expires_at": None,
        },
        "state": "active",
        "cordoned": False,
        "public_key": PUBLIC_KEY,
        "desired": {"release": RELEASE, "caps": CAPS, "profile": "reviewed", "declared": True},
    }


def principal() -> str:
    """The SQL principal the grant derives (``hq_frame_grant.frame_grant`` checks it)."""
    return sql_principal(ObservationAuthority(**grant()["authority"]))


def authority(active: dict | None) -> dict:
    """hq-service v0.1.0 ``GET /authority``: the signed record naming ``active``."""
    frames = {"frame-1": {"active": active}} if active is not None else {}
    return {
        "head": HEAD,
        "record": {
            "backend_identity": "svc:hq",
            "generation": "g1",
            "revision": 7,
            "config_backend": "sql:cfg",
            "config_generation": "cfg-gen",
            "config_head": CONFIG_HEAD,
            "state": {"revision": 7, "issued_at": 1.0, "expires_at": EXPIRES_AT, "frames": frames},
            "hive_policies": {"bh": {"config_revision": "cfg-1", "valid_until": EXPIRES_AT}},
        },
        "signature": "sig",
    }


def config_body(signed: dict, documents: list[tuple[str, str]]) -> dict:
    return {
        "authority": signed,
        "publication": "pub-1",
        "sequence": 1,
        "documents_sha256": ordered_documents_digest(
            [FleetConfigDocument(path, content) for path, content in documents]
        ),
        "documents": [
            {
                "path": path,
                "kind": "fleet" if path == "fleet.yaml" else "host",
                "ordinal": ordinal,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content": content,
            }
            for ordinal, (path, content) in enumerate(documents)
        ],
    }


def eligibility_body(active, *, session="fresh", evidence="fresh", admits=True) -> dict:
    """hq-service v0.1.0 ``GET /eligibility/{frame}?release_id=&release_digest=``."""
    return {
        "head": HEAD,
        "config_head": CONFIG_HEAD,
        "frame_id": "frame-1",
        "active": active,
        "principal": principal() if active else None,
        "epoch": 3 if active else None,
        "route": {"carrier": "service", "signer_fingerprint": KEY} if active else None,
        "liveness": {
            "session": {"verdict": session, "renewed_at": 100.0, "age_s": 4.0, "ttl_s": 300},
            "evidence": {
                "verdict": evidence,
                "status": "conformant",
                "measured_at": 90.0,
                "age_s": 14.0,
                "ttl_s": 900,
                "digest": "sha256:" + "e" * 64,
            },
        }
        if active
        else None,
        "release": {**RELEASE, "admits": admits, "basis": "pin", "row": None} if active else None,
    }


def lease_body(holder: str = "host-1") -> dict:
    return {
        "head": HEAD,
        "prefix": "bh",
        "revision": "r" * 64,
        "lease": {
            "host_id": holder,
            "label": holder,
            "epoch": 2,
            "adopted_at": "2026-11-01T00:00:00Z",
            "expires_at": "2026-11-01T01:00:00Z",
        },
        "held": True,
    }


def serve(service: FakeHqService, active=None, **eligibility) -> dict:
    """Serve one consistent HQ: authority, config (with this host's manifest), eligibility."""
    active = grant() if active is None else active
    signed = authority(active or None)
    written = manifest().model_dump(mode="json", exclude_none=True)
    service.reads["/authority"] = signed
    service.reads["/config"] = config_body(
        signed, [("fleet.yaml", FLEET_YAML), ("hosts/host-1.yaml", json.dumps(written))]
    )
    service.reads["/eligibility/frame-1"] = eligibility_body(active or None, **eligibility)
    service.hive_leases["bh"] = lease_body()
    return signed


@pytest.fixture
def service():
    fake = FakeHqService(clock=lambda: 0.0)
    serve(fake)
    return fake


@pytest.fixture
def no_sql(monkeypatch):
    """Every SQL opener, the SQL plane and the SQL port adapter raise: none is constructed."""
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
    monkeypatch.setattr(hq_sql_adapter.SqlHqAdapter, "__init__", tripwire)
    return opened


@pytest.fixture
def api_frame(tmp_path, monkeypatch, service, no_sql):
    """A frame host (``host-1`` / ``frame-1``) on ``hq.route: api`` with no ``hq.sql``."""
    home = tmp_path / "bh-home"
    (home / "hq").mkdir(parents=True)
    (home / "config.yaml").write_text(host_yaml())
    key = tmp_path / "frame-key"
    key.write_bytes(PRIVATE_KEY)
    key.chmod(0o600)
    (home / "host.yaml").write_text(f"host_id: host-1\nlabel: frame\nsigning_key: {key}\n")
    service.provision(
        principal(),
        frame_id="frame-1",
        holder="host-1",
        instance="vm-1",
        epoch=3,
        public_key=PUBLIC_KEY,
    )
    monkeypatch.setenv("BH_HOME", str(home))
    for name in ("BH_CONFIG", "WS_CONFIG", "BH_HQ", "WS_HQ", "BH_HQ_ROUTE", "BH_FRAME_HEARTBEAT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    monkeypatch.delenv("BH_HQ_AUTHORITY_ENFORCE", raising=False)

    def port(host_config):
        return adapter_from_config(
            host_config, verifier=lambda _m, p: p, transport=service.transport()
        )

    monkeypatch.setattr(hq_control_plane, "_api_port", port)
    return home


def _plane() -> hq_control_plane.ApiControlPlane:
    plane = hq_control_plane.control_plane()
    assert isinstance(plane, hq_control_plane.ApiControlPlane)
    return plane


def _paths(service) -> list[str]:
    return [path + (f"?{query}" if query else "") for _m, path, query, _b in service.requests]


# ---- bh host eligible ----------------------------------------------------------------------


def test_host_eligible_for_a_hive_is_decided_through_the_port(api_frame, service, no_sql):
    from beadhive.cli import app

    result = CliRunner().invoke(app, ["host", "eligible", "--hive", "bh", "--json"])
    assert result.exit_code == 0, result.output
    payload = next(
        json.loads(line)
        for line in result.output.splitlines()
        if line.startswith("{") and '"eligible"' in line
    )
    assert payload["eligible"] is True, payload
    predicates = payload["predicates"]
    for name in (
        "current_frame_incarnation",
        "authenticated_fresh_heartbeat",
        "conformance_pass",
        "release_matches",
        "current_hive_lease_holder",
        "hive_policy_bound",
    ):
        assert predicates[name] is True, name
    # The manifest's release is the measured release the service judged.
    assert "/eligibility/frame-1?release_id=rel-1&release_digest=sha256%3A" + "1" * 64 in _paths(
        service
    )
    assert "/hive-leases?prefix=bh" in _paths(service)
    assert no_sql == []


@pytest.mark.parametrize(
    ("eligibility", "predicate"),
    [
        ({"session": "stale"}, "authenticated_fresh_heartbeat"),
        ({"evidence": "stale"}, "conformance_pass"),
        ({"admits": False}, "release_matches"),
    ],
)
def test_a_failing_server_verdict_makes_the_frame_ineligible(
    api_frame, service, no_sql, eligibility, predicate
):
    serve(service, **eligibility)
    decision = frame_eligibility.decision_for("host-1", {"prefix": "bh"})
    assert decision is not None and not decision.allowed
    assert predicate in decision.reason
    assert no_sql == []


def test_another_hosts_lease_is_not_held(api_frame, service):
    service.hive_leases["bh"] = lease_body("host-2")
    decision = frame_eligibility.decision_for("host-1", {"prefix": "bh"})
    assert decision.allowed  # candidate eligibility is independent of holding the lease
    observation = _plane().read_eligibility(manifest(), prefix="bh")[2]
    assert observation.current_hive_lease_holder is False
    assert _plane().read_hive_lease("bh", holder_identity="host-1") is None


# ---- intake: require_eligible and the authoritative primary --------------------------------


def test_intake_require_eligible_and_the_primary_read_go_through_the_port(
    api_frame, service, no_sql
):
    decision = frame_eligibility.require_eligible("host-1", {"prefix": "bh"})
    assert decision is not None and decision.allowed
    stamps = dict(decision.admission)
    assert stamps["carrier"] == "session"
    assert stamps["predicates"]["current_hive_lease_holder"] is True
    lease = _plane().read_hive_lease("bh", holder_identity="host-1")
    assert lease is not None and lease.held_by("host-1")
    assert lease.advisory_expiry is True  # a failover hint on the session carrier
    assert no_sql == []


def test_require_eligible_refuses_a_cordoned_frame(api_frame, service):
    serve(service, active={**grant(), "cordoned": True})
    with pytest.raises(frame_eligibility.EligibilityError, match="not_cordoned"):
        frame_eligibility.require_eligible("host-1", {"prefix": "bh"})


def test_incumbent_read_needs_a_current_grant(api_frame, service):
    plane = _plane()
    assert plane.read_hive_lease_record("bh", incumbent_identity="host-1")[1] is not None
    serve(service, active={})  # the frame holds no grant
    assert plane.read_hive_lease_record("bh", incumbent_identity="host-1")[1] is None


# ---- hive lease: claim/renew/release/packup through the port --------------------------------


def test_the_hive_lease_record_is_read_through_the_port(api_frame, service, no_sql):
    revision, lease = host_lease.read_record("origin", "bh", cwd=config.hq_dir())
    assert revision == "r" * 64 and lease.host_id == "host-1" and lease.epoch == 2
    assert host_lease.read_record("origin", "zz", cwd=config.hq_dir()) == ("", None)
    assert no_sql == []


def _submitted(service) -> list[dict]:
    return [
        body
        for method, path, _q, body in service.requests
        if method == "POST" and path == "/hive-leases/bh"
    ]


def test_claim_renew_and_release_go_through_the_port(api_frame, service, no_sql):
    """Adopt from absent, renew, release: each a signed request the service decides, each CAS
    on the revision the previous write answered."""
    hq = config.hq_dir()
    del service.hive_leases["bh"]  # never adopted: unknown_prefix reads as absent
    adopted = host_lease.adopt("origin", "bh", host_id="host-1", label="frame", cwd=hq)
    assert adopted.lease.epoch == 1 and adopted.previous is None
    assert service.hive_leases["bh"]["revision"] == adopted.sha
    assert host_lease.read_record("origin", "bh", cwd=hq)[1].host_id == "host-1"

    renewed = host_lease.renew("origin", "bh", host_id="host-1", cwd=hq)
    assert renewed.lease.epoch == 1 and renewed.sha != adopted.sha

    released = host_lease.release("origin", "bh", host_id="host-1", cwd=hq)
    assert released.lease.is_tombstone and released.lease.epoch == 1
    assert host_lease.read_record("origin", "bh", cwd=hq)[1].is_tombstone

    requests = [body["request"] for body in _submitted(service)]
    assert [r["operation"] for r in requests] == ["adopt", "renew", "release"]
    assert [r["expected_revision"] for r in requests] == ["", adopted.sha, renewed.sha]
    first = requests[0]
    assert (first["principal"], first["frame_id"], first["holder_identity"], first["epoch"]) == (
        principal(),
        "frame-1",
        "host-1",
        3,
    )
    assert first["key_fingerprint"] == KEY and first["authority_revision"] == HEAD
    assert first["domain"] == "beadhive/sql-hive-lease/v1" and first["force"] is False
    assert no_sql == []


def test_a_refused_write_is_a_definite_refusal_with_the_service_code(
    api_frame, service, no_sql, monkeypatch
):
    """The row moved after the caller read it: the service refuses the stale CAS (409
    ``cas_conflict`` in its error envelope) and nothing is written."""
    real_read = host_lease._read

    def stale(remote, prefix, *, cwd):
        _sha, lease = real_read(remote, prefix, cwd=cwd)
        return "r" * 64, lease

    service.hive_leases["bh"]["revision"] = "s" * 64
    monkeypatch.setattr(host_lease, "_read", stale)
    with pytest.raises(HqRefused, match="cas_conflict") as refused:
        host_lease.renew("origin", "bh", host_id="host-1", cwd=config.hq_dir())
    assert isinstance(refused.value, hq_control_plane.ControlPlaneError)
    assert refused.value.code == "cas_conflict"
    assert service.hive_leases["bh"]["revision"] == "s" * 64  # nothing written
    assert no_sql == []


def test_a_cordoned_frame_publishes_nothing(api_frame, service, no_sql):
    from beadhive.host_lease_contracts import HostLease

    plane = _plane()
    serve(service, active={**grant(), "cordoned": True})
    lease = HostLease("host-1", "frame", 3, "2026-11-01T00:00:00Z", "2026-11-01T01:00:00Z")
    with pytest.raises(hq_control_plane.ControlPlaneError, match="exact active incarnation"):
        plane.publish_hive_lease("bh", lease, expected="r" * 64, operation="renew")
    assert _submitted(service) == []


def test_packup_releases_through_the_port(api_frame, service, no_sql):
    from beadhive.cli import app

    result = CliRunner().invoke(app, ["host", "packup"])
    assert result.exit_code == 0, result.output
    assert "released 1 hive(s): bh" in result.output
    assert service.hive_leases["bh"]["lease"]["host_id"] == ""
    assert [b["request"]["operation"] for b in _submitted(service)] == ["release"]
    assert no_sql == []


# ---- config: bound to the signed record, pinned revision, cached fallback ------------------


def test_config_reads_are_bound_to_the_signed_record(api_frame, service, no_sql):
    from beadhive.cli import app

    result = CliRunner().invoke(app, ["config", "get", "delimiter"])
    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1] == "/"
    paths = _paths(service)
    assert "/config" in paths and "/authority" in paths
    assert no_sql == []


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda env: env.update(head="b" * 32), id="another-head"),
        pytest.param(lambda env: env["record"].update(config_head="d" * 32), id="config-head"),
        pytest.param(lambda env: env["record"].update(revision=6), id="revision"),
    ],
)
def test_a_config_not_bound_to_the_signed_record_is_refused(api_frame, service, tamper):
    from beadhive.cli import app

    envelope = json.loads(json.dumps(service.reads["/config"]["authority"]))
    tamper(envelope)
    service.reads["/config"] = {**service.reads["/config"], "authority": envelope}
    with pytest.raises(HqRefused, match="authority_unbound") as refused:
        _plane().config_store().load_snapshot()
    assert isinstance(refused.value, hq_control_plane.ControlPlaneError)
    result = CliRunner().invoke(app, ["config", "get", "delimiter"])
    assert result.exit_code != 0


def test_a_signed_record_whose_state_names_another_revision_is_refused(api_frame, service):
    signed = service.reads["/authority"]
    signed["record"]["state"]["revision"] = 6  # served the same in both reads
    with pytest.raises(HqRefused, match="authority_unbound"):
        _plane().config_store().load_snapshot()


def test_eligibility_whose_grant_is_not_the_signed_one_is_refused(api_frame, service):
    body = service.reads["/eligibility/frame-1"]
    body["active"] = {**body["active"], "cordoned": True}  # unsigned body, signed grant differs
    with pytest.raises(HqRefused, match="authority_unbound"):
        _plane().read_eligibility(manifest())
    decision = frame_eligibility.decision_for("host-1", {"prefix": "bh"})
    assert not decision.allowed and "authority_available" in decision.reason


def test_a_manifest_of_another_incarnation_differs_from_the_grant(api_frame, service):
    with pytest.raises(hq_control_plane.ControlPlaneError, match="differs from current grant"):
        _plane().read_eligibility(manifest(instance_ref="vm-2"))


def test_a_pinned_revision_must_still_be_current(api_frame, service):
    store = _plane().config_store()
    assert store.load_snapshot(revision=CONFIG_HEAD).commit_revision == CONFIG_HEAD
    with pytest.raises(hq_control_plane.ControlPlaneError, match="no longer current"):
        store.load_snapshot(revision="e" * 32)


@pytest.fixture
def floored(api_frame, tmp_path):
    """A restore floor naming the served authority head, as the real verifier writes it."""
    floor = tmp_path / "floor" / "hq-api.floor"
    floor.parent.mkdir(mode=0o700)
    floor.write_text(json.dumps({"backend": "", "generation": "", "sequence": 7, "head": HEAD}))
    os.chmod(floor, 0o600)
    (api_frame / "config.yaml").write_text(host_yaml(str(floor)))
    hq_control_plane._API_DEGRADED_WARNED.clear()
    return floor


def test_a_trusted_hive_local_read_degrades_to_the_cached_snapshot(floored, service, monkeypatch):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "trusted")
    clock = [1000.0]
    plane = _plane()
    plane.clock = lambda: clock[0]
    fresh = plane.config_store().load_snapshot()
    del service.reads["/config"]  # the service stops answering
    warned = []
    clock[0] += 5
    cached = plane.config_store().load_snapshot_or_cached(warn=warned.append)
    assert cached.commit_revision == fresh.commit_revision == CONFIG_HEAD
    assert cached.documents == fresh.documents
    assert len(warned) == 1 and "cached verified HQ config" in warned[0]
    clock[0] += 60  # older than the validity window
    with pytest.raises(HqUnavailable, match="no cached fallback"):
        plane.config_store().load_snapshot_or_cached(warn=warned.append)


def test_a_cached_snapshot_is_refused_once_the_floor_moves(floored, service, monkeypatch):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "trusted")
    plane = _plane()
    plane.config_store().load_snapshot()
    floored.write_text(json.dumps({"sequence": 8, "head": "b" * 32}))
    del service.reads["/config"]
    with pytest.raises(HqUnavailable, match="restore floor moved"):
        plane.config_store().load_snapshot_or_cached()


def test_signed_mode_never_degrades_to_a_cached_snapshot(floored, service):
    plane = _plane()
    plane.config_store().load_snapshot()
    del service.reads["/config"]
    with pytest.raises(HqUnavailable, match="signed authority mode"):
        plane.config_store().load_snapshot_or_cached()


def test_a_refusal_never_degrades_to_a_cached_snapshot(floored, service, monkeypatch):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "trusted")
    plane = _plane()
    plane.config_store().load_snapshot()
    service.reads["/config"]["authority"] = {**service.reads["/config"]["authority"], "head": "b"}
    with pytest.raises(HqRefused, match="authority_unbound"):
        plane.config_store().load_snapshot_or_cached()


# ---- the authority-expiry advisory and the port surface ------------------------------------


def test_the_expiry_advisory_reads_the_api_authority(api_frame, service, no_sql):
    from beadhive import hq_authority_expiry

    plane = hq_authority_expiry._frame_plane()
    assert isinstance(plane, hq_control_plane.ApiControlPlane)
    status = hq_authority_expiry.authority_status(plane)
    assert status["revision"] == HEAD and status["config_bound"] is True
    assert no_sql == []


def test_both_adapters_serve_the_hive_lease_read():
    from beadhive import hq_port

    assert "read_hive_lease" in vars(hq_port.HqReads)

    class Plane:
        def read_hive_lease_record(self, prefix):
            return ("rev", f"lease-of-{prefix}")

    adapter = object.__new__(hq_sql_adapter.SqlHqAdapter)
    adapter._plane = Plane()
    assert adapter.read_hive_lease("bh", deadline=0.0) == ("rev", "lease-of-bh")


# ---- an unheld, empty lease after a re-join (bh-5i27m) ---------------------------------------


def empty_lease_body(**changes) -> dict:
    """hq-service's answer for a known prefix no live incarnation holds (e.g. its holder's
    incarnation was retired): a 200 with an epoch-0, empty lease and ``held: false``."""
    return {
        "head": HEAD,
        "prefix": "bh",
        "revision": "",
        "lease": {"host_id": "", "label": "", "epoch": 0, "adopted_at": "", "expires_at": ""},
        "held": False,
        **changes,
    }


def _cli_eligible() -> tuple[int, dict]:
    from beadhive.cli import app

    result = CliRunner().invoke(app, ["host", "eligible", "--hive", "bh", "--json"])
    payload = next(
        json.loads(line)
        for line in result.output.splitlines()
        if line.startswith("{") and '"eligible"' in line
    )
    return result.exit_code, payload


@pytest.mark.parametrize(
    "body",
    [
        empty_lease_body(),
        empty_lease_body(revision="q" * 64),
        {key: value for key, value in empty_lease_body().items() if key in ("lease", "held")},
    ],
)
def test_an_unheld_empty_lease_body_is_no_lease(body):
    from beadhive import hq_api_eligibility as api_reads

    assert api_reads.hive_lease("bh", body) == (body.get("revision", ""), None)


def _held(**lease) -> dict:
    body = lease_body()
    return {**body, "lease": {**body["lease"], **lease}}


@pytest.mark.parametrize(
    "body",
    [
        _held(epoch=0),  # a HELD lease must carry an epoch >= 1
        _held(adopted_at="not-a-stamp"),
        _held(expires_at=""),
        empty_lease_body(held=True),  # empty but claims to be held
        empty_lease_body(lease={**empty_lease_body()["lease"], "host_id": "host-9"}),
        empty_lease_body(prefix="zz"),  # another hive's answer
        empty_lease_body(revision=None),
        empty_lease_body(lease={**empty_lease_body()["lease"], "epoch": "0"}),
    ],
)
def test_a_malformed_lease_record_is_still_refused(body):
    from beadhive import hq_api_eligibility as api_reads

    with pytest.raises(api_reads.ApiReadError, match="protected hive lease record invalid"):
        api_reads.hive_lease("bh", body)


def test_an_unheld_empty_lease_reads_as_absent_and_fails_only_the_holder(
    api_frame, service, no_sql
):
    service.hive_leases["bh"] = empty_lease_body()
    assert host_lease.read_record("origin", "bh", cwd=config.hq_dir()) == ("", None)
    decision = frame_eligibility.decision_for("host-1", {"prefix": "bh"})
    assert decision is not None and decision.allowed, decision.reason
    observation = _plane().read_eligibility(manifest(), prefix="bh")[2]
    assert observation.current_hive_lease_holder is False and observation.reason == ""
    code, payload = _cli_eligible()
    assert code == 1
    assert [name for name, ok in payload["predicates"].items() if not ok] == [
        "current_hive_lease_holder"
    ]
    assert payload["candidate_eligible"] is True
    assert no_sql == []


def test_a_lease_read_failure_fails_only_the_lease_predicates(api_frame, service, no_sql):
    """A malformed lease row is not an authority read failure: the frame's other predicates
    are still judged on the signed eligibility read, and the holder predicate fails closed."""
    service.hive_leases["bh"] = _held(epoch=0)
    decision = frame_eligibility.decision_for("host-1", {"prefix": "bh"})
    assert decision is not None and decision.allowed, decision.reason
    assert dict(decision.predicates)["authority_available"] is True
    assert dict(decision.admission)["predicates"]["current_hive_lease_holder"] is False
    observation = _plane().read_eligibility(manifest(), prefix="bh")[2]
    assert observation.current_hive_lease_holder is False
    assert "hive lease unavailable for bh" in observation.reason
    code, payload = _cli_eligible()
    assert code == 1
    assert [name for name, ok in payload["predicates"].items() if not ok] == [
        "current_hive_lease_holder"
    ]
    # The lease's own reads still refuse it rather than reading it as absent.
    with pytest.raises(hq_control_plane.ControlPlaneError, match="hive lease record invalid"):
        host_lease.read_record("origin", "bh", cwd=config.hq_dir())
    assert no_sql == []


def test_an_authority_read_failure_is_still_authority_unavailable(api_frame, service):
    """Isolation is for the lease row only: an eligibility read failure fails closed whole."""
    service.hive_leases["bh"] = empty_lease_body()
    del service.reads["/eligibility/frame-1"]
    decision = frame_eligibility.decision_for("host-1", {"prefix": "bh"})
    assert decision is not None and not decision.allowed
    assert dict(decision.predicates)["authority_available"] is False


def _bare_and_clone(tmp_path, name):
    import subprocess

    def git(*args, cwd):
        subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)

    remote = tmp_path / f"{name}.git"
    git("init", "--bare", "-q", str(remote), cwd=tmp_path)
    clone = tmp_path / name
    clone.mkdir()
    git("init", "-q", cwd=clone)
    git("config", "user.email", "t@example.invalid", cwd=clone)
    git("config", "user.name", "t", cwd=clone)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=clone)
    return str(remote), clone


@pytest.mark.parametrize("revision", ["", "q" * 64])
def test_a_rejoined_frame_adopts_an_unheld_hive_then_is_eligible(
    api_frame, service, no_sql, tmp_path, revision
):
    """After a re-join the service serves the hive's lease as unheld and empty: adopt passes
    eligibility, moves the fence, publishes the lease CAS on the served revision, and the
    frame is then the hive's eligible holder."""
    from beadhive import host_adopt, host_fence

    service.hive_leases["bh"] = empty_lease_body(revision=revision)
    hive_remote, hive_cwd = _bare_and_clone(tmp_path, "hive")
    outcome = host_adopt.adopt(
        prefix="bh",
        hive_remote=hive_remote,
        hq_remote="origin",
        hive_cwd=hive_cwd,
        hq_cwd=config.hq_dir(),
        host_id="host-1",
        label="frame",
        at=1_800_000_000.0,  # a fixed adopt clock: the lease stamps must parse as real times
    )
    assert outcome.epoch == 1
    assert host_fence.read_fence(hive_remote, cwd=hive_cwd)[1].epoch == 1
    (request,) = [body["request"] for body in _submitted(service)]
    assert request["operation"] == "adopt" and request["expected_revision"] == revision
    assert service.hive_leases["bh"]["held"] is True
    assert service.hive_leases["bh"]["lease"]["host_id"] == "host-1"
    code, payload = _cli_eligible()
    assert code == 0, payload
    assert payload["eligible"] is True and payload["predicates"]["current_hive_lease_holder"]
    assert no_sql == []
