"""``hq.route: api`` selects the control plane (bh-e437a).

``control_plane()`` and ``attach_fleet_config`` honor the route: an api host with no ``hq.sql``
block resolves ``bh config get``, ``bh hq authority status`` and ``bh host list`` through the HQ
port, against the in-process fake hq-service, and opens no SQL connection (every SQL opener is
a tripwire here). A plane method the service has no route for fails with ``HqUnavailable``
naming the method; nothing falls back to SQL.

The response verifier is a recording pass-through stand-in for the one ``select_hq_port`` wires
from the ``hq.api`` pins (bh-2r7qd, proven in ``tests/test_hq_service_verifier.py``): these
tests pin the plane seam and its reading of the verified bodies, not verification.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from tests.harness.fake_hq_service import FakeHqService
from typer.testing import CliRunner

from beadhive import config, hosts, hq_control_plane, hq_sql_transport
from beadhive.hq_port import HqUnavailable
from beadhive.hq_service_adapter import adapter_from_config
from beadhive.modules.config.domain.ports import FleetConfigDocument, ordered_documents_digest

URL = "https://hq.test"
HOST_YAML = f"hq:\n  route: api\n  api:\n    url: {URL}\n"
FLEET_YAML = (
    "schema_version: 1\n"
    "delimiter: '/'\n"
    "providers: [github]\n"
    "managed_repos: []\n"
    "hq:\n"
    "  mode: dolt-server\n"
)
EXPIRES_AT = 4_000_000_000.0
STATE = {"revision": 7, "expires_at": EXPIRES_AT}
CROSSREF = ["svc:hq", "g1", "head-1"]


VERIFIED: list[str] = []

#: hq-service v0.1.0 ``GET /authority`` (``store/authority.go``): the signed record carries the
#: config cross-reference the config read is bound to.
AUTHORITY = {
    "head": "auth-9",
    "record": {
        "revision": 7,
        "config_backend": CROSSREF[0],
        "config_generation": CROSSREF[1],
        "config_head": CROSSREF[2],
        "state": STATE,
        "hive_policies": {},
    },
    "signature": "sig",
}


def config_body(documents: list[tuple[str, str]]) -> dict:
    """hq-service v0.1.0 ``GET /config`` (``store/reads.go`` ``ConfigRead``)."""
    return {
        "authority": AUTHORITY,
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


def eligibility_body(frame: str, verdict: str = "fresh", age: float | None = 3.0) -> dict:
    """hq-service v0.1.0 ``GET /eligibility/{frame}`` (``EligibilityRead``)."""
    return {
        "head": "auth-9",
        "config_head": CROSSREF[2],
        "frame_id": frame,
        "active": {
            "authority": {
                "frame_id": frame,
                "holder_identity": "host-1",
                "instance_ref": "vm-1",
                "epoch": 0,
                "config_revision": "cfg-1",
            },
            "state": "active",
            "cordoned": False,
            "desired": {"profile": "reviewed", "declared": True},
        },
        "liveness": {
            "session": {"verdict": verdict, "renewed_at": 1.0, "age_s": age, "ttl_s": 60},
            "evidence": {"verdict": "fresh", "status": "conformant", "ttl_s": 600},
        },
    }


def serve_eligibility(service, body: dict) -> None:
    """Serve *body* and sign its grant: the api plane binds the eligibility ``active`` grant to
    the signed authority record (bh-h9dn1), so the record names the same grant."""
    record = json.loads(json.dumps(AUTHORITY["record"]))
    if body["active"] is not None:
        record["state"]["frames"] = {body["frame_id"]: {"active": body["active"]}}
        record["hive_policies"] = {"bh": {"config_revision": "cfg-1", "valid_until": EXPIRES_AT}}
    signed = {**AUTHORITY, "record": record}
    service.reads["/authority"] = signed
    service.reads["/config"] = {**service.reads["/config"], "authority": signed}
    service.reads[f"/eligibility/{body['frame_id']}"] = body


def frame_manifest(frame: str = "frame-1") -> hosts.HostManifest:
    return hosts.HostManifest(
        host_id="host-1",
        frame_id=frame,
        label="frame",
        instance_ref="vm-1",
        os="linux",
        arch="x86_64",
        role="executor",
        identity={"kind": "none"},
    )


def _verify(method, payload):
    """Stand-in verifier: records the method and returns the body, as the real one does once
    the signature and floor check out."""
    VERIFIED.append(method)
    return payload


@pytest.fixture
def service():
    fake = FakeHqService(clock=lambda: 0.0)
    fake.reads["/config"] = config_body([("fleet.yaml", FLEET_YAML)])
    fake.reads["/authority"] = AUTHORITY
    return fake


@pytest.fixture
def no_sql(monkeypatch):
    """Every SQL opener and the SQL plane itself raise: an api host must reach none."""
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
    return opened


@pytest.fixture
def api_host(tmp_path, monkeypatch, service, no_sql):
    home = tmp_path / "bh-home"
    (home / "hq").mkdir(parents=True)
    (home / "config.yaml").write_text(HOST_YAML)
    monkeypatch.setenv("BH_HOME", str(home))
    for name in ("BH_CONFIG", "WS_CONFIG", "BH_HQ", "WS_HQ", "BH_HQ_ROUTE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    monkeypatch.delenv("BH_HQ_AUTHORITY_ENFORCE", raising=False)
    built = []
    VERIFIED.clear()

    def port(host_config):
        built.append(host_config)
        return adapter_from_config(host_config, verifier=_verify, transport=service.transport())

    monkeypatch.setattr(hq_control_plane, "_api_port", port)
    return home, built


def test_the_route_selects_the_api_plane_with_no_sql_block(api_host):
    _home, built = api_host
    plane = hq_control_plane.control_plane()
    assert isinstance(plane, hq_control_plane.ApiControlPlane)
    assert plane.config_backend == "api"
    assert built == [{"hq": {"route": "api", "api": {"url": URL}}}]
    assert config.fleet_sql_selected() is True


def test_attach_fleet_config_reads_the_committed_snapshot_through_the_port(api_host, service):
    store, snapshot = hq_control_plane.attach_fleet_config(
        bootstrap={"route": "api", "api": {"url": URL}}
    )
    assert isinstance(store, hq_control_plane.ApiConfigStore)
    assert (snapshot.backend_identity, snapshot.commit_revision) == ("svc:hq", "head-1")
    # The config's authority envelope is bound to a separate verified authority read (bh-h9dn1).
    assert service.seen == [("read", "/config"), ("read", "/authority")]


def test_config_get_resolves_through_the_port(api_host, service, no_sql):
    from beadhive.cli import app

    result = CliRunner().invoke(app, ["config", "get", "delimiter"])
    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1] == "/"
    assert ("read", "/config") in service.seen
    assert "read_config" in VERIFIED
    assert no_sql == []


def test_authority_status_resolves_through_the_port(api_host, service, no_sql):
    from beadhive.cli import app

    result = CliRunner().invoke(app, ["hq", "authority", "status"])
    assert result.exit_code == 0, result.output
    status = json.loads(result.output.strip().splitlines()[-1])
    assert status["revision"] == "auth-9"
    assert status["expires_at"] == EXPIRES_AT
    # The signed crossref names the committed head the port served, so it is bound.
    assert status["config_bound"] is True
    assert ("read", "/authority") in service.seen
    assert "read_authority" in VERIFIED
    assert no_sql == []


def test_host_list_observes_frames_through_the_port(api_host, service, no_sql, tmp_path):
    """The roster is the committed snapshot's ``hosts/`` documents and each frame's
    observation is the port's eligibility read: both through hq-service."""
    from beadhive.cli import app

    written = hosts.save(
        tmp_path / "manifest",
        hosts.HostManifest(
            host_id="host-1",
            frame_id="frame-1",
            label="frame",
            instance_ref="vm-1",
            os="linux",
            arch="x86_64",
            role="executor",
            identity={"kind": "none"},
        ),
    )
    service.reads["/config"] = config_body(
        [("fleet.yaml", FLEET_YAML), ("hosts/host-1.yaml", written.read_text())]
    )
    serve_eligibility(service, eligibility_body("frame-1"))
    result = CliRunner().invoke(app, ["host", "list", "--json"])
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.output[result.output.index("[") :])
    assert (row["host_id"], row["heartbeat_status"], row["heartbeat_verified"]) == (
        "host-1",
        "fresh",
        True,
    )
    assert ("read", "/eligibility/frame-1") in service.seen
    assert no_sql == []


@pytest.mark.parametrize("method", sorted(hq_control_plane.API_UNROUTED))
def test_an_unrouted_method_fails_naming_it_and_never_reaches_sql(api_host, no_sql, method):
    plane = hq_control_plane.control_plane()
    with pytest.raises(HqUnavailable, match=method) as raised:
        getattr(plane, method)("arg", expected="x")
    # Also a control-plane refusal, so every existing CLI error path reports it.
    assert isinstance(raised.value, hq_control_plane.ControlPlaneError)
    assert no_sql == []


def test_an_unrouted_store_method_fails_naming_it(api_host):
    store = hq_control_plane.control_plane().config_store()
    with pytest.raises(HqUnavailable, match="config_store.publish_snapshot"):
        store.publish_snapshot((), expected_revision="head-1")


def test_an_unreachable_service_is_unavailable_naming_the_method(api_host, monkeypatch, no_sql):
    plane = hq_control_plane.control_plane()
    with pytest.raises(HqUnavailable, match="read_eligibility"):
        plane.read_eligibility(
            hosts.HostManifest(
                host_id="h",
                frame_id="absent",
                label="x",
                os="linux",
                arch="x86_64",
                role="executor",
                identity={"kind": "none"},
            )
        )
    assert no_sql == []


def test_an_absent_attribute_is_not_a_plane_method(api_host):
    plane = hq_control_plane.control_plane()
    assert getattr(plane, "signed_liveness", False) is False
    assert not hasattr(plane, "_read")


def test_route_api_needs_an_endpoint(tmp_path, monkeypatch):
    from beadhive.modules.config.contracts import HqConfig

    with pytest.raises(ValueError, match="hq.api"):
        HqConfig.model_validate({"route": "api"})
    with pytest.raises(ValueError, match="hq.api"):
        HqConfig.model_validate({"mode": "api"})
    with pytest.raises(ValueError, match="hq.route api"):
        HqConfig.model_validate({"mode": "api", "route": "sql", "api": {"url": URL}})
    assert HqConfig.model_validate({"mode": "api", "api": {"url": URL}}).sql.enabled is False


def test_hq_mode_api_selects_the_api_route():
    from beadhive.hq_port import configured_route

    assert configured_route({"hq": {"mode": "api"}}) == "api"
    assert configured_route({"hq": {"mode": "api", "route": "api"}}) == "api"
    assert configured_route({"hq": {"mode": "dolt-server"}}) == "sql"


def test_a_route_sql_host_keeps_the_sql_plane(tmp_path, monkeypatch):
    """No hq.route: the SQL/Git selection is unchanged and the port is never built."""
    home = tmp_path / "bh-home"
    (home / "hq").mkdir(parents=True)
    (home / "config.yaml").write_text("otel:\n  enabled: false\n")
    monkeypatch.setenv("BH_HOME", str(home))
    for name in ("BH_CONFIG", "WS_CONFIG", "BH_HQ", "WS_HQ", "BH_HQ_ROUTE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        hq_control_plane, "_api_port", lambda _cfg: pytest.fail("route=sql built the api port")
    )
    assert isinstance(hq_control_plane.control_plane(), hq_control_plane.GitControlPlane)
    assert config.fleet_sql_selected() is False


@pytest.mark.parametrize("path", ["/authority", "/config"])
def test_a_verified_body_of_the_wrong_shape_is_unavailable(api_host, service, path):
    service.reads[path] = {"authority": {"head": "auth-9"}, "unexpected": True}
    plane = hq_control_plane.control_plane()
    with pytest.raises(HqUnavailable, match="read_authority|read_config"):
        if path == "/authority":
            plane.authority_state()
        else:
            plane.config_store().load_snapshot()


def test_a_config_whose_documents_do_not_match_their_digest_is_unavailable(api_host, service):
    from beadhive.hq_control_plane import HqPlaneUnavailable

    service.reads["/config"] = {
        **config_body([("fleet.yaml", FLEET_YAML)]),
        "documents_sha256": "0",
    }
    with pytest.raises(HqPlaneUnavailable, match="not a committed config snapshot"):
        hq_control_plane.attach_fleet_config(bootstrap={"route": "api", "api": {"url": URL}})


@pytest.mark.parametrize(
    ("verdict", "age", "status", "fresh"),
    [
        ("fresh", 3.0, "fresh", True),
        ("stale", 90.0, "stale", False),
        ("never-renewed", None, "missing", False),
    ],
)
def test_eligibility_observation_is_the_servers_session_verdict(
    api_host, service, verdict, age, status, fresh
):
    serve_eligibility(service, eligibility_body("frame-1", verdict, age))
    head, desired, observation = hq_control_plane.control_plane().read_eligibility(frame_manifest())
    assert head == "auth-9"
    assert (observation.status, observation.fresh, observation.age_seconds) == (status, fresh, age)
    assert observation.verified is True
    assert desired["state"] == "active" and desired["profile"] == "reviewed"
    assert desired["authority"]["holder_identity"] == "host-1"


def test_a_frame_with_no_active_grant_has_a_missing_observation_and_no_desired(api_host, service):
    body = {**eligibility_body("frame-1"), "active": None, "liveness": None}
    serve_eligibility(service, body)
    _head, desired, observation = hq_control_plane.control_plane().read_eligibility(
        frame_manifest()
    )
    assert desired == {} and observation.status == "missing" and not observation.fresh
