"""Replay of hq-service ``conformance/operator/v1`` against the adapter (bh-lejfy).

The corpus is normative for hq-service's operator routes (rebind, release-policy and release
catalog import plan/apply). This replays it from the CLIENT's side: a scripted service answers
each case's own plan (and accepted apply) and the test asserts what the adapter puts on the wire
and what it signs, with a real Ed25519 key over the plan's ``signing_input``. Cases that
exercise server rules only (a tampered or missing signature, edited replays, clock moves, apply
refusals) cannot be driven by a correct client and are skipped by shape, never by name.

The corpus is read in place from an hq-service export: set ``BH_HQ_SERVICE_CONFORMANCE`` to the
export's root (the directory holding ``conformance/``). With it unset or missing the replay
skips, naming the variable. The copy pinned for ordinary CI is in
``tests/test_hq_service_contract.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive.hq_port import HqRefused, HqUnavailable
from beadhive.hq_service_adapter import PREFIX, HqServiceAdapter
from beadhive.hq_sql_signatures import (
    AUTHORITY_DOMAIN,
    CATALOG_DOMAIN,
    canonical,
    sign_authority,
    sign_catalog_record,
    verify_authority,
)

ENV = "BH_HQ_SERVICE_CONFORMANCE"
HEAD, NEW_HEAD, CONFIG_HEAD = "a" * 32, "b" * 32, "c" * 32


def _cases_dir() -> Path | None:
    root = os.environ.get(ENV)
    path = Path(root) / "conformance/operator/v1/cases" if root else None
    return path if path and path.is_dir() else None


def _case_names() -> list[str]:
    directory = _cases_dir()
    return sorted(p.stem for p in directory.glob("*.json")) if directory else []


def _later() -> float:
    return time.monotonic() + 30


def _replayable(case: dict) -> str | None:
    """Why a case is not a client-side replay (``None``: it is)."""
    if case["op"] not in {"rebind", "release-policy", "catalog-import"}:
        return "the route is not routed by the adapter (catalog revoke is 0.41.0)"
    try:
        body = json.loads(case["plan"]["body"])
    except ValueError:
        return "the plan body is not JSON, which no client sends"
    if not isinstance(body, dict):
        return "the plan body is not an object, which no client sends"
    apply = case.get("apply")
    if apply is None:
        return None
    if apply.get("sign") != "operator" or any(
        apply.get(key) for key in ("override", "delete", "echo", "echo_edit", "delay_s", "then")
    ):
        return "the apply is altered or unsigned: it tests the service, not the client"
    if apply["expect"].get("status") != 200:
        return "the apply is refused for a reason only the service can judge"
    return None


class Script:
    """A service that plays one case: its plan, then an accepted apply."""

    def __init__(self, case: dict, public_key: str):
        self.case, self.public_key, self.requests = case, public_key, []
        self.is_catalog = case["op"] == "catalog-import"
        self.domain = CATALOG_DOMAIN if self.is_catalog else AUTHORITY_DOMAIN
        self.plan_request = json.loads(case["plan"]["body"])
        self.plan: dict | None = None

    def _resolve(self, value):
        text = json.dumps(value)
        for token, replacement in {
            "$BACKEND": "svc:hq",
            "$GENERATION": "g1",
            "$CONFIG_HEAD": CONFIG_HEAD,
            "$HEAD": HEAD,
        }.items():
            text = text.replace(token, replacement)
        return json.loads(text)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path.removeprefix(PREFIX), body))
        if request.url.path.endswith("/plan"):
            return self._plan_response(body)
        return self._apply_response(body)

    def _plan_response(self, body: dict) -> httpx.Response:
        assert body == self.plan_request, "the plan request is not the case's body"
        expect = self.case["plan"]["expect"]
        if expect["status"] != 200:
            return httpx.Response(
                expect["status"],
                json={
                    "error": {
                        "code": expect["code"],
                        "message": expect["code"],
                        "retryable": expect.get("retryable", False),
                        "requestId": "req-replay",
                        "details": {"reason": expect.get("reason", "")},
                    }
                },
            )
        record = self._resolve(expect["record"])
        signing_input = self.domain + canonical(record, limit=4 * 1024 * 1024)
        needed = (self.case["given"].get("mode") != "trusted") or self.is_catalog
        self.plan = {
            "outcome": "planned",
            "revision": HEAD,
            "record": record,
            "signing_input": base64.b64encode(signing_input).decode(),
            "signing_sha256": hashlib.sha256(signing_input).hexdigest(),
            "issued_at": self.case["now"],
            "plan_expires_at": self.case["now"] + 900,
            "signature_needed": needed,
            **self._resolve(expect.get("body") or {}),
        }
        if not self.is_catalog:
            state = record["state"]
            self.plan["config_head"] = record.get("config_head", CONFIG_HEAD)
            self.plan["expires_at"] = state["expires_at"]
        return httpx.Response(200, json=self.plan)

    def _apply_response(self, body: dict) -> httpx.Response:
        plan = self.plan
        assert plan is not None
        extra = {"expected_revision", "issued_at", "signature"}
        if not self.is_catalog:
            extra |= {"config_head", "expires_at", "record"}
        wanted = {*self.plan_request, *extra}
        if plan["signature_needed"] is False:
            wanted.discard("signature")
        assert set(body) == wanted, "the apply is not the plan request plus the plan's inputs"
        assert {k: body[k] for k in self.plan_request} == self.plan_request
        assert body["expected_revision"] == plan["revision"]
        assert body["issued_at"] == plan["issued_at"]
        if not self.is_catalog:
            assert body["record"] == plan["record"]
            assert (body["config_head"], body["expires_at"]) == (
                plan["config_head"],
                plan["expires_at"],
            )
        if plan["signature_needed"]:
            verify_authority(
                plan["record"],
                body["signature"],
                granted_public_key=self.public_key,
                domain=self.domain,
            )
        outcome = self.case["apply"]["expect"].get("outcome", "accepted")
        return httpx.Response(200, json={"outcome": outcome, "revision": NEW_HEAD})


@pytest.mark.parametrize("name", _case_names() or ["<no corpus>"])
def test_operator_case_replays_against_the_adapter(name, tmp_path):
    if _cases_dir() is None:
        pytest.skip(
            f"set {ENV} to an hq-service export root (the directory holding conformance/) "
            "to replay conformance/operator/v1"
        )
    case = json.loads((_cases_dir() / f"{name}.json").read_text())
    reason = _replayable(case)
    if reason:
        pytest.skip(reason)
    private = Ed25519PrivateKey.generate()
    key = tmp_path / "operator"
    key.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    script = Script(case, public)
    adapter = HqServiceAdapter(
        httpx.Client(transport=httpx.MockTransport(script.handle), base_url="http://hq")
    )
    request = script.plan_request
    op = case["op"]

    def call():
        if op == "rebind":
            return adapter.rebind(
                request,
                signer=lambda r: sign_authority(r, signing_key=str(key)),
                expected_revision=HEAD,
                deadline=_later(),
            )
        if op == "release-policy":
            return adapter.release_policy(
                case["frame"],
                request,
                signer=lambda r: sign_authority(r, signing_key=str(key)),
                expected_revision=HEAD,
                deadline=_later(),
            )
        return adapter.import_release_catalog(
            case["catalog"],
            request,
            signer=lambda r: sign_catalog_record(r, signing_key=str(key)),
            deadline=_later(),
        )

    expect = case["plan"]["expect"]
    if expect["status"] != 200:
        with pytest.raises((HqRefused, HqUnavailable), match=expect["code"]):
            call()
        assert [m for m, _, _ in script.requests] == ["POST"]  # the plan; never an apply
        return
    if case.get("apply") is None:
        if op != "release-policy":
            pytest.skip("a plan-only case: the adapter plans only to apply it")
        plan = adapter.plan_release_policy(case["frame"], request, deadline=_later())
        assert plan["record"] == script.plan["record"]
        return
    assert call() == NEW_HEAD
    assert [p.rsplit("/", 1)[1] for _, p, _ in script.requests] == ["plan", "apply"]
