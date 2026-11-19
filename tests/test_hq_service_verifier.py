"""Contract tests: api authority/config/eligibility reads verify signature and floor (bh-2r7qd)."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import hq_authority_enforce
from beadhive.hq_port import HqRefused, HqUnavailable
from beadhive.hq_service_adapter import PREFIX, select_hq_port
from beadhive.hq_sql_signatures import sign_authority

_REAL_CLIENT = httpx.Client
FLOOR = "f" * 32
H1, H2, H3 = "a" * 32, "b" * 32, "c" * 32


@pytest.fixture(autouse=True)
def _signed(monkeypatch):
    monkeypatch.setattr(hq_authority_enforce, "enforced", lambda: True)
    monkeypatch.setattr(hq_authority_enforce, "trusted", lambda: False)


@pytest.fixture
def keys(tmp_path):
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    path = tmp_path / "op"
    path.write_bytes(pem)
    path.chmod(0o600)
    public = private.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    return str(path), public.decode()


def _later() -> float:
    return time.monotonic() + 30


def _envelope(keys, head, revision=2, **extra):
    record = {"revision": revision, **extra}
    return {
        "head": head,
        "record": record,
        "signature": sign_authority(record, signing_key=keys[0]),
    }


def _port(tmp_path, keys, routes, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        body = routes[request.url.path.removeprefix(PREFIX)]
        return httpx.Response(200, json=body)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), base_url="http://hq"),
    )
    cfg = {
        "hq": {
            "route": "api",
            "api": {
                "url": "http://hq",
                "operator_public_key": keys[1],
                "floor_path": str(tmp_path / "floor" / "api.json"),
                "initial_revision": FLOOR,
            },
        }
    }
    return select_hq_port(cfg), calls


def _history(head, parent=FLOOR, tables=()):
    return {
        "commits": [{"commit": head, "parent": parent}],
        "diff_summary": [{"to_table": t, "diff_type": "modified"} for t in tables],
    }


def test_authority_read_is_verified_and_advances_the_floor(tmp_path, keys, monkeypatch):
    env = _envelope(keys, H1)
    port, _ = _port(
        tmp_path, keys, {"/authority": env, f"/history/{FLOOR}": _history(H1)}, monkeypatch
    )
    assert port.read_authority(deadline=_later()) == env
    floor = json.loads((tmp_path / "floor" / "api.json").read_text())
    assert (floor["sequence"], floor["head"]) == (2, H1)


@pytest.mark.parametrize("method", ["config", "eligibility"])
def test_config_and_eligibility_reads_verify_their_authority(tmp_path, keys, monkeypatch, method):
    good = {"authority": _envelope(keys, H1), "body": 1}
    bad = {"authority": {**good["authority"], "record": {"revision": 9}}, "body": 1}
    hist = {f"/history/{FLOOR}": _history(H1)}

    def read(port):
        if method == "config":
            return port.read_config(deadline=_later())
        return port.read_eligibility("f", deadline=_later())

    port, _ = _port(
        tmp_path,
        keys,
        {f"/{method}" + ("/f" if method == "eligibility" else ""): good, **hist},
        monkeypatch,
    )
    assert read(port) == good
    port, _ = _port(
        tmp_path / "b",
        keys,
        {f"/{method}" + ("/f" if method == "eligibility" else ""): bad, **hist},
        monkeypatch,
    )
    with pytest.raises(HqRefused) as err:
        read(port)
    assert err.value.code == "authority_signature"


def test_config_without_an_authority_envelope_is_unavailable(tmp_path, keys, monkeypatch):
    port, _ = _port(tmp_path, keys, {"/config": {"body": 1}}, monkeypatch)
    with pytest.raises(HqUnavailable):
        port.read_config(deadline=_later())


def test_tampered_record_and_bad_signature_are_refused(tmp_path, keys, monkeypatch):
    env = _envelope(keys, H1)
    env["record"]["note"] = "tampered"
    port, _ = _port(tmp_path, keys, {"/authority": env}, monkeypatch)
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "authority_signature"
    assert not (tmp_path / "floor" / "api.json").exists()


def test_unsigned_marker_is_refused_in_signed_mode(tmp_path, keys, monkeypatch):
    env = {**_envelope(keys, H1), "signature": hq_authority_enforce.UNSIGNED_SIGNATURE}
    port, _ = _port(tmp_path, keys, {"/authority": env}, monkeypatch)
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "authority_signature"


def test_rolled_back_sequence_is_refused(tmp_path, keys, monkeypatch):
    port, _ = _port(
        tmp_path,
        keys,
        {"/authority": _envelope(keys, H1, 3), f"/history/{FLOOR}": _history(H1)},
        monkeypatch,
    )
    port.read_authority(deadline=_later())
    port, _ = _port(tmp_path, keys, {"/authority": _envelope(keys, H1, 2)}, monkeypatch)
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "restore_floor" and "below restore floor" in err.value.reason


def test_forked_head_is_refused(tmp_path, keys, monkeypatch):
    port, _ = _port(
        tmp_path,
        keys,
        {"/authority": _envelope(keys, H1), f"/history/{FLOOR}": _history(H1, parent=H3)},
        monkeypatch,
    )
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "restore_floor" and "fork" in err.value.reason


def test_same_sequence_schema_rewrite_is_refused_but_provisioning_is_allowed(
    tmp_path, keys, monkeypatch
):
    routes = {"/authority": _envelope(keys, H1, 2), f"/history/{FLOOR}": _history(H1)}
    port, _ = _port(tmp_path, keys, routes, monkeypatch)
    port.read_authority(deadline=_later())
    ok = {
        "/authority": _envelope(keys, H2, 2),
        f"/history/{H1}": _history(H2, parent=H1, tables=["frame_x_inbox", "dolt_ignore"]),
    }
    port, _ = _port(tmp_path, keys, ok, monkeypatch)
    port.read_authority(deadline=_later())
    bad = {
        "/authority": _envelope(keys, H3, 2),
        f"/history/{H2}": _history(H3, parent=H2, tables=["hq_authority"]),
    }
    port, _ = _port(tmp_path, keys, bad, monkeypatch)
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "restore_floor" and "hq_authority" in err.value.reason


def test_no_floor_path_keeps_the_reads_fail_closed(tmp_path, keys, monkeypatch):
    port, _ = _port(tmp_path, keys, {"/authority": _envelope(keys, H1)}, monkeypatch)
    port._verifier = None
    with pytest.raises(HqUnavailable, match="verif"):
        port.read_authority(deadline=_later())


def test_missing_operator_key_is_unavailable_in_signed_mode(tmp_path, keys, monkeypatch):
    port, _ = _port(
        tmp_path,
        (keys[0], ""),
        {"/authority": _envelope(keys, H1), f"/history/{FLOOR}": _history(H1)},
        monkeypatch,
    )
    with pytest.raises(HqUnavailable):
        port.read_authority(deadline=_later())
