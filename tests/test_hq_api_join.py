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


# -- Gateway v0.1.1 enforces the principal kind itself: a bare 403 (bh-garne) ------------------

OPERATOR = {"kind": "operator"}
FRAME_CALLER = {"kind": "frame", "frame_id": FRAME, "host_id": HOST_ID}


def test_through_the_gateway_each_seat_skips_the_half_it_is_not_bound_for(
    api_frame, service, tripwires, frame_key
):
    """The Gateway answers the wrong seat's half with a bare 403 before hq-service sees it:
    the operator's admit and the frame's register are skipped, not refused."""
    roster.save(config.hq_dir(), _manifest())
    service.gateway_kinds = True
    service.caller = OPERATOR
    operator = _join("--frame", HOST_ID, "--public-key", f"{frame_key[0]}.pub")
    assert operator.exit_code == 0, operator.output
    registered = _result(operator.output)
    assert (registered["registered"], registered["admitted"]) == (True, False)
    assert registered["principal"].startswith("frame_")
    assert ("POST", f"/admissions/{FRAME}") in _calls(service)  # probed, Gateway-refused
    assert service.authority_state["frames"] == {}

    service.caller = FRAME_CALLER
    frame = _join()
    assert frame.exit_code == 0, frame.output
    admitted = _result(frame.output)
    assert (admitted["registered"], admitted["admitted"]) == (False, True)
    assert admitted["principal"] == registered["principal"]
    assert service.authority_state["frames"][FRAME]["active"]["state"] == "active"
    assert tripwires == []


def test_an_operator_joining_another_host_only_registers(api_frame, service, tripwires, frame_key):
    """--frame naming a host other than this one is the operator's half alone: no admit."""
    (api_frame / "host.yaml").write_text("host_id: operator-box\nlabel: operator\n")
    roster.save(config.hq_dir(), _manifest())
    service.gateway_kinds = True
    service.caller = OPERATOR
    result = _join("--frame", HOST_ID, "--public-key", f"{frame_key[0]}.pub")
    assert result.exit_code == 0, result.output
    joined = _result(result.output)
    assert (joined["registered"], joined["admitted"]) == (True, False)
    assert ("POST", "/incarnations") in _calls(service)
    assert not [path for _method, path in _calls(service) if path.startswith("/admissions/")]
    assert tripwires == []


def test_an_operator_refused_its_own_registration_fails(api_frame, service, tripwires, frame_key):
    """The seat is known (another host), so a Gateway 403 on the register half is not skipped."""
    (api_frame / "host.yaml").write_text("host_id: operator-box\nlabel: operator\n")
    roster.save(config.hq_dir(), _manifest())
    service.gateway_kinds = True
    service.caller = FRAME_CALLER  # a frame credential is not the operator
    result = _join("--frame", HOST_ID, "--public-key", f"{frame_key[0]}.pub")
    assert result.exit_code == 1
    assert "principal is not bound to this route" in result.output
    assert not [path for _method, path in _calls(service) if path.startswith("/admissions/")]
    assert tripwires == []


@pytest.mark.parametrize(
    "reason",
    [
        "a worker may act only on its own frame",
        "the caller is bound to another host than the body's host_id",
    ],
)
def test_a_gateway_403_on_the_frames_own_admit_fails(
    api_frame, service, tripwires, frame_key, reason
):
    roster.save(config.hq_dir(), _manifest())
    service.gateway_kinds = True
    service.caller = OPERATOR
    assert _join("--frame", HOST_ID, "--public-key", f"{frame_key[0]}.pub").exit_code == 0

    service.caller = FRAME_CALLER
    service.gateway_refusals[("POST", f"/admissions/{FRAME}")] = reason
    result = _join()
    assert result.exit_code == 1
    assert reason in result.output
    assert service.authority_state["frames"] == {}
    assert tripwires == []


def test_a_gateway_403_on_the_operators_admit_probe_other_than_wrong_seat_fails(
    api_frame, service, tripwires, frame_key
):
    roster.save(config.hq_dir(), _manifest())
    service.caller = OPERATOR
    reason = "a worker may act only on its own frame"
    service.gateway_refusals[("POST", f"/admissions/{FRAME}")] = reason
    result = _join("--frame", HOST_ID, "--public-key", f"{frame_key[0]}.pub")
    assert result.exit_code == 1
    assert reason in result.output
    assert tripwires == []


def test_a_credential_bound_for_neither_half_is_refused(api_frame, service, tripwires):
    roster.save(config.hq_dir(), _manifest())
    service.gateway_kinds = True
    service.caller = {"kind": "host", "host_id": HOST_ID}
    result = _join()
    assert result.exit_code == 1
    assert "bound for neither half of the join" in result.output
    assert service.incarnations == set()
    assert service.authority_state["frames"] == {}
    assert tripwires == []


# -- the signed admission over an operator-signed authority (hq-service v0.1.4, bh-j7pdz) ------


@pytest.fixture
def operator_key(tmp_path):
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "operator_ed25519"
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


class _Clock:
    """The one mocked clock both the fake service and both seats read (no sleeping)."""

    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(service, monkeypatch):
    from beadhive import hq_admission_signing

    tick = _Clock()
    service._clock = tick
    monkeypatch.setattr(hq_admission_signing, "now", tick)
    return tick


@pytest.fixture
def signed_fleet(api_frame, service, operator_key, clock, frame_key):
    """A trusted fleet whose authority is operator-signed, the manifest committed and the
    incarnation registered by the operator, through the Gateway's kind enforcement."""
    service.trusted = True
    service.authority_signed = True
    service.operator_key = operator_key[1]
    service.gateway_kinds = True
    roster.save(config.hq_dir(), _manifest())
    service.caller = OPERATOR
    register = _join("--frame", HOST_ID, "--public-key", f"{frame_key[0]}.pub")
    assert register.exit_code == 0, register.output
    service.caller = FRAME_CALLER
    return api_frame


def _handoff() -> Path:
    """The frame's default hand-off file (``$BH_HOME/state``)."""
    from beadhive import hq_admission_signing

    return hq_admission_signing.default_path(HOST_ID, config.home() / "state")


def _admissions(service: FakeHqService) -> list[dict]:
    return [body for m, p, _q, body in service.requests if p == f"/admissions/{FRAME}"]


def _sign(record: Path, operator_key: str | None, *extra: str):
    from beadhive.cli import app

    args = ["hq", "authority", "sign-admission", "--record", str(record), *extra]
    if operator_key:
        args += ["--operator-key", operator_key]
    return CliRunner().invoke(app, args)


def test_over_a_signed_authority_the_frame_plans_the_operator_signs_and_the_frame_admits(
    signed_fleet, service, tripwires, frame_key, operator_key, tmp_path
):
    from beadhive.hq_sql_signatures import verify_authority

    probed = len(_admissions(service))  # the operator's join probed the frame's route
    pending = _join()
    assert pending.exit_code == 0, pending.output
    planned = _result(pending.output)
    assert (planned["admitted"], planned["signature_needed"]) == (False, True)
    handoff = _handoff()
    assert planned["admission_plan"] == str(handoff) and handoff.is_file()
    assert "sign-admission" in planned["next"]
    unsigned, plan = _admissions(service)[probed:]
    assert "plan" not in unsigned and plan["plan"] is True  # refused unsigned, then planned
    assert service.authority_state["frames"] == {}

    # The operator works on its own copy and calls no write route: the admission is the frame's.
    copy = tmp_path / "operator-copy.json"
    copy.write_text(handoff.read_text())
    service.caller = OPERATOR
    writes = len(_calls(service))
    preview = _sign(copy, None)
    assert preview.exit_code == 0, preview.output
    assert _result(preview.output)["plan"] is True
    assert "signature" not in json.loads(copy.read_text())
    signed = _sign(copy, operator_key[0], "--confirm")
    assert signed.exit_code == 0, signed.output
    assert _result(signed.output)["signed"] is True
    assert len(_calls(service)) == writes
    record = json.loads(copy.read_text())
    verify_authority(
        record["plan"]["record"], record["signature"], granted_public_key=operator_key[1]
    )

    handoff.write_text(copy.read_text())
    service.caller = FRAME_CALLER
    admit = _join()
    assert admit.exit_code == 0, admit.output
    admitted = _result(admit.output)
    assert (admitted["admitted"], admitted["signed"]) == (True, True)
    apply = _admissions(service)[-1]
    assert apply["signature"] == record["signature"]
    assert apply["issued_at"] == NOW and apply["config_head"] == service.config_head
    assert apply["expires_at"] == record["plan"]["expires_at"]
    assert service.authority_signed is True  # the served authority stays operator-signed
    assert service.authority_state["frames"][FRAME]["active"]["public_key"] == frame_key[1]
    assert not handoff.exists()
    assert tripwires == []


def test_a_pending_plan_is_reported_again_not_replanned(signed_fleet, service, tripwires):
    assert _join().exit_code == 0
    planned = len(_admissions(service))
    again = _join()
    assert again.exit_code == 0, again.output
    assert _result(again.output)["signature_needed"] is True
    assert len(_admissions(service)) == planned
    assert tripwires == []


def test_a_signed_plan_applied_after_its_window_is_void_and_the_next_join_plans_afresh(
    signed_fleet, service, tripwires, operator_key, clock
):

    assert _join().exit_code == 0
    handoff = _handoff()
    assert _sign(handoff, operator_key[0], "--confirm").exit_code == 0
    clock.now += 901
    void = _join()
    assert void.exit_code == 1
    assert "plan_expired" in void.output and "plan afresh" in void.output
    assert not handoff.exists()
    assert service.authority_state["frames"] == {}
    replanned = _join()
    assert replanned.exit_code == 0, replanned.output
    assert _result(replanned.output)["issued_at"] == clock.now
    assert tripwires == []


def test_a_signature_hq_service_does_not_verify_names_its_operator_key(
    signed_fleet, service, tripwires, frame_key, tmp_path
):

    assert _join().exit_code == 0
    handoff = _handoff()
    assert _sign(handoff, frame_key[0], "--confirm").exit_code == 0  # not the operator's key
    refused = _join()
    assert refused.exit_code == 1
    assert "operator_signature_invalid" in refused.output and "-operator-key" in refused.output
    assert not handoff.exists()
    assert service.authority_state["frames"] == {}
    assert tripwires == []


def test_the_operator_refuses_to_sign_a_plan_that_grants_more_than_the_frame(
    signed_fleet, service, tripwires, operator_key
):
    import base64

    from beadhive.hq_sql_signatures import AUTHORITY_DOMAIN, canonical

    assert _join().exit_code == 0
    handoff = _handoff()
    original = json.loads(handoff.read_text())

    tampered = json.loads(json.dumps(original))
    tampered["plan"]["record"]["state"]["frames"]["frame-x"] = {"active": None}
    handoff.write_text(json.dumps(tampered))
    refused = _sign(handoff, operator_key[0], "--confirm")
    assert refused.exit_code == 1 and "signing_input is not its record" in refused.output

    record = tampered["plan"]["record"]
    tampered["plan"]["signing_input"] = base64.b64encode(
        AUTHORITY_DOMAIN + canonical(record)
    ).decode()
    handoff.write_text(json.dumps(tampered))
    refused = _sign(handoff, operator_key[0], "--confirm")
    assert refused.exit_code == 1 and "changes frames other than" in refused.output
    assert "signature" not in json.loads(handoff.read_text())

    rekeyed = json.loads(json.dumps(original))
    rekeyed["public_key"] = operator_key[1]
    handoff.write_text(json.dumps(rekeyed))
    refused = _sign(handoff, operator_key[0], "--confirm")
    assert refused.exit_code == 1 and "not the hand-off's frame and key" in refused.output
    assert tripwires == []


def test_the_operator_refuses_a_plan_the_authority_moved_past(
    signed_fleet, service, tripwires, operator_key
):

    assert _join().exit_code == 0
    service.head = "f" * 32  # another publication landed since the frame planned
    refused = _sign(_handoff(), operator_key[0], "--confirm")
    assert refused.exit_code == 1
    assert "authority moved since the frame planned" in refused.output
    assert "bh hq authority join --confirm" in refused.output
    assert tripwires == []


def test_sign_admission_without_a_record_or_key_is_refused(
    signed_fleet, service, tripwires, operator_key, tmp_path
):

    missing = _sign(tmp_path / "absent.json", operator_key[0], "--confirm")
    assert missing.exit_code == 1 and "no admission plan" in missing.output
    assert _join().exit_code == 0
    keyless = _sign(_handoff(), None, "--confirm")
    assert keyless.exit_code == 1 and "--operator-key" in keyless.output
    assert tripwires == []


def test_a_signed_service_refuses_even_the_plan_and_names_the_trusted_fleet(
    signed_fleet, service, tripwires
):
    reason = (
        "this service publishes unsigned authority only in trusted mode; a signed fleet's "
        "admission needs the operator's signature over the new record"
    )
    service.refusals[("POST", f"/admissions/{FRAME}")] = ("operator_signature_required", reason)
    refused = _join()
    assert refused.exit_code == 1
    assert "operator_signature_required" in refused.output
    assert "hq.default_authority_mode: trusted" in refused.output
    assert tripwires == []


def test_an_unsigned_trusted_authority_still_admits_with_no_hand_off(
    api_frame, service, tripwires, clock
):
    """A key-less trusted fleet (authority unsigned) is unchanged: one unsigned admission."""

    service.trusted = True
    roster.save(config.hq_dir(), _manifest())
    result = _join()
    assert result.exit_code == 0, result.output
    assert _result(result.output)["admitted"] is True
    assert [("plan" in body) for body in _admissions(service)] == [False]  # no plan
    assert not _handoff().exists()
    assert tripwires == []


def test_provision_push_over_a_signed_authority_names_the_join_that_plans(
    signed_fleet, service, tripwires
):
    """A join with no hand-off (``bh host provision --push``) cannot plan for the operator: it
    is refused naming the command that does."""
    with pytest.raises(hq_control_plane.ControlPlaneError, match="bh hq authority join --confirm"):
        hq.publish_host_manifest(config.hq_dir(), HOST_ID)
    assert not _handoff().exists()
    assert service.authority_state["frames"] == {}
    assert tripwires == []
