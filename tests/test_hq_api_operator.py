"""The operator subset on ``hq.route: api`` (bh-lejfy): rebind, release-policy plan/apply and
the release catalog's verify and import, against the in-process fake of hq-service v0.1.0.

Nothing here may open SQL: ``no_sql`` makes every SQL opener fail the test."""

from __future__ import annotations

import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.harness.fake_hq_service import FakeHqService
from typer.testing import CliRunner

from beadhive import (
    hq_authority_enforce,
    hq_control_plane,
    hq_operator_settings,
    hq_release_catalog,
    hq_release_catalog_cli,
    hq_sql_transport,
)
from beadhive.cli import app
from beadhive.hq_release_catalog import CatalogEntry
from beadhive.hq_service_adapter import HqServiceAdapter

FRAME = "frame-b"
DIGEST = "sha256:" + "2" * 64
WHEEL = "8" * 64
COMMIT = "c" * 40


@pytest.fixture
def key(tmp_path):
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "operator"
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


def _policy_record(spec: str | None) -> dict:
    desired = {"release": {"id": "0.29.0", "digest": "sha256:" + "6" * 64}, "profile": "reviewed"}
    if spec:
        desired["release_policy"] = {"format": 1, "spec": spec, "catalog": "beadhive"}
    return {
        "revision": 2,
        "state": {"frames": {FRAME: {"active": {"desired": desired}, "candidate": None}}},
        "hive_policies": {},
    }


@pytest.fixture
def fake(key):
    service = FakeHqService(clock=lambda: 1793000000.0)
    service.operator_key = key[1]
    service.operator_record = _policy_record(">=0.29,==0.29.*")
    return service


@pytest.fixture
def plane(fake):
    client = httpx.Client(transport=fake.transport(), base_url="http://hq")
    return hq_control_plane.ApiControlPlane(HqServiceAdapter(client))


@pytest.fixture
def no_sql(monkeypatch):
    """Every way a CLI verb could reach SQL fails the test."""

    def refuse(*_args, **_kwargs):
        pytest.fail("an HQ SQL opener was constructed on the api path")

    monkeypatch.setattr(hq_control_plane.SqlControlPlane, "__init__", refuse)
    monkeypatch.setattr(hq_operator_settings, "operator_plane", refuse)
    monkeypatch.setattr(hq_release_catalog.ReleaseCatalogStore, "__init__", refuse)
    monkeypatch.setattr(hq_sql_transport, "connect", refuse)
    monkeypatch.setattr(hq_operator_settings, "release_catalog_store", refuse)


@pytest.fixture
def api_host(monkeypatch, plane, no_sql):
    """An operator host with ``hq.route: api`` whose plane is the fake service."""
    monkeypatch.setenv("BH_HQ_ROUTE", "api")
    monkeypatch.setattr(hq_control_plane, "api_control_plane", lambda _host: plane)
    monkeypatch.delenv(hq_operator_settings.ENV, raising=False)
    return plane


def _signed(monkeypatch, enforced: bool):
    monkeypatch.setattr(hq_authority_enforce, "enforced", lambda: enforced)


# -- rebind ------------------------------------------------------------------------------------


def test_rebind_signs_the_plans_record_and_applies(fake, plane, key, monkeypatch):
    _signed(monkeypatch, True)
    head = plane.renew(expected=fake.head, operator_key=key[0], duration=3600, frame=FRAME)
    assert head == fake.head
    plan, apply = fake.requests
    assert plan[:2] == ("POST", "/authority/rebind/plan")
    assert plan[3] == {"frame": FRAME, "duration_s": 3600}
    assert apply[1] == "/authority/rebind/apply" and apply[3]["signature"]
    assert apply[3]["record"] == fake.operator_record


def test_rebind_without_duration_or_frame_sends_an_empty_request(fake, plane, key, monkeypatch):
    _signed(monkeypatch, True)
    plane.renew(expected=fake.head, operator_key=key[0])
    assert fake.requests[0][3] == {}


def test_a_trusted_rebind_needs_no_key(fake, plane, monkeypatch):
    _signed(monkeypatch, False)
    fake.trusted = True
    plane.renew(expected=fake.head, operator_key="")
    assert "signature" not in fake.requests[1][3]


def test_a_signed_rebind_without_a_key_is_refused_before_any_request(fake, plane, monkeypatch):
    _signed(monkeypatch, True)
    with pytest.raises(ValueError, match="operator-key"):
        plane.renew(expected=fake.head, operator_key="")
    assert fake.requests == []


def test_a_stale_rebind_is_a_control_plane_refusal(fake, plane, key, monkeypatch):
    _signed(monkeypatch, True)
    with pytest.raises(hq_control_plane.ControlPlaneError, match="stale_revision"):
        plane.renew(expected="9" * 32, operator_key=key[0])
    assert len(fake.requests) == 1  # the plan; nothing was applied


# -- release-policy ----------------------------------------------------------------------------


def test_release_policy_plan_writes_nothing_and_reports_the_policy(fake, plane):
    result = plane.release_policy(FRAME, "plan", spec="^0.29")
    assert [r[1] for r in fake.requests] == [f"/authority/release-policy/{FRAME}/plan"]
    assert result["stored_spec"] == ">=0.29,==0.29.*"
    assert result["frame_id"] == FRAME and result["plan_sha256"].startswith("sha256:")
    assert result["epoch_advanced"] is False and result["revision"] == fake.head


def test_release_policy_apply_needs_the_reviewed_digest_and_confirm(fake, plane, key, monkeypatch):
    _signed(monkeypatch, True)
    digest = plane.release_policy(FRAME, "plan", spec="^0.29")["plan_sha256"]
    refusal = "exact reviewed plan digest"
    for kwargs in (
        {"plan_sha256": "sha256:other", "confirm": True, "operator_key": key[0]},
        {"plan_sha256": digest, "confirm": False, "operator_key": key[0]},
        {"plan_sha256": digest, "confirm": True, "operator_key": ""},
    ):
        with pytest.raises(hq_control_plane.ControlPlaneError, match=refusal):
            plane.release_policy(FRAME, "apply", spec="^0.29", **kwargs)
    assert all(r[1].endswith("/plan") for r in fake.requests)  # nothing was applied
    result = plane.release_policy(
        FRAME, "apply", spec="^0.29", plan_sha256=digest, confirm=True, operator_key=key[0]
    )
    assert result["revision"] == fake.head and result["plan_sha256"] == digest
    assert fake.requests[-1][1] == f"/authority/release-policy/{FRAME}/apply"
    assert fake.requests[-1][3]["spec"] == "^0.29" and fake.requests[-1][3]["signature"]


def test_a_dropped_policy_plans_as_none(fake, plane):
    fake.operator_record = _policy_record(None)
    result = plane.release_policy(FRAME, "plan", spec="none")
    assert result["release_policy"] is None and result["stored_spec"] is None


def test_the_plan_digest_ignores_the_plans_timestamps(fake, plane):
    first = plane.release_policy(FRAME, "plan", spec="^0.29")["plan_sha256"]
    fake.operator_record["revision"] = 3  # the plan's record moved on; the policy did not
    assert plane.release_policy(FRAME, "plan", spec="^0.29")["plan_sha256"] == first


# -- release catalog ---------------------------------------------------------------------------


def _entry(version="0.29.1", digest=DIGEST, bundle=b"{}"):
    return CatalogEntry(version, digest, "beadhive-tree-sha256-v1", WHEEL, COMMIT, bundle)


@pytest.fixture
def store(fake, plane):
    from beadhive.hq_release_catalog_api import ApiReleaseCatalogStore

    return ApiReleaseCatalogStore(plane.port, operator_key=None)


def test_catalog_import_signs_under_the_catalog_domain(fake, plane, key, monkeypatch):
    from beadhive.hq_release_catalog_api import ApiReleaseCatalogStore

    store = ApiReleaseCatalogStore(plane.port, operator_key=key[0])
    assert store.add(_entry()) == "inserted"
    plan, apply = [r for r in fake.requests if r[0] == "POST"]
    assert plan[1] == "/release-catalog/beadhive/import/plan"
    assert set(plan[3]) == {"version", "measured_digest", "wheel_sha256", "commit", "bundle"}
    assert set(apply[3]) == {*plan[3], "expected_revision", "issued_at", "signature"}
    assert [r.version for r in store.rows()] == ["0.29.1"]
    assert store.add(_entry()) == "unchanged"  # already active: no second import


def test_catalog_import_signed_with_the_wrong_key_is_refused(fake, plane, tmp_path, key):
    from beadhive.hq_release_catalog_api import ApiReleaseCatalogStore

    other = Ed25519PrivateKey.generate()
    path = tmp_path / "other"
    path.write_bytes(
        other.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(hq_release_catalog.CatalogError, match="operator_signature_invalid"):
        ApiReleaseCatalogStore(plane.port, operator_key=str(path)).add(_entry())
    assert fake.catalog_rows == []


def test_catalog_import_needs_a_key(store, fake):
    with pytest.raises(ValueError, match="operator-key"):
        store.add(_entry())
    assert fake.catalog_rows == []


def test_a_second_active_digest_for_a_version_is_a_conflict_before_the_service(fake, plane, key):
    from beadhive.hq_release_catalog_api import ApiReleaseCatalogStore

    store = ApiReleaseCatalogStore(plane.port, operator_key=key[0])
    store.add(_entry())
    posts = len([r for r in fake.requests if r[0] == "POST"])
    with pytest.raises(hq_release_catalog.CatalogConflict):
        store.add(_entry(digest="sha256:" + "3" * 64))
    assert len([r for r in fake.requests if r[0] == "POST"]) == posts


def test_revoke_is_not_routed(store):
    with pytest.raises(hq_release_catalog.CatalogError, match="0.41.0"):
        store.revoke("0.29.1", "bad")


class _Verifier:
    def verify(self, bundle, *, identity, issuer, trusted_root=None):
        raise AssertionError("verify_rows must call _reverify, patched below")


def test_catalog_verify_re_verifies_each_active_row(fake, plane, key, store, monkeypatch):
    from beadhive.hq_release_catalog_api import ApiReleaseCatalogStore

    ApiReleaseCatalogStore(plane.port, operator_key=key[0]).add(_entry())
    monkeypatch.setattr(hq_release_catalog, "_reverify", lambda row, *_a: None)
    result = hq_release_catalog.verify_rows(store=store, verifier=_Verifier())
    assert result == {
        "verified": 1,
        "failed": 0,
        "entries": [{"version": "0.29.1", "status": "active", "ok": True, "error": None}],
    }


# -- the CLI on an api host: the port, never SQL -----------------------------------------------


def _run(*args):
    return CliRunner().invoke(app, list(args))


def test_cli_rebind_uses_the_port(api_host, fake, key, monkeypatch):
    _signed(monkeypatch, True)
    run = _run(
        "hq", "authority", "rebind", "--frame", FRAME, "--confirm", "--operator-key", key[0],
        "--expected-revision", fake.head, "--duration", "1h",
    )  # fmt: skip
    assert run.exit_code == 0, run.output
    assert json.loads(run.stdout) == {"revision": fake.head}
    plan = next(r for r in fake.requests if r[1] == "/authority/rebind/plan")
    assert plan[3] == {"frame": FRAME, "duration_s": 3600}


def test_cli_release_policy_plan_then_apply_uses_the_port(api_host, fake, key, monkeypatch):
    _signed(monkeypatch, True)
    base = ["hq", "authority", "release-policy"]
    plan = _run(*base, "plan", "--frame", FRAME, "--spec", "^0.29")
    assert plan.exit_code == 0, plan.output
    digest = json.loads(plan.stdout)["plan_sha256"]
    run = _run(
        *base, "apply", "--frame", FRAME, "--spec", "^0.29", "--plan-sha256", digest,
        "--confirm", "--operator-key", key[0],
    )  # fmt: skip
    assert run.exit_code == 0, run.output
    assert json.loads(run.stdout)["revision"] == fake.head


def test_cli_a_refused_apply_exits_one_with_the_service_code(api_host, fake, key, monkeypatch):
    _signed(monkeypatch, True)
    run = _run(
        "hq", "authority", "rebind", "--confirm", "--operator-key", key[0],
        "--expected-revision", "9" * 32,
    )  # fmt: skip
    assert run.exit_code == 1 and "stale_revision" in run.output


def test_cli_an_operator_settings_file_never_selects_the_sql_plane_on_api(
    api_host, fake, key, monkeypatch, tmp_path
):
    _signed(monkeypatch, True)
    settings = tmp_path / "operator.json"
    settings.write_text("{}")
    run = _run(
        "hq", "authority", "rebind", "--confirm", "--operator-key", key[0],
        "--expected-revision", fake.head, "--operator-settings", str(settings),
    )  # fmt: skip
    assert run.exit_code == 0, run.output


def test_cli_catalog_import_and_verify_use_the_port(api_host, fake, key, monkeypatch):
    entry = _entry()

    def import_release(version, *, source, verifier, store, policy=None, trusted_root=None):
        assert version == "0.29.1"
        return {"version": version, "result": store.add(entry)}

    monkeypatch.setattr(hq_release_catalog, "import_release", import_release)
    monkeypatch.setattr(hq_release_catalog_cli, "_verifier", lambda: _Verifier())
    monkeypatch.setattr(hq_release_catalog, "_reverify", lambda row, *_a: None)
    monkeypatch.setenv(hq_release_catalog_cli.OPERATOR_KEY_ENV, key[0])
    run = _run("hq", "release-catalog", "import", "0.29.1")
    assert run.exit_code == 0, run.output
    assert json.loads(run.stdout)["result"] == "inserted"
    verify = _run("hq", "release-catalog", "verify", "--all")
    assert verify.exit_code == 0, verify.output
    assert json.loads(verify.stdout)["verified"] == 1


def test_cli_catalog_revoke_and_provision_are_refused_on_api(api_host):
    revoke = _run("hq", "release-catalog", "revoke", "0.29.1", "--reason", "bad")
    assert revoke.exit_code == 1 and "0.41.0" in revoke.output
    provision = _run("hq", "release-catalog", "provision")
    assert provision.exit_code == 1 and "server-local" in provision.output


def test_cli_catalog_import_on_api_needs_the_operator_key_env(api_host, monkeypatch):
    monkeypatch.delenv(hq_release_catalog_cli.OPERATOR_KEY_ENV, raising=False)
    run = _run("hq", "release-catalog", "import", "0.29.1")
    assert run.exit_code == 1 and hq_release_catalog_cli.OPERATOR_KEY_ENV in run.output


def test_the_sql_route_keeps_its_stores(monkeypatch):
    monkeypatch.setenv("BH_HQ_ROUTE", "sql")
    sentinel = object()
    monkeypatch.setattr(hq_operator_settings, "release_catalog_store", lambda _p=None: sentinel)
    assert hq_release_catalog_cli._store(None) is sentinel
    assert hq_release_catalog_cli._api_route() is False


# -- rebind --plan and the signed manifest publication (hq-service v0.1.3, bh-hb75g) -----------

HOST = "host-n"
MANIFEST = {
    "host_id": HOST,
    "label": HOST,
    "os": "linux",
    "arch": "x86_64",
    "role": "executor",
    "identity": {"kind": "none", "value": ""},
}


def _posts(fake):
    return [(r[1], r[3]) for r in fake.requests if r[0] == "POST"]


def test_rebind_plan_writes_nothing_and_needs_no_key(fake, plane, monkeypatch):
    _signed(monkeypatch, True)
    head = fake.head
    result = plane.rebind_plan(frame=FRAME, duration=3600)
    assert _posts(fake) == [("/authority/rebind/plan", {"frame": FRAME, "duration_s": 3600})]
    assert fake.head == head  # nothing was applied
    assert result["plan"] is True and result["revision"] == head
    assert result["frame"] == FRAME and result["signature_needed"] is True
    assert result["config_head"] == fake.config_head


def test_rebind_plan_at_another_head_is_stale(fake, plane):
    with pytest.raises(hq_control_plane.ControlPlaneError, match="stale_revision"):
        plane.rebind_plan(expected="9" * 32)
    assert [p for p, _ in _posts(fake)] == ["/authority/rebind/plan"]


def test_manifest_publish_plan_only_writes_nothing(fake, plane):
    head, config_head = fake.head, fake.config_head
    result = plane.operator_publish_manifest(MANIFEST, plan_only=True)
    assert _posts(fake) == [(f"/manifests/{HOST}/publish/plan", {"manifest": MANIFEST})]
    assert (fake.head, fake.config_head, fake.manifests) == (head, config_head, set())
    assert result["plan"] is True and result["host_id"] == HOST
    assert result["path"] == f"hosts/{HOST}.yaml"
    assert result["manifest_sha256"] and result["config_published"] is True
    assert result["config_head"] == config_head
    assert result["config_head_after"] not in (None, config_head)
    assert result["config_moved"] is True


def test_manifest_publish_signs_the_plan_and_echoes_it_in_the_apply(fake, plane, key, monkeypatch):
    from beadhive.hq_sql_signatures import verify_authority

    _signed(monkeypatch, True)
    plans = _intercept(fake, plane)
    result = plane.operator_publish_manifest(
        MANIFEST, confirm=True, operator_key=key[0], duration=3600
    )
    (plan_path, plan_body), (apply_path, apply_body) = _posts(fake)
    assert plan_path.endswith("/publish/plan") and apply_path.endswith("/publish/apply")
    assert plan_body == {"manifest": MANIFEST, "duration_s": 3600}
    (plan,) = plans
    assert apply_body == {
        **plan_body,
        "expected_revision": plan["revision"],
        "config_head": plan["config_head"],
        "issued_at": plan["issued_at"],
        "expires_at": plan["expires_at"],
        "record": plan["record"],
        "signature": apply_body["signature"],
    }
    verify_authority(plan["record"], apply_body["signature"], granted_public_key=key[1])
    assert apply_body["record"]["config_head"] == plan["config_head_after"]
    assert result["outcome"] == "accepted" and result["revision"] == fake.head
    assert result["config_published"] is True and result["sends"] == 1
    assert result["config_head"] == fake.config_head == plan["config_head_after"]
    assert fake.served_manifests == {HOST: MANIFEST}


def test_a_trusted_manifest_publish_sends_no_signature(fake, plane, monkeypatch):
    _signed(monkeypatch, False)
    fake.trusted = True
    result = plane.operator_publish_manifest(MANIFEST, confirm=True)
    apply_body = _posts(fake)[-1][1]
    assert "signature" not in apply_body
    assert result["outcome"] == "accepted"


def test_an_already_published_manifest_only_rebinds(fake, plane, key, monkeypatch):
    _signed(monkeypatch, True)
    fake.manifests.add(HOST)
    fake.manifest_bodies[HOST] = MANIFEST  # committed by the host's PUT, not yet served
    config_head = fake.config_head
    plan = plane.operator_publish_manifest(MANIFEST, plan_only=True)
    assert plan["config_published"] is False and plan["config_head_after"] == config_head
    result = plane.operator_publish_manifest(MANIFEST, confirm=True, operator_key=key[0])
    assert result["config_published"] is False and fake.config_head == config_head
    assert fake.served_manifests == {HOST: MANIFEST}


def test_a_publication_that_stops_between_its_commits_is_completed_by_the_same_apply(
    fake, plane, key, monkeypatch
):
    _signed(monkeypatch, True)
    fake.crash_between_commits = 1
    result = plane.operator_publish_manifest(MANIFEST, confirm=True, operator_key=key[0])
    applies = [b for p, b in _posts(fake) if p.endswith("/apply")]
    assert len(applies) == 2 and applies[0] == applies[1]  # the SAME apply, byte for byte
    assert result["sends"] == 2 and result["outcome"] == "accepted"
    assert result["config_published"] is False  # the retry found the config already committed
    assert fake.served_manifests == {HOST: MANIFEST}


def test_a_publication_that_keeps_failing_says_to_run_it_again_and_a_new_plan_recovers(
    fake, plane, key, monkeypatch
):
    from beadhive import hq_api_operator

    _signed(monkeypatch, True)
    fake.crash_between_commits = hq_api_operator.MANIFEST_RESENDS + 1
    with pytest.raises(hq_control_plane.ControlPlaneError, match="Run this command again"):
        plane.operator_publish_manifest(MANIFEST, confirm=True, operator_key=key[0])
    sent = len([p for p, _ in _posts(fake) if p.endswith("/apply")])
    assert sent == hq_api_operator.MANIFEST_RESENDS + 1
    assert fake.served_manifests == {}  # the config holds it; the authority does not pin it yet
    replan = plane.operator_publish_manifest(MANIFEST, plan_only=True)
    assert replan["config_published"] is False  # only the rebind is left
    result = plane.operator_publish_manifest(MANIFEST, confirm=True, operator_key=key[0])
    assert result["outcome"] == "accepted" and fake.served_manifests == {HOST: MANIFEST}


def test_a_repeated_identical_apply_is_a_replay(fake, plane, key, monkeypatch):
    from beadhive import hq_service_adapter as adapter_module

    _signed(monkeypatch, True)
    plane.operator_publish_manifest(MANIFEST, confirm=True, operator_key=key[0])
    body = _posts(fake)[-1][1]
    answer = plane._port_call("apply_manifest_publish", HOST, body)
    assert answer["outcome"] == "replayed"
    assert adapter_module.operator_apply_body  # the body builder the client sends with


def test_manifest_publish_needs_confirm_and_a_key_before_any_request(fake, plane, monkeypatch):
    _signed(monkeypatch, True)
    with pytest.raises(hq_control_plane.ControlPlaneError, match="--confirm"):
        plane.operator_publish_manifest(MANIFEST)
    with pytest.raises(hq_control_plane.ControlPlaneError, match="operator-key"):
        plane.operator_publish_manifest(MANIFEST, confirm=True)
    assert fake.requests == []


OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHj/j/YOvfch7uS6hONz0fFlIB+ekYf4wWuBWPRDasAA"


@pytest.mark.parametrize(
    ("field", "value", "code", "hint"),
    [
        ("operator_apply", False, "operator_apply_disabled", "-operator-apply"),
        ("operator_key", OTHER_KEY, "operator_signature_invalid", "--operator-key"),
    ],
)
def test_manifest_publish_refusals_say_what_to_do(
    fake, plane, key, monkeypatch, field, value, code, hint
):
    _signed(monkeypatch, True)
    setattr(fake, field, value)
    with pytest.raises(hq_control_plane.ControlPlaneError) as caught:
        plane.operator_publish_manifest(MANIFEST, confirm=True, operator_key=key[0])
    assert code in str(caught.value) and hint in str(caught.value)
    assert len([p for p, _ in _posts(fake) if p.endswith("/apply")]) == 1  # refused: no resend
    assert fake.served_manifests == {}


def _intercept(fake, plane, apply_response=None, after_plan=None):
    """Wrap the fake: record each plan it answers (returned), run ``after_plan`` once a plan is
    answered, and answer applies with ``apply_response`` instead of the fake."""
    handle, plans = fake.handle, []

    def wrapped(request):
        if apply_response is not None and request.url.path.endswith("/publish/apply"):
            fake.requests.append(("POST", request.url.path, "", json.loads(request.content)))
            return apply_response
        response = handle(request)
        if request.url.path.endswith("/publish/plan"):
            plans.append(json.loads(response.content))
            if after_plan is not None:
                after_plan()
        return response

    _route(plane, wrapped)
    return plans


def _route(plane, handler):
    """Point the plane's adapter at ``handler`` (the fixture bound the fake's own)."""
    plane.port._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://hq")


def test_a_head_that_moves_after_the_plan_is_stale_and_not_resent(fake, plane, key, monkeypatch):
    _signed(monkeypatch, True)
    _intercept(fake, plane, after_plan=lambda: setattr(fake, "head", "d" * 32))
    with pytest.raises(hq_control_plane.ControlPlaneError) as caught:
        plane.operator_publish_manifest(MANIFEST, confirm=True, operator_key=key[0])
    assert "stale_revision" in str(caught.value) and "plan afresh" in str(caught.value)
    assert len([p for p, _ in _posts(fake) if p.endswith("/apply")]) == 1
    assert fake.manifests == set()


def test_committed_unreadable_is_a_refusal_to_plan_again_not_a_resend(
    fake, plane, key, monkeypatch
):
    _signed(monkeypatch, True)
    error = {"error": {"code": "committed_unreadable", "message": "plan again", "retryable": False}}
    _intercept(fake, plane, apply_response=httpx.Response(500, json=error))
    with pytest.raises(hq_control_plane.ControlPlaneError) as caught:
        plane.operator_publish_manifest(MANIFEST, confirm=True, operator_key=key[0])
    assert "committed_unreadable" in str(caught.value) and "plan afresh" in str(caught.value)
    assert len([p for p, _ in _posts(fake) if p.endswith("/apply")]) == 1


def test_an_older_service_without_the_route_is_named(fake, plane, monkeypatch):
    def old(request):
        fake.requests.append((request.method, request.url.path, "", None))
        return httpx.Response(404, json={"error": "not found"})

    _route(plane, old)
    with pytest.raises(hq_control_plane.ControlPlaneError, match="v0.1.3"):
        plane.operator_publish_manifest(MANIFEST, plan_only=True)


def _manifest_file(tmp_path, body=MANIFEST):
    from ruamel.yaml import YAML

    path = tmp_path / f"{body['host_id']}.yaml"
    with path.open("w") as stream:
        YAML().dump(dict(body), stream)
    return path


def test_cli_rebind_plan_prints_the_plan_without_confirm_or_key(api_host, fake, monkeypatch):
    _signed(monkeypatch, True)
    head = fake.head
    run = _run("hq", "authority", "rebind", "--plan", "--frame", FRAME)
    assert run.exit_code == 0, run.output
    result = json.loads(run.stdout)
    assert result["plan"] is True and result["revision"] == head and result["frame"] == FRAME
    assert [p for p, _ in _posts(fake)] == ["/authority/rebind/plan"] and fake.head == head


def test_cli_plan_is_refused_on_other_actions(api_host):
    run = _run("hq", "authority", "status", "--plan")
    assert run.exit_code == 2 and "--plan applies to" in run.output


def test_cli_publish_manifest_plan_then_confirm(api_host, fake, key, monkeypatch, tmp_path):
    _signed(monkeypatch, True)
    path = _manifest_file(tmp_path)
    plan = _run("hq", "authority", "publish-manifest", "--manifest", str(path), "--plan")
    assert plan.exit_code == 0, plan.output
    planned = json.loads(plan.stdout)
    assert planned["plan"] is True and planned["manifest_sha256"]
    assert planned["config_head_after"] and fake.served_manifests == {}
    run = _run(
        "hq", "authority", "publish-manifest", "--manifest", str(path), "--frame", HOST,
        "--confirm", "--operator-key", key[0],
    )  # fmt: skip
    assert run.exit_code == 0, run.output
    applied = json.loads(run.stdout)
    assert applied["outcome"] == "accepted" and applied["revision"] == fake.head
    assert applied["config_head"] == planned["config_head_after"]
    served = fake.served_manifests[HOST]  # the manifest as `bh host init` dumps it
    assert {name: served[name] for name in MANIFEST} == MANIFEST


def test_cli_publish_manifest_refuses_a_manifest_of_another_host(api_host, fake, tmp_path):
    path = _manifest_file(tmp_path)
    run = _run("hq", "authority", "publish-manifest", "--manifest", str(path), "--frame", "x-1")
    assert run.exit_code == 1 and "not --frame x-1" in run.output
    assert _posts(fake) == []


def test_cli_publish_manifest_needs_the_api_route(monkeypatch, tmp_path):
    class SqlPlane:
        pass

    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda _settings=None: SqlPlane())
    run = _run("hq", "authority", "publish-manifest", "--manifest", str(_manifest_file(tmp_path)))
    assert run.exit_code == 1 and "needs hq.route api" in run.output
    rebind = _run("hq", "authority", "rebind", "--plan")
    assert rebind.exit_code == 1 and "needs hq.route api" in rebind.output
