"""Contract tests: the adapter against the shapes hq-service serves (bh-bmb4t, bh-21vpt).

The first four routes (GET /authority and the three signed submissions) are pinned to
``responses.json`` (hq-service ``api/api.go`` at f69779c). Incarnations, manifests, admissions,
the rebind and release-policy plan/apply pairs, history, config and eligibility are pinned to
hq-service v0.1.0 (4e0fc6b): the request bodies are the conformance corpus's own
(``admission_v1``, ``operator_v1``), the read bodies ``reads_v0.1.0.json`` (see
``tests/fixtures/hq_service_contract/PROVENANCE.md``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from beadhive.hq_framelease_contracts import ObservationAuthority
from beadhive.hq_port import AdmissionRequest, HqOutcomeUnknown, HqRefused, HqUnavailable
from beadhive.hq_service_adapter import PREFIX, HqServiceAdapter

CONTRACT = Path(__file__).parent / "fixtures/hq_service_contract"
FIXTURES = json.loads((CONTRACT / "responses.json").read_text())
READS = json.loads((CONTRACT / "reads_v0.1.0.json").read_text())
HEAD = "a" * 32
ENVELOPE = {"request": {"request_id": "r1", "prefix": "p"}, "signature": "s"}


def _later() -> float:
    return time.monotonic() + 30


def _adapter(status: int, key: str, verifier=None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=FIXTURES[key])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://hq")
    return HqServiceAdapter(client, verifier=verifier), calls


def test_authority_read_is_a_bare_get_and_hands_the_record_to_the_verifier():
    seen = []
    adapter, calls = _adapter(200, "authority_200", verifier=lambda m, p: seen.append(p) or p)
    result = adapter.read_authority(deadline=_later())
    assert (calls[0].method, calls[0].url.path, calls[0].url.query) == (
        "GET",
        f"{PREFIX}/authority",
        b"",
    )
    assert set(result) == {"head", "record", "signature"} and seen == [FIXTURES["authority_200"]]


def test_authority_409_refusal_carries_the_service_code():
    adapter, _ = _adapter(409, "authority_409_refused", verifier=lambda m, p: p)
    with pytest.raises(HqRefused) as err:
        adapter.read_authority(deadline=_later())
    assert err.value.code == "authority_failed"


@pytest.mark.parametrize("key", ["submission_200_accepted", "submission_200_replayed"])
def test_submission_outcomes_and_revision(key):
    adapter, calls = _adapter(200, key)
    result = adapter.submit_hive_lease(None, "p", ENVELOPE, deadline=_later())
    assert (result.outcome, result.revision) == (FIXTURES[key]["outcome"], "rev-2")
    assert json.loads(calls[0].content) == ENVELOPE  # the envelope goes through unwrapped
    assert calls[0].headers["content-type"] == "application/json"


def test_submission_refusal_is_a_stable_code_not_an_exception():
    adapter, _ = _adapter(409, "submission_409_refused")
    result = adapter.submit_registration(None, ENVELOPE, deadline=_later())
    assert (result.outcome, result.refusal) == ("refused", "request_id_reused")


def test_service_error_bodies_map_to_the_port_taxonomy():
    adapter, _ = _adapter(400, "error_400_malformed")
    with pytest.raises(HqRefused) as bad:
        adapter.submit_registration(None, ENVELOPE, deadline=_later())
    assert bad.value.code == "bad_request"
    adapter, _ = _adapter(500, "error_500")
    with pytest.raises(HqUnavailable, match="storage unavailable"):
        adapter.read_authority(deadline=_later())


# ---- v0.1.0: admission, operator, history, config and eligibility (bh-21vpt) ----------------


def _case(directory: str, name: str) -> dict:
    return json.loads((CONTRACT / directory / f"{name}.json").read_text())


def _wire(case: dict, key: str = "request", **substitutions: str) -> dict:
    """The corpus case's request body as the service reads it (placeholders filled in)."""
    text = case[key]["body"]
    for placeholder, value in {"$HEAD": HEAD, **substitutions}.items():
        text = text.replace(placeholder, value)
    return json.loads(text)


def _writer(responses: dict):
    """An adapter whose service answers ``(method, path) -> (status, json)``; calls recorded."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status, body = responses[(request.method, request.url.path.removeprefix(PREFIX))]
        return httpx.Response(status, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://hq")
    return HqServiceAdapter(client, verifier=lambda m, p: p), calls


def test_incarnation_body_is_the_corpus_body_and_the_answer_is_checked():
    case = _case("admission_v1", "incarnation-accepted-first-epoch")
    body = _wire(case)
    reply = {
        "outcome": "accepted",
        "revision": HEAD,
        **{k: body[k] for k in ("principal", "epoch")},
    }
    adapter, calls = _writer({("POST", "/incarnations"): (200, reply)})
    route = (body["principal"], "frame-a", "host-a", "inst-host-a", body["epoch"], "key")
    revision = adapter.register_incarnation(
        route, expected_revision=HEAD, frame_address="ignored", login_secret=None, deadline=_later()
    )
    assert revision == HEAD
    assert (calls[0].method, calls[0].url.path) == ("POST", f"{PREFIX}/incarnations")
    assert json.loads(calls[0].content) == body  # exactly {principal, epoch, expected_revision}
    adapter, _ = _writer({("POST", "/incarnations"): (200, {**reply, "epoch": 9})})
    with pytest.raises(HqUnavailable, match="different incarnation"):
        adapter.register_incarnation(
            route, expected_revision=HEAD, frame_address="", deadline=_later()
        )


def _authority(frame: str, host: str) -> ObservationAuthority:
    return ObservationAuthority(frame, host, "inst-" + host, "SHA256:k", 0, "hq", "cfg-1")


@pytest.mark.parametrize(
    ("name", "admitted"),
    [("admission-open-accepted", True), ("admission-candidate-accepted", False)],
)
def test_admission_body_is_the_corpus_body(name, admitted):
    case = _case("admission_v1", name)
    body = _wire(case)
    frame = case["request"]["path"].rsplit("/", 1)[1]
    host = body.get("host_id") or "host-b"
    reply = {"outcome": "accepted", "revision": "b" * 32, "frame_id": frame, "host_id": host}
    adapter, calls = _writer({("POST", f"/admissions/{frame}"): (200, reply)})
    request = AdmissionRequest(
        frame, _authority(frame, host), body.get("public_key", ""), {}, admitted=admitted
    )
    revision = adapter.admit(
        frame, request, signer=lambda r: "", expected_revision=HEAD, deadline=_later()
    )
    assert revision == "b" * 32
    assert calls[0].url.path == f"{PREFIX}/admissions/{frame}"
    assert json.loads(calls[0].content) == body


def test_a_stale_admission_is_the_retryable_envelope_and_writes_nothing():
    case = _case("admission_v1", "admission-open-refused-stale-revision")
    envelope = {
        "error": {"code": "stale_revision", "message": "x", "retryable": True, "requestId": "r"}
    }
    adapter, _ = _writer({("POST", "/admissions/frame-n"): (409, envelope)})
    body = _wire(case, **{"$STALE": "9" * 32})
    request = AdmissionRequest(
        "frame-n", _authority("frame-n", "host-n"), body["public_key"], {}, admitted=True
    )
    with pytest.raises(HqUnavailable, match="stale_revision"):
        adapter.admit("frame-n", request, signer=str, expected_revision="9" * 32, deadline=_later())


def test_a_5xx_on_an_admission_envelope_is_outcome_unknown():
    envelope = {"error": {"code": "storage_unavailable", "message": "x", "retryable": True}}
    adapter, _ = _writer({("POST", "/admissions/frame-n"): (503, envelope)})
    request = AdmissionRequest("frame-n", _authority("frame-n", "host-n"), "k", {}, admitted=True)
    with pytest.raises(HqOutcomeUnknown):
        adapter.admit("frame-n", request, signer=str, expected_revision=HEAD, deadline=_later())


def test_manifest_body_is_the_manifest_and_the_answer_names_the_config_head():
    case = _case("admission_v1", "manifest-accepted-new-frame-host")
    body = _wire(case)
    reply = {
        "outcome": "accepted",
        "revision": "d" * 32,
        "host_id": body["host_id"],
        "path": f"hosts/{body['host_id']}.yaml",
        "sha256": "0" * 64,
    }
    adapter, calls = _writer({("PUT", f"/manifests/{body['host_id']}"): (200, reply)})
    revision = adapter.publish_manifest(body, expected_revision=HEAD, deadline=_later())
    assert revision == "d" * 32  # the CONFIG head, not the runtime head
    assert json.loads(calls[0].content) == body  # the manifest itself: no wrapper, no revision


class _Operator:
    """A service for one plan/apply pair that returns the corpus case's own record."""

    def __init__(self, case: dict, frame: str | None):
        from tests.harness.fake_hq_service import FakeHqService

        self.case, self.fake = case, FakeHqService(clock=lambda: 0.0)
        record = json.loads(
            json.dumps(case["plan"]["expect"]["record"])
            .replace("$BACKEND", "svc:hq")
            .replace("$GENERATION", "g1")
            .replace("$CONFIG_HEAD", self.fake.config_head)
        )
        self.fake.operator_record = record
        self.record, self.frame = record, frame

    def run(self, adapter_call, **kwargs):
        client = httpx.Client(transport=self.fake.transport(), base_url="http://hq")
        return adapter_call(HqServiceAdapter(client), **kwargs)


def test_rebind_plans_then_signs_the_record_then_applies():
    operator = _Operator(_case("operator_v1", "rebind-frame"), "frame-a")
    state = json.loads(operator.case["plan"]["body"])
    signed: list[dict] = []
    revision = operator.run(
        lambda a: a.rebind(
            state,
            signer=lambda record: signed.append(record) or "c2ln",
            expected_revision=operator.fake.head,
            deadline=_later(),
        )
    )
    plan, apply = operator.fake.requests
    assert plan[:2] == ("POST", "/authority/rebind/plan") and plan[3] == state
    assert apply[:2] == ("POST", "/authority/rebind/apply")
    applied = {"expected_revision", "config_head", "issued_at", "expires_at", "signature", "record"}
    assert set(apply[3]) == {*state, *applied}
    assert signed == [operator.record] and apply[3]["signature"] == "c2ln"
    assert revision == operator.fake.head


@pytest.mark.parametrize("name", ["release-policy-set-caret", "release-policy-drop"])
def test_release_policy_routes_name_the_frame_in_the_path(name):
    operator = _Operator(_case("operator_v1", name), None)
    frame = operator.case["frame"]
    state = json.loads(operator.case["plan"]["body"])
    operator.run(
        lambda a: a.release_policy(
            frame, state, signer=lambda r: "c2ln", expected_revision=operator.fake.head,
            deadline=_later(),
        )
    )  # fmt: skip
    plan, apply = operator.fake.requests
    assert plan[1] == f"/authority/release-policy/{frame}/plan" and plan[3] == state
    assert apply[1] == f"/authority/release-policy/{frame}/apply"
    assert apply[3]["spec"] == state["spec"] and "frame" not in apply[3]


@pytest.mark.parametrize(
    ("name", "published"),
    [("manifest-publish-apply-signed", True), ("manifest-publish-already-published", False)],
)
def test_manifest_publication_routes_name_the_host_and_echo_the_plan(name, published):
    """hq-service v0.1.3 (bh-hb75g): ``POST /manifests/{host_id}/publish/{plan,apply}``; the plan
    body is the case's own and the apply is it plus the plan's inputs, signed once."""
    from beadhive.hq_service_adapter import operator_apply_body

    operator = _Operator(_case("operator_v1", name), None)
    host, body = operator.case["host"], json.loads(operator.case["plan"]["body"])
    if not published:
        operator.fake.manifest_bodies[host] = body["manifest"]
    signed: list[dict] = []

    def publish(adapter):
        plan = adapter.plan_manifest_publish(host, body, deadline=_later())
        apply = operator_apply_body(
            body, plan, signer=lambda record: signed.append(record) or "c2ln"
        )
        return plan, adapter.apply_manifest_publish(host, apply, deadline=_later())

    plan, answer = operator.run(publish)
    planned, applied = operator.fake.requests
    assert planned[:2] == ("POST", f"/manifests/{host}/publish/plan") and planned[3] == body
    assert applied[:2] == ("POST", f"/manifests/{host}/publish/apply")
    inputs = {"expected_revision", "config_head", "issued_at", "expires_at", "signature", "record"}
    assert set(applied[3]) == {*body, *inputs}
    assert signed == [plan["record"]] and plan["record"]["config_head"] == plan["config_head_after"]
    assert plan["config_publication"]["publish"] is published
    assert answer["outcome"] == "accepted" and answer["config_published"] is published


def test_a_trusted_fleet_plan_skips_the_signer():
    operator = _Operator(_case("operator_v1", "rebind-plain-signed"), None)
    operator.fake.trusted = True
    operator.run(
        lambda a: a.rebind(
            {}, signer=lambda r: pytest.fail("signed"), expected_revision=operator.fake.head,
            deadline=_later(),
        )
    )  # fmt: skip
    assert "signature" not in operator.fake.requests[1][3]


def test_a_plan_whose_signing_input_is_not_its_record_is_never_signed():
    operator = _Operator(_case("operator_v1", "rebind-plain-signed"), None)
    operator.fake.operator_record = {"revision": 2}
    plan_input = operator.fake.operator_record
    operator.fake.operator_record = {"revision": 3}  # the service hands out another record's input
    original = operator.fake._operate

    def lying(kind, body, phase):
        refusal, facts = original(kind, body, phase)
        if phase == "plan":
            facts = {**facts, "record": plan_input}
        return refusal, facts

    operator.fake._operate = lying
    with pytest.raises(HqUnavailable, match="signing_input"):
        operator.run(
            lambda a: a.rebind(
                {}, signer=lambda r: pytest.fail("signed"), expected_revision=operator.fake.head,
                deadline=_later(),
            )
        )  # fmt: skip


def test_a_plan_made_at_another_head_is_refused_stale():
    operator = _Operator(_case("operator_v1", "rebind-plain-signed"), None)
    with pytest.raises(HqRefused) as err:
        operator.run(
            lambda a: a.rebind(
                {}, signer=lambda r: "s", expected_revision="9" * 32, deadline=_later()
            )
        )
    assert err.value.code == "stale_revision"
    assert len(operator.fake.requests) == 1  # nothing was applied


def test_history_is_a_get_with_the_floor_in_the_query_and_returns_revisions():
    floor = READS["history_200"]["floor"]
    adapter, calls = _writer({("GET", "/history"): (200, READS["history_200"])})
    history = adapter._read_history(since=floor, deadline=_later())
    assert (calls[0].method, calls[0].url.path, calls[0].url.query) == (
        "GET",
        f"{PREFIX}/history",
        f"since={floor}".encode(),
    )
    assert (history.head, history.floor) == (HEAD, floor)
    assert [r["id"] for r in history.revisions] == ["b" * 32, HEAD]
    assert history.revisions[0]["changes"] == [
        {"table": "dolt_ignore", "data": True, "schema": False}
    ]


def test_history_refusals_are_envelopes_with_the_service_code():
    adapter, _ = _writer({("GET", "/history"): (409, READS["error_unknown_floor_409"])})
    with pytest.raises(HqRefused) as err:
        adapter._read_history(since="f" * 32, deadline=_later())
    assert err.value.code == "unknown_floor"


def test_config_is_served_with_its_authority_envelope_and_documents():
    seen = []
    adapter, _ = _writer({("GET", "/config"): (200, READS["config_200"])})
    adapter._verifier = lambda m, p: seen.append(m) or p
    body = adapter.read_config(deadline=_later())
    assert set(body) == {"authority", "publication", "sequence", "documents_sha256", "documents"}
    assert set(body["authority"]) == {"head", "record", "signature"} and seen == ["read_config"]
    assert set(body["documents"][0]) == {"path", "kind", "ordinal", "sha256", "content"}


def test_eligibility_is_bound_to_a_verified_authority_read():
    routes = {
        ("GET", "/authority"): (200, READS["authority_200"]),
        ("GET", "/eligibility/frame-a"): (200, READS["eligibility_200"]),
    }
    adapter, calls = _writer(routes)
    seen = []
    adapter._verifier = lambda m, p: seen.append(m) or p
    body = adapter.read_eligibility("frame-a", deadline=_later())
    assert seen == ["read_authority"]  # the service signs nothing on the eligibility route
    assert [c.url.path for c in calls] == [f"{PREFIX}/authority", f"{PREFIX}/eligibility/frame-a"]
    assert set(body) == {
        *("head", "config_head", "frame_id", "active", "principal", "epoch", "route"),
        *("liveness", "release", "authority"),
    }
    assert body["authority"] == READS["authority_200"]


@pytest.mark.parametrize("field", ["head", "config_head"])
def test_eligibility_of_another_authority_is_unavailable_not_trusted(field):
    moved = {**READS["eligibility_200"], field: "9" * 32}
    adapter, _ = _writer(
        {
            ("GET", "/authority"): (200, READS["authority_200"]),
            ("GET", "/eligibility/frame-a"): (200, moved),
        }
    )
    with pytest.raises(HqUnavailable, match="moved"):
        adapter.read_eligibility("frame-a", deadline=_later())


# ---- v0.1.0: withdraw, catalog, hive lease, release verdict, caller binding ------------------


def _fake_adapter(fake):
    client = httpx.Client(transport=fake.transport(), base_url="http://hq")
    return HqServiceAdapter(client, verifier=lambda m, p: p)


def test_withdraw_is_a_bodyless_delete_and_a_missing_manifest_is_a_replay():
    from tests.harness.fake_hq_service import FakeHqService

    case = _case("admission_v1", "manifest-withdraw-accepted")
    host = case["request"]["path"].rsplit("/", 1)[1]
    assert (case["request"]["method"], case["request"]["body"]) == ("DELETE", "")
    fake = FakeHqService(clock=lambda: 0.0)
    fake.manifests.add(host)
    before = fake.config_head
    adapter = _fake_adapter(fake)
    moved = adapter.withdraw_manifest(host, expected_revision=HEAD, deadline=_later())
    assert moved != before and host not in fake.manifests
    method, path, query, body = fake.requests[0]
    assert (method, path, query, body) == ("DELETE", f"/manifests/{host}", "", None)
    again = adapter.withdraw_manifest(host, expected_revision=HEAD, deadline=_later())
    assert again == moved  # replayed at the unchanged config head
    assert fake.seen[-1] == ("write", "replayed")


def test_a_caller_bound_to_another_host_is_a_refusal_not_an_outage():
    from tests.harness.fake_hq_service import FakeHqService

    fake = FakeHqService(clock=lambda: 0.0)
    fake.caller = {"kind": "host", "frame_id": "", "host_id": "host-other"}
    adapter = _fake_adapter(fake)
    with pytest.raises(HqRefused) as err:
        adapter.withdraw_manifest("host-n", expected_revision=HEAD, deadline=_later())
    assert err.value.code == "caller_not_bound"
    assert _case("admission_v1", "manifest-withdraw-refused-caller-bound-to-another-host")


def _catalog_rows() -> list[dict]:
    body = _case("admission_v1", "catalog-read-lists-rows")["expect"]["body"]
    return body["rows"]


def test_catalog_read_lists_rows_and_a_measured_release_gets_the_folded_row():
    from tests.harness.fake_hq_service import FakeHqService

    fake = FakeHqService(clock=lambda: 0.0)
    fake.catalog_rows = _catalog_rows()[:2]  # the two active rows the verify cases are given
    adapter = _fake_adapter(fake)
    listing = adapter._read_catalog("beadhive", deadline=_later())
    assert listing["rows"] == fake.catalog_rows and listing["release"] is None
    row = fake.catalog_rows[1]
    measured = {"id": row["version"], "digest": row["measured_digest"]}
    folded = adapter.read_release_catalog_row(fake.head, "beadhive", measured, deadline=_later())
    expected = _case("admission_v1", "catalog-verify-verified")["expect"]["body"]["release"]["row"]
    assert (
        folded == expected
    )  # {catalog, version, measured_digest, status, algorithm, active_digests}
    query = fake.requests[-1][2]
    assert query == f"release_id={row['version']}&release_digest={row['measured_digest']}".replace(
        ":", "%3A"
    )
    unknown = {"id": "9.9.9", "digest": "sha256:" + "9" * 64}
    assert (
        adapter.read_release_catalog_row(fake.head, "beadhive", unknown, deadline=_later()) is None
    )


def test_a_catalog_read_at_another_head_than_the_verified_one_is_retryable():
    from tests.harness.fake_hq_service import FakeHqService

    fake = FakeHqService(clock=lambda: 0.0)
    fake.catalog_rows = _catalog_rows()
    adapter = _fake_adapter(fake)
    measured = {"id": "0.28.3", "digest": fake.catalog_rows[1]["measured_digest"]}
    with pytest.raises(HqUnavailable, match="another head"):
        adapter.read_release_catalog_row("9" * 32, "beadhive", measured, deadline=_later())


def test_hive_lease_is_read_by_prefix_in_the_query():
    adapter, calls = _writer({("GET", "/hive-leases"): (200, READS["hive_lease_200"])})
    body = adapter.read_hive_lease("bh", deadline=_later())
    assert (calls[0].url.path, calls[0].url.query) == (f"{PREFIX}/hive-leases", b"prefix=bh")
    assert body["held"] is True and body["lease"]["host_id"] == "host-a"
    adapter, _ = _writer({("GET", "/hive-leases"): (404, READS["error_unknown_prefix_404"])})
    with pytest.raises(HqRefused) as err:
        adapter.read_hive_lease("zz", deadline=_later())
    assert err.value.code == "unknown_prefix"


def test_eligibility_carries_principal_epoch_route_and_an_optional_release_verdict():
    release = READS["eligibility_200_release"]["release"]
    served = {**READS["eligibility_200"], "release": release}
    adapter, calls = _writer(
        {
            ("GET", "/authority"): (200, READS["authority_200"]),
            ("GET", "/eligibility/frame-a"): (200, served),
        }
    )
    measured = {"id": release["id"], "digest": release["digest"]}
    body = adapter.read_eligibility("frame-a", measured=measured, deadline=_later())
    assert calls[1].url.query.decode() == (
        f"release_id=rel-1&release_digest={release['digest'].replace(':', '%3A')}"
    )
    assert (body["principal"], body["epoch"], body["route"]["carrier"]) == (
        "frame_09dfb858e523b921bc5c",
        3,
        "service",
    )
    assert body["release"]["admits"] is True and body["release"]["basis"] == "catalog"
    assert body["authority"] == READS["authority_200"]


def test_eligibility_without_a_grant_has_null_identity_fields():
    body = READS["eligibility_200_no_grant"]
    assert all(body[k] is None for k in ("active", "principal", "epoch", "route", "liveness"))
