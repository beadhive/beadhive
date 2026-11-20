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
