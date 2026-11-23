"""Join and provisioning on ``hq.route: api`` (bh-obhfx).

A new frame joins through the HQ port only, against the in-process fake hq-service (which, with
``authority_state`` set, admits as ``store/admission.go`` does): ``bh host init`` publishes the
manifest (``PUT /manifests``), the incarnation is registered (``POST /incarnations``) and the
frame open-admits itself (``POST /admissions``); it lands as an ACTIVE grant and renews. No SQL
connection is opened, no login is provisioned and no secret store is touched (every one is a
tripwire here). ``bh host rm`` withdraws the manifest through the port (``DELETE /manifests``).

The Gateway's caller binding is exercised with the fake's ``caller``: registration is
operator-bound and admission frame-bound, so the two seats each do their own half.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from ruamel.yaml import YAML
from tests.harness.fake_hq_service import FakeHqService
from typer.testing import CliRunner

from beadhive import (
    config,
    hq,
    hq_control_plane,
    hq_sql_operator,
    hq_sql_session_provision,
    hq_sql_transport,
)
from beadhive import fleet_roster as roster
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
    "  default_authority_mode: trusted\n"
)
HOST_ID, FRAME = "host-1", "frame-1"
#: The HQ's beadyard identity (hq-service stamps it into a frame manifest that omits it).
YARD = "12345678-1234-4234-8234-123456789abc"
NOW = 1_793_000_000.0
CORPUS = Path(__file__).parent / "fixtures/hq_service_contract/session_v1"


def _passthrough(_method, payload):
    return payload


def _manifest() -> roster.HostManifest:
    return roster.HostManifest(
        host_id=HOST_ID,
        frame_id=FRAME,
        beadyard_id=YARD,
        label="frame",
        instance_ref="vm-1",
        os="linux",
        arch="x86_64",
        role="executor",
        identity={"kind": "none"},
        release={"id": "0.40.0", "digest": "sha256:" + "0" * 64},
        capabilities={
            "isolation": "kvm",
            "trust_zone": "self-hosted",
            "arch": "x86_64",
            "harnesses": ["claude"],
            "max_sessions": 2,
        },
    )


def _dump(body: dict) -> str:
    from io import StringIO

    stream = StringIO()
    YAML().dump(body, stream)
    return stream.getvalue()


def _config(service: FakeHqService, bodies: dict | None = None) -> dict:
    """``GET /config`` over what the fake committed: fleet.yaml plus every PUT manifest (or
    ``bodies``: the manifests the authority's pinned head serves)."""
    bodies = service.manifest_bodies if bodies is None else bodies
    documents = [("fleet.yaml", FLEET_YAML)] + [
        (f"hosts/{host_id}.yaml", _dump(body)) for host_id, body in sorted(bodies.items())
    ]
    return {
        "authority": _authority(service),
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


def _authority(service: FakeHqService) -> dict:
    """``GET /authority`` at the fake's current runtime and config heads."""
    return {
        "head": service.head,
        "record": {
            # The signed record names its own revision, which its state repeats (the api reads
            # bind to that, bh-h9dn1).
            "revision": service.authority_state["revision"],
            "config_backend": "svc:hq",
            "config_generation": "g1",
            "config_head": service.config_head,
            "state": service.authority_state,
            "hive_policies": {},
        },
        "signature": "sig",
    }


@pytest.fixture
def service():
    fake = FakeHqService(clock=lambda: NOW)
    fake.authority_state = {"revision": 1, "expires_at": 4_000_000_000.0, "frames": {}}
    fake.reads["/authority"] = lambda: _authority(fake)
    fake.reads["/config"] = lambda: _config(fake)
    return fake


@pytest.fixture
def tripwires(monkeypatch):
    """Every SQL opener, the SQL plane, SQL provisioning and the secret broker raise."""
    hit: list[str] = []

    def wire(name):
        def tripped(*_args, **_kwargs):
            hit.append(name)
            raise AssertionError(f"route=api reached {name}")

        return tripped

    monkeypatch.setattr(hq_sql_transport, "connect", wire("sql connect"))
    monkeypatch.setattr(hq_sql_transport, "_strict_driver", wire("sql driver"))
    for name in ("hq_sql_config", "hq_sql_runtime", "hq_sql_operator", "hq_sql_receiver"):
        module = __import__(f"beadhive.{name}", fromlist=["connect"])
        monkeypatch.setattr(module, "connect", wire(f"{name}.connect"))
    monkeypatch.setattr(hq_control_plane.SqlControlPlane, "__init__", wire("SqlControlPlane"))
    monkeypatch.setattr(
        hq_sql_operator.SqlRuntimeOperator,
        "provision_release_route",
        wire("provision_release_route"),
    )
    monkeypatch.setattr(hq_sql_session_provision, "resume_incarnation", wire("resume_incarnation"))
    monkeypatch.setattr(hq_sql_transport.FnoxBroker, "__init__", wire("FnoxBroker"))
    monkeypatch.setattr(hq_sql_transport.FnoxBroker, "get", wire("FnoxBroker.get"))
    return hit


@pytest.fixture
def frame_key(tmp_path):
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "frame_ed25519"
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
    Path(f"{path}.pub").write_text(public + "\n")
    return str(path), public


@pytest.fixture
def api_frame(tmp_path, monkeypatch, service, tripwires, frame_key):
    """This host: ``hq.route: api``, host identity ``host-1`` with its frame key, a learned
    trusted fleet default, and the port wired to the fake (passthrough verifier)."""
    home = tmp_path / "bh-home"
    (home / "hq").mkdir(parents=True)
    (home / "state").mkdir()
    (home / "config.yaml").write_text(HOST_YAML)
    (home / "host.yaml").write_text(
        f"host_id: {HOST_ID}\nlabel: frame\nsigning_key: {frame_key[0]}\n"
    )
    (home / "state" / "hq-fleet-authority-mode.json").write_text(json.dumps({"mode": "trusted"}))
    monkeypatch.setenv("BH_HOME", str(home))
    for name in (
        "BH_CONFIG",
        "WS_CONFIG",
        "BH_HQ",
        "WS_HQ",
        "BH_HQ_ROUTE",
        "BH_HQ_OPERATOR_SETTINGS",
        "BH_HQ_AUTHORITY_ENFORCE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "trusted")

    def port(host_config):
        return adapter_from_config(
            host_config, verifier=_passthrough, transport=service.transport()
        )

    monkeypatch.setattr(hq_control_plane, "_api_port", port)
    return home


def _result(output: str) -> dict:
    """The JSON result line (a frame-mode warning may precede it)."""
    return json.loads(next(line for line in reversed(output.splitlines()) if line.startswith("{")))


def _calls(service: FakeHqService) -> list[tuple[str, str]]:
    return [(method, path) for method, path, _query, _body in service.requests if method != "GET"]


def _join(*args: str):
    from beadhive.cli import app

    return CliRunner().invoke(app, ["hq", "authority", "join", *args, "--confirm"])


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(NOW, tz or UTC)


def _eligibility(service: FakeHqService) -> dict:
    """``GET /eligibility/{frame}`` over the grant the fake's admission wrote (v0.1.0
    ``EligibilityRead``): the frame's ACTIVE record and its registered service route."""
    from beadhive.hq_framelease_contracts import ObservationAuthority
    from beadhive.hq_sql_signatures import sql_principal

    active = service.authority_state["frames"][FRAME]["active"]
    authority = active["authority"]
    return {
        "head": service.head,
        "config_head": service.config_head,
        "frame_id": FRAME,
        "active": active,
        "principal": sql_principal(ObservationAuthority(**authority)),
        "epoch": authority["epoch"],
        "route": {"carrier": "service", "signer_fingerprint": authority["key_fingerprint"]},
        "liveness": None,
        "release": None,
    }


def _renew(service: FakeHqService, monkeypatch):
    """One renew through the real adapter and its DEFAULT beat source: the anchor and the
    grant-bound beat fields are read back from hq-service (``ApiFrameAuthority``, bh-65s71),
    the beat is built by the sender's generator from the cached conformance report and signed
    with this host's recorded frame key."""
    import time

    from beadhive import heartbeat_conformance
    from beadhive import heartbeat_report as report
    from beadhive.hq_frame_grant import ApiFrameAuthority

    service.reads[f"/eligibility/{FRAME}"] = lambda: _eligibility(service)
    monkeypatch.setattr(report, "installed_release", lambda: dict(_manifest().release.model_dump()))
    monkeypatch.setattr(report, "datetime", _Frozen)
    heartbeat_conformance.refresh(
        lambda: [{"id": name, "status": "pass"} for name in heartbeat_conformance.CACHED_CHECK_IDS],
        path=report.conformance_cache_path(),
        clock=lambda: NOW,
    )
    adapter = adapter_from_config(
        {"hq": {"route": "api", "api": {"url": URL}}},
        verifier=_passthrough,
        transport=service.transport(),
        clock=lambda: NOW,
    )
    anchor = ApiFrameAuthority(adapter, FRAME).grant().anchor
    return anchor, adapter.renew_session(anchor, deadline=time.monotonic() + 15)


def test_a_new_frame_joins_as_an_active_grant_and_renews(
    api_frame, service, tripwires, frame_key, monkeypatch
):
    roster.save(config.hq_dir(), _manifest())
    assert service.manifest_bodies[HOST_ID]["frame_id"] == FRAME

    result = _join()
    assert result.exit_code == 0, result.output
    joined = _result(result.output)
    assert joined["registered"] is True and joined["admitted"] is True
    assert joined["route"] == "api"
    assert _calls(service) == [
        ("PUT", f"/manifests/{HOST_ID}"),
        ("POST", "/incarnations"),
        ("POST", f"/admissions/{FRAME}"),
    ]
    # The registration body is the incarnation alone: no login, no password, no secret.
    (incarnation,) = [body for m, p, _q, body in service.requests if p == "/incarnations"]
    assert set(incarnation) == {"principal", "epoch", "expected_revision"}
    assert incarnation["principal"] == joined["principal"]
    active = service.authority_state["frames"][FRAME]["active"]
    assert active["state"] == "active"
    assert active["authority"]["holder_identity"] == HOST_ID
    assert active["public_key"] == frame_key[1]
    assert service.principals[joined["principal"]].grant == "active"

    # The grant is ACTIVE as admitted (no renew is needed first), so the anchor resolves and
    # the default beat source renews it.
    anchor, renewed = _renew(service, monkeypatch)
    assert (anchor.principal, anchor.epoch) == (joined["principal"], joined["epoch"])
    assert renewed.outcome == "accepted"
    assert service.principals[joined["principal"]].renewed_at == NOW
    assert tripwires == []


def test_a_repeated_join_is_reported_not_rewritten(api_frame, service, tripwires):
    roster.save(config.hq_dir(), _manifest())
    assert _join().exit_code == 0
    writes = len(_calls(service))
    again = _join()
    assert again.exit_code == 0, again.output
    assert _result(again.output)["changed"] is False
    assert len(_calls(service)) == writes
    assert tripwires == []


def test_provision_push_joins_through_the_port(api_frame, service, tripwires):
    """``bh host provision --push``'s publish step on an api host is the join."""
    roster.save(config.hq_dir(), _manifest())
    assert hq.publish_host_manifest(config.hq_dir(), HOST_ID) is True
    assert service.authority_state["frames"][FRAME]["active"]["state"] == "active"
    assert tripwires == []


def test_operator_registers_and_the_frame_admits_itself(api_frame, service, tripwires, frame_key):
    """Caller-bound at the Gateway: each seat does the half it is bound for."""
    roster.save(config.hq_dir(), _manifest())
    service.caller = {"kind": "operator"}
    operator = _join("--frame", HOST_ID, "--public-key", f"{frame_key[0]}.pub")
    assert operator.exit_code == 0, operator.output
    registered = _result(operator.output)
    assert (registered["registered"], registered["admitted"]) == (True, False)
    assert service.authority_state["frames"] == {}

    service.caller = {"kind": "frame", "frame_id": FRAME, "host_id": HOST_ID}
    frame = _join()
    assert frame.exit_code == 0, frame.output
    admitted = _result(frame.output)
    assert (admitted["registered"], admitted["admitted"]) == (False, True)
    assert admitted["principal"] == registered["principal"]
    assert service.authority_state["frames"][FRAME]["active"]["state"] == "active"
    assert tripwires == []


def test_a_frame_with_no_registered_incarnation_is_told_who_registers_it(
    api_frame, service, tripwires
):
    roster.save(config.hq_dir(), _manifest())
    service.caller = {"kind": "frame", "frame_id": FRAME, "host_id": HOST_ID}
    result = _join()
    assert result.exit_code != 0
    assert "not registered with hq-service" in result.output
    assert f"--frame {HOST_ID}" in result.output
    assert service.authority_state["frames"] == {}
    assert tripwires == []


def test_join_refuses_an_sql_operator_binding_on_an_api_host(
    api_frame, service, tripwires, monkeypatch, tmp_path
):
    roster.save(config.hq_dir(), _manifest())
    monkeypatch.setenv("BH_HQ_OPERATOR_SETTINGS", str(tmp_path / "operator.yaml"))
    result = _join()
    assert result.exit_code != 0
    assert "hq.route is api" in result.output
    assert _calls(service) == [("PUT", f"/manifests/{HOST_ID}")]
    assert tripwires == []


def test_host_init_publishes_the_manifest_through_the_port(api_frame, service, tripwires):
    from beadhive.cli import app

    result = CliRunner().invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 0, result.output
    assert _calls(service) == [("PUT", f"/manifests/{HOST_ID}")]
    assert service.manifest_bodies[HOST_ID]["role"] == "executor"
    assert tripwires == []


def test_host_rm_withdraws_the_manifest_through_the_port(api_frame, service, tripwires):
    from beadhive.cli import app

    roster.save(config.hq_dir(), _manifest())
    result = CliRunner().invoke(app, ["host", "rm", HOST_ID, "--force", "--confirm"])
    assert result.exit_code == 0, result.output
    assert _calls(service)[-1] == ("DELETE", f"/manifests/{HOST_ID}")
    assert HOST_ID not in service.manifests
    assert tripwires == []


def _pin_config(service: FakeHqService) -> None:
    """Serve the config as pinned BEFORE any publish: a PUT manifest is not in it (bh-e4aou)."""
    pinned = _config(service)
    service.reads["/config"] = lambda: {**pinned, "authority": _authority(service)}


def test_host_init_reports_a_published_manifest_pending_a_rebind(api_frame, service, tripwires):
    from beadhive.cli import app

    _pin_config(service)
    result = CliRunner().invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 0, result.output
    assert _calls(service) == [("PUT", f"/manifests/{HOST_ID}")]
    assert "pending a rebind" in result.output
    assert "wrote" not in result.output
    assert tripwires == []


def test_host_init_reports_a_served_manifest(api_frame, service, tripwires):
    from beadhive.cli import app

    result = CliRunner().invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 0, result.output
    assert "the manifest is served" in result.output
    assert "pending" not in result.output


def test_host_rm_force_withdraws_a_published_but_unserved_manifest(api_frame, service, tripwires):
    from beadhive.cli import app

    _pin_config(service)
    roster.save(config.hq_dir(), _manifest())
    assert not roster.served(HOST_ID)
    result = CliRunner().invoke(app, ["host", "rm", HOST_ID, "--force", "--confirm"])
    assert result.exit_code == 0, result.output
    assert _calls(service)[-1] == ("DELETE", f"/manifests/{HOST_ID}")
    assert HOST_ID not in service.manifests
    assert tripwires == []


def test_host_rm_without_force_refuses_an_unserved_manifest_with_a_hint(
    api_frame, service, tripwires
):
    from beadhive.cli import app

    _pin_config(service)
    roster.save(config.hq_dir(), _manifest())
    result = CliRunner().invoke(app, ["host", "rm", HOST_ID, "--confirm"])
    assert result.exit_code == 1
    assert "--force" in result.output
    assert _calls(service) == [("PUT", f"/manifests/{HOST_ID}")]


def test_a_signed_api_fleet_has_no_registration_route(api_frame, service, tripwires):
    (api_frame / "state" / "hq-fleet-authority-mode.json").write_text(
        json.dumps({"mode": "signed"})
    )
    roster.save(config.hq_dir(), _manifest())
    with pytest.raises(hq_control_plane.HqPlaneUnavailable, match="publish_registration"):
        hq.publish_host_manifest(config.hq_dir(), HOST_ID)
    assert _calls(service) == [("PUT", f"/manifests/{HOST_ID}")]
    assert tripwires == []


# -- the route=api join with the signed manifest publication (hq-service v0.1.3, bh-hb75g) -----


def _serve_pinned(service: FakeHqService) -> None:
    """Serve what the authority pins, as hq-service does: a PUT manifest is committed but not
    served until an operator publication rebinds the authority onto it."""
    service.reads["/config"] = lambda: {
        **_config(service, service.served_manifests),
        "authority": _authority(service),
    }


def test_an_operator_join_of_an_unserved_manifest_names_the_publication(
    api_frame, service, tripwires, frame_key
):
    _serve_pinned(service)
    roster.save(config.hq_dir(), _manifest())
    service.caller = {"kind": "operator"}
    result = _join("--frame", HOST_ID, "--public-key", f"{frame_key[0]}.pub")
    assert result.exit_code == 1
    assert "hq-service does not serve it" in result.output
    assert f"bh hq authority publish-manifest --frame {HOST_ID}" in result.output
    assert ("POST", "/incarnations") not in _calls(service)
    assert tripwires == []


def test_a_fresh_frame_joins_after_publish_register_admit(api_frame, service, tripwires, frame_key):
    """J7-J10: the frame writes its manifest, the operator publishes it (and so rebinds), the
    operator registers the incarnation and the frame admits itself, with no separate rebind."""
    from beadhive.cli import app

    _serve_pinned(service)
    service.trusted = True  # a trusted fleet: the publication needs no operator signature
    local = roster.save(config.hq_dir(), _manifest())  # J7: what `bh host init` does
    assert local.is_file()  # the file the operator publishes
    assert not roster.served(HOST_ID)

    service.caller = {"kind": "operator"}
    base = ["hq", "authority", "publish-manifest", "--manifest", str(local)]
    plan = CliRunner().invoke(app, [*base, "--plan"])
    assert plan.exit_code == 0, plan.output
    assert _result(plan.output)["config_published"] is False  # the PUT committed it already
    assert not roster.served(HOST_ID)
    publish = CliRunner().invoke(app, [*base, "--confirm"])
    assert publish.exit_code == 0, publish.output
    assert _result(publish.output)["outcome"] == "accepted"
    assert roster.served(HOST_ID)

    register = _join("--frame", HOST_ID, "--public-key", f"{frame_key[0]}.pub")
    assert register.exit_code == 0, register.output
    assert _result(register.output)["registered"] is True

    service.caller = {"kind": "frame", "frame_id": FRAME, "host_id": HOST_ID}
    admit = _join()
    assert admit.exit_code == 0, admit.output
    assert _result(admit.output)["admitted"] is True
    assert service.authority_state["frames"][FRAME]["active"]["state"] == "active"
    assert [path for method, path in _calls(service) if "/rebind" in path] == []
    assert tripwires == []


def test_host_init_leaves_the_manifest_for_the_operator_and_names_the_publication(
    api_frame, service, tripwires
):
    from beadhive.cli import app

    _serve_pinned(service)
    result = CliRunner().invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 0, result.output
    assert "bh hq authority publish-manifest --manifest" in result.output
    local = roster.manifest_path(config.hq_dir(), HOST_ID)
    assert YAML(typ="safe").load(local.read_text())["host_id"] == HOST_ID
    assert tripwires == []
