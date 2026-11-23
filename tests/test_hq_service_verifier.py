"""Contract tests: api authority/config/eligibility reads verify signature and floor (bh-2r7qd).

The history shape is hq-service v0.1.0's ``{head, floor, revisions[{id, parents, changes}]}``
(bh-21vpt); eligibility carries no envelope there, so its authority is a verified GET /authority.
"""

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
        status, body = body if isinstance(body, tuple) else (200, body)
        return httpx.Response(status, json=body)

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


def _history(head, parent=FLOOR, tables=(), floor=FLOOR):
    """hq-service v0.1.0 ``GET /history`` body: one revision, ``head``, after ``parent``."""
    changes = [{"table": t, "data": True, "schema": False} for t in tables]
    revision = {"id": head, "message": "m", "at": 1.0, "parents": [parent], "changes": changes}
    return {"head": head, "floor": floor, "revisions": [revision]}


def test_authority_read_is_verified_and_advances_the_floor(tmp_path, keys, monkeypatch):
    env = _envelope(keys, H1)
    port, _ = _port(tmp_path, keys, {"/authority": env, "/history": _history(H1)}, monkeypatch)
    assert port.read_authority(deadline=_later()) == env
    floor = json.loads((tmp_path / "floor" / "api.json").read_text())
    assert (floor["sequence"], floor["head"]) == (2, H1)


def test_config_read_verifies_its_authority(tmp_path, keys, monkeypatch):
    good = {"authority": _envelope(keys, H1), "body": 1}
    bad = {"authority": {**good["authority"], "record": {"revision": 9}}, "body": 1}
    port, _ = _port(tmp_path, keys, {"/config": good, "/history": _history(H1)}, monkeypatch)
    assert port.read_config(deadline=_later()) == good
    port, _ = _port(tmp_path / "b", keys, {"/config": bad, "/history": _history(H1)}, monkeypatch)
    with pytest.raises(HqRefused) as err:
        port.read_config(deadline=_later())
    assert err.value.code == "authority_signature"


def test_eligibility_rests_on_a_verified_authority_read(tmp_path, keys, monkeypatch):
    envelope = _envelope(keys, H1, config_head="c" * 32)
    body = {"head": H1, "config_head": "c" * 32, "frame_id": "f", "active": None, "liveness": None}
    routes = {"/authority": envelope, "/eligibility/f": body, "/history": _history(H1)}
    port, _ = _port(tmp_path, keys, routes, monkeypatch)
    assert port.read_eligibility("f", deadline=_later()) == {**body, "authority": envelope}
    forged = {**envelope, "record": {**envelope["record"], "revision": 9}}
    port, _ = _port(tmp_path / "b", keys, {**routes, "/authority": forged}, monkeypatch)
    with pytest.raises(HqRefused) as err:
        port.read_eligibility("f", deadline=_later())
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
        {"/authority": _envelope(keys, H1, 3), "/history": _history(H1)},
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
        {"/authority": _envelope(keys, H1), "/history": _history(H1, parent=H3)},
        monkeypatch,
    )
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "restore_floor" and "fork" in err.value.reason


def test_same_sequence_schema_rewrite_is_refused_but_provisioning_is_allowed(
    tmp_path, keys, monkeypatch
):
    routes = {"/authority": _envelope(keys, H1, 2), "/history": _history(H1)}
    port, _ = _port(tmp_path, keys, routes, monkeypatch)
    port.read_authority(deadline=_later())
    ok = {
        "/authority": _envelope(keys, H2, 2),
        "/history": _history(H2, parent=H1, tables=["frame_x_inbox", "dolt_ignore"]),
    }
    port, _ = _port(tmp_path, keys, ok, monkeypatch)
    port.read_authority(deadline=_later())
    bad = {
        "/authority": _envelope(keys, H3, 2),
        "/history": _history(H3, parent=H2, tables=["hq_authority"]),
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
        {"/authority": _envelope(keys, H1), "/history": _history(H1)},
        monkeypatch,
    )
    with pytest.raises(HqUnavailable):
        port.read_authority(deadline=_later())


def test_a_head_that_is_not_among_the_revisions_after_the_floor_is_a_fork(
    tmp_path, keys, monkeypatch
):
    routes = {"/authority": _envelope(keys, H1), "/history": _history(H2)}  # another head
    port, _ = _port(tmp_path, keys, routes, monkeypatch)
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "restore_floor" and "fork" in err.value.reason


def test_descent_walks_every_parent_of_a_merge(tmp_path, keys, monkeypatch):
    side = {"id": H3, "message": "m", "at": 1.0, "parents": [FLOOR], "changes": []}
    merge = {"id": H1, "message": "m", "at": 2.0, "parents": [H2, H3], "changes": []}
    other = {"id": H2, "message": "m", "at": 1.5, "parents": ["e" * 32], "changes": []}
    history = {"head": H1, "floor": FLOOR, "revisions": [side, other, merge]}
    port, _ = _port(
        tmp_path, keys, {"/authority": _envelope(keys, H1), "/history": history}, monkeypatch
    )
    port.read_authority(deadline=_later())  # the floor is the second parent's parent


def test_history_the_service_refuses_is_not_append_only(tmp_path, keys, monkeypatch):
    refusal = {"error": {"code": "unknown_floor", "message": "m", "retryable": False}}
    routes = {"/authority": _envelope(keys, H1), "/history": (409, refusal)}
    port, _ = _port(tmp_path, keys, routes, monkeypatch)
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "restore_floor" and "unknown_floor" in err.value.reason


CATALOG = "beadhive"
ROW_A = ("0.28.2", "sha256:" + "1" * 64)
ROW_B = ("0.28.3", "sha256:" + "2" * 64)


def _entry(row, status="active"):
    return {
        "catalog": CATALOG,
        "version": row[0],
        "measured_digest": row[1],
        "status": status,
        "algorithm": "beadhive-tree-sha256-v1",
    }


def _folded(rows, row):
    """The service's verdict for ``row`` over the listed ``rows`` (store/catalog.go)."""
    mine = [r for r in rows if (r["version"], r["measured_digest"]) == row]
    revoked = any(r["status"] == "revoked" for r in mine)
    folded = None
    if mine:
        folded = {
            "catalog": CATALOG,
            "version": row[0],
            "measured_digest": row[1],
            "status": "revoked" if revoked else "active",
            "algorithm": "beadhive-tree-sha256-v1",
            "active_digests": 0 if revoked else 1,
        }
    verified = folded is not None and not revoked
    return {"id": row[0], "digest": row[1], "verified": verified, "row": folded}


def _catalog_port(tmp_path, keys, monkeypatch, rows, *, lie=None):
    """A floor at sequence 2, then a same-sequence head that changes ``hq_release_catalog``."""

    def serve(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(PREFIX)
        if path.startswith("/release-catalog/"):
            params = request.url.params
            if "release_id" not in params:
                return httpx.Response(200, json={"head": H2, "catalog": CATALOG, "rows": rows})
            row = (params["release_id"], params["release_digest"])
            verdict = _folded(rows, row)
            if lie:
                verdict = lie(verdict)
            return httpx.Response(
                200, json={"head": H2, "catalog": CATALOG, "rows": None, "release": verdict}
            )
        routes = {
            "/authority": _envelope(keys, H2, 2),
            "/history": _history(H2, parent=H1, tables=["hq_release_catalog"], floor=H1),
        }
        return httpx.Response(200, json=routes[path])

    seed = {"/authority": _envelope(keys, H1, 2), "/history": _history(H1)}
    port, _ = _port(tmp_path, keys, seed, monkeypatch)
    port.read_authority(deadline=_later())
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(serve), base_url="http://hq"),
    )
    return select_hq_port(
        {
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
    )


def test_a_same_sequence_catalog_change_is_accepted_when_every_row_verifies(
    tmp_path, keys, monkeypatch
):
    rows = [_entry(ROW_A), _entry(ROW_B), _entry(ROW_B, "revoked")]
    port = _catalog_port(tmp_path, keys, monkeypatch, rows)
    port.read_authority(deadline=_later())


def test_a_catalog_row_that_does_not_verify_refuses_the_head(tmp_path, keys, monkeypatch):
    def lying(verdict):
        return {**verdict, "verified": False}  # the service itself does not vouch for the row

    port = _catalog_port(tmp_path, keys, monkeypatch, [_entry(ROW_A)], lie=lying)
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "restore_floor" and ROW_A[0] in err.value.reason


def test_a_catalog_row_the_service_cannot_fold_refuses_the_head(tmp_path, keys, monkeypatch):
    port = _catalog_port(
        tmp_path, keys, monkeypatch, [_entry(ROW_A)], lie=lambda v: {**v, "row": None}
    )
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "restore_floor"


def test_an_unlisted_status_in_the_catalog_refuses_the_head(tmp_path, keys, monkeypatch):
    port = _catalog_port(tmp_path, keys, monkeypatch, [_entry(ROW_A, "edited")])
    with pytest.raises(HqRefused) as err:
        port.read_authority(deadline=_later())
    assert err.value.code == "restore_floor"


def test_a_same_sequence_catalog_change_with_no_catalog_reader_is_unavailable(
    tmp_path, keys, monkeypatch
):
    port = _catalog_port(tmp_path, keys, monkeypatch, [_entry(ROW_A)])
    port._verifier._catalog = None
    with pytest.raises(HqUnavailable, match="catalog reader"):
        port.read_authority(deadline=_later())


# --- whole-history descent is not capped at the provisioning bound (bh-qivt4) ---


def _descent_verifier(tmp_path, revisions, head):
    from beadhive.hq_port import HistorySinceFloor
    from beadhive.hq_service_verifier import ServiceResponseVerifier

    verifier = ServiceResponseVerifier(floor_path=str(tmp_path / "f.json"), initial_revision=FLOOR)
    verifier.bind_history(
        lambda **_: HistorySinceFloor(head=head, floor=FLOOR, revisions=revisions)
    )
    return verifier


def _linear(count, first_parent=FLOOR, table="frame_x"):
    revisions, previous = [], first_parent
    for index in range(count):
        identifier = f"r{index:031d}"
        revisions.append({"id": identifier, "parents": [previous], "changes": [{"table": table}]})
        previous = identifier
    return revisions, previous


@pytest.mark.parametrize("count", [64, 65, 171, 1000])
def test_a_linear_history_of_any_length_descends_from_the_floor(tmp_path, count):
    revisions, head = _linear(count)
    _descent_verifier(tmp_path, revisions, head)._check_descent(FLOOR, head, same_sequence=False)


def test_a_history_whose_floor_is_not_an_ancestor_is_a_fork(tmp_path):
    revisions, head = _linear(100, first_parent="e" * 32)
    with pytest.raises(HqRefused, match="history fork detected") as err:
        _descent_verifier(tmp_path, revisions, head)._check_descent(
            FLOOR, head, same_sequence=False
        )
    assert err.value.code == "restore_floor"


def test_a_same_sequence_head_past_the_provisioning_bound_is_truncated_not_a_fork(tmp_path):
    revisions, head = _linear(65)
    with pytest.raises(HqRefused, match="exceeds verifier bound") as err:
        _descent_verifier(tmp_path, revisions, head)._check_descent(FLOOR, head, same_sequence=True)
    assert err.value.code == "restore_floor"
    assert "fork" not in str(err.value)


def test_a_same_sequence_head_within_the_bound_is_still_judged_on_its_changes(tmp_path):
    revisions, head = _linear(64)
    _descent_verifier(tmp_path, revisions, head)._check_descent(FLOOR, head, same_sequence=True)
    bad, head = _linear(3, table="hq_authority")
    with pytest.raises(HqRefused, match="only reviewed session provisioning"):
        _descent_verifier(tmp_path, bad, head)._check_descent(FLOOR, head, same_sequence=True)
