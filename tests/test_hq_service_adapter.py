"""hq-service adapter and ``hq.route`` selection (bh-g76hp)."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from beadhive import hq_port
from beadhive.hq_port import (
    EvidenceReport,
    FrameAnchor,
    HqDeadline,
    HqOutcomeUnknown,
    HqRefused,
    HqRouteError,
    HqUnavailable,
)
from beadhive.hq_service_adapter import (
    PREFIX,
    HqServiceAdapter,
    adapter_from_config,
    select_hq_port,
)

ANCHOR = FrameAnchor(principal="frame_a", frame="frame-a", epoch=7)


def _later() -> float:
    return time.monotonic() + 30


def _adapter(handler, verifier=None):
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record), base_url="http://hq")
    return HqServiceAdapter(client, verifier=verifier), calls


def _json(payload, status=200):
    return lambda request: httpx.Response(status, json=payload)


def test_renew_session_is_one_route_with_identity_in_path():
    adapter, calls = _adapter(_json({"revision": "r2"}))
    route = adapter.renew_session(ANCHOR, deadline=_later())
    assert route.revision == "r2"
    (request,) = calls
    assert (request.method, request.url.path) == ("POST", f"{PREFIX}/sessions/frame_a/7/renew")
    assert request.headers["content-type"] == "application/json"
    assert 0 < int(request.headers["X-Beadhive-Deadline-Ms"]) <= 30_000


def test_publish_evidence_posts_report():
    adapter, calls = _adapter(_json({"revision": "r3"}))
    adapter.publish_evidence(ANCHOR, EvidenceReport({"k": 1}), deadline=_later())
    assert json.loads(calls[0].content) == {"k": 1}
    assert calls[0].url.path.endswith("/sessions/frame_a/7/evidence")


def test_heartbeat_accepted_and_refusal_is_a_stable_code():
    adapter, calls = _adapter(_json({"outcome": "accepted", "revision": "r9"}))
    sub = adapter.submit_heartbeat(ANCHOR, {"request": {}}, request_id="rq1", deadline=_later())
    assert (sub.outcome, sub.revision, sub.request_id) == ("accepted", "r9", "rq1")
    assert (calls[0].method, calls[0].url.path) == ("PUT", f"{PREFIX}/heartbeats/rq1")

    adapter, _ = _adapter(_json({"outcome": "refused", "refusal": "beat_not_fresh"}, 409))
    sub = adapter.submit_heartbeat(ANCHOR, {"request": {}}, request_id="rq1", deadline=_later())
    assert (sub.outcome, sub.refusal) == ("refused", "beat_not_fresh")
    assert adapter.read_result(sub, deadline=_later()) is sub


def test_replayed_hive_lease():
    adapter, calls = _adapter(_json({"outcome": "replayed", "revision": "r1"}))
    sub = adapter.submit_hive_lease(
        ANCHOR, "bh", {"request": {"request_id": "q"}}, deadline=_later()
    )
    assert (sub.outcome, sub.request_id) == ("replayed", "q")
    assert calls[0].url.path == f"{PREFIX}/hive-leases/bh"


def test_path_segments_cannot_escape():
    adapter, calls = _adapter(_json({"outcome": "accepted", "revision": "r"}))
    adapter.submit_hive_lease(ANCHOR, "a/b?c", {"request": {}}, deadline=_later())
    assert calls[0].url.raw_path.decode() == f"{PREFIX}/hive-leases/a%2Fb%3Fc"


def test_non_submission_refusal_raises_with_code():
    adapter, _ = _adapter(_json({"outcome": "refused", "refusal": "stale_revision"}, 409))
    with pytest.raises(HqRefused) as info:
        adapter.publish_manifest({"host_id": "h"}, expected_revision="r0", deadline=_later())
    assert info.value.code == "stale_revision"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_gateway_refusals_before_the_service_are_unavailable(status):
    adapter, _ = _adapter(_json({"error": "no"}, status))
    with pytest.raises(HqUnavailable):
        adapter.admit("f", {"a": 1}, signer=lambda r: "", expected_revision="r", deadline=_later())


def _writes(adapter):
    """Every non-idempotent write the adapter sends, each with expected revision ``r7``."""
    signer = lambda record: "sig"  # noqa: E731
    return {
        "register_incarnation": lambda: adapter.register_incarnation(
            ("p", 1), expected_revision="r7", frame_address="f", deadline=_later()
        ),
        "publish_manifest": lambda: adapter.publish_manifest(
            {"host_id": "h"}, expected_revision="r7", deadline=_later()
        ),
        "admit": lambda: adapter.admit(
            "f", {"a": 1}, signer=signer, expected_revision="r7", deadline=_later()
        ),
        "rebind": lambda: adapter.rebind(
            {}, signer=signer, expected_revision="r7", deadline=_later()
        ),
        "release_policy": lambda: adapter.release_policy(
            "f", {}, signer=signer, expected_revision="r7", deadline=_later()
        ),
    }


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize(
    "name", ["register_incarnation", "publish_manifest", "admit", "rebind", "release_policy"]
)
def test_5xx_on_a_non_idempotent_write_is_outcome_unknown(name, status):
    def handler(request):
        body = json.loads(request.content)
        if body.get("phase") == "plan":  # the plan phase writes nothing; the apply phase fails
            return httpx.Response(200, json={"plan": {"head": "h"}})
        return httpx.Response(status, json={"error": "boom"})

    adapter, _ = _adapter(handler)
    with pytest.raises(HqOutcomeUnknown) as info:
        _writes(adapter)[name]()
    assert info.value.expected_revision == "r7"


@pytest.mark.parametrize("status", [500, 503])
def test_5xx_on_the_plan_phase_writes_nothing_so_is_unavailable(status):
    adapter, calls = _adapter(_json({"error": "boom"}, status))
    with pytest.raises(HqUnavailable):
        _writes(adapter)["rebind"]()
    assert len(calls) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_on_idempotent_submissions_and_reads_stays_unavailable(status):
    adapter, _ = _adapter(_json({"error": "boom"}, status))
    for call in (
        lambda: adapter.submit_heartbeat(ANCHOR, {}, request_id="rq", deadline=_later()),
        lambda: adapter.submit_hive_lease(ANCHOR, "p", {}, deadline=_later()),
        lambda: adapter.read_authority(deadline=_later()),
        lambda: adapter._read_history(since="r0", deadline=_later()),
    ):
        with pytest.raises(HqUnavailable):
            call()


@pytest.mark.parametrize("status", [400, 413, 415])
def test_malformed_calls_are_bad_request_refusals(status):
    adapter, _ = _adapter(_json({"error": "no"}, status))
    with pytest.raises(HqRefused) as info:
        adapter.register_incarnation(
            ("a",), expected_revision="r", frame_address="x", deadline=_later()
        )
    assert info.value.code == "bad_request"


def test_spent_deadline_never_calls_and_is_deadline():
    adapter, calls = _adapter(_json({}))
    with pytest.raises(HqDeadline):
        adapter.renew_session(ANCHOR, deadline=time.monotonic() - 1)
    assert not calls


def test_connect_failure_is_unavailable_but_lost_write_is_unknown():
    def refuse(request):
        raise httpx.ConnectError("down", request=request)

    adapter, _ = _adapter(refuse)
    with pytest.raises(HqUnavailable):
        adapter.register_incarnation(
            ("a",), expected_revision="r", frame_address="", deadline=_later()
        )

    def lost(request):
        raise httpx.ReadError("reset", request=request)

    adapter, _ = _adapter(lost)
    with pytest.raises(HqOutcomeUnknown) as info:
        adapter.register_incarnation(
            ("a",), expected_revision="r7", frame_address="", deadline=_later()
        )
    assert info.value.expected_revision == "r7"


def test_read_timeout_on_a_read_is_deadline():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    adapter, _ = _adapter(slow)
    with pytest.raises(HqDeadline):
        adapter._read_history(since="abc", deadline=_later())


def test_history_floor_is_a_path_segment():
    adapter, calls = _adapter(_json({"commits": [{"h": 1}], "diff_summary": []}))
    history = adapter._read_history(since="deadbeef", deadline=_later())
    assert calls[0].url.path == f"{PREFIX}/history/deadbeef" and not calls[0].url.query
    assert history.commits == ({"h": 1},)


def test_authority_reads_fail_closed_without_a_verifier():
    adapter, _ = _adapter(_json({"state": {}}))
    for call in (
        lambda: adapter.read_authority(deadline=_later()),
        lambda: adapter.read_eligibility("f", deadline=_later()),
        lambda: adapter.read_config(deadline=_later()),
    ):
        with pytest.raises(HqUnavailable, match="verif"):
            call()


def test_authority_reads_return_what_the_verifier_returns():
    seen = []
    adapter, calls = _adapter(
        _json({"state": {}}), verifier=lambda m, p: seen.append(m) or ("verified", m)
    )
    assert adapter.read_authority(deadline=_later()) == ("verified", "read_authority")
    adapter.read_eligibility("frame-a", deadline=_later())
    adapter.read_config(deadline=_later())
    assert [c.url.path for c in calls] == [
        f"{PREFIX}/authority",
        f"{PREFIX}/eligibility/frame-a",
        f"{PREFIX}/config",
    ]


def test_gap_methods_raise_unavailable_without_any_request():
    adapter, calls = _adapter(_json({}))
    d = _later()
    gaps = [
        lambda: adapter.describe_incarnation("p", 1, "f", target_route=None, deadline=d),
        lambda: adapter.preflight(route="r", deadline=d),
        lambda: adapter.withdraw_manifest("h", expected_revision="r", deadline=d),
        lambda: adapter.read_frame_anchor(expected_head=None, deadline=d),
        lambda: adapter.read_eligibility("f", prefix="bh", deadline=d),
        lambda: adapter.read_release_catalog_row("h", "c", {}, deadline=d),
        lambda: adapter.publish_authority({}, signer=str, expected_revision="r", deadline=d),
        lambda: adapter.read_evidence("f", "h", expected_revision="r", deadline=d),
        lambda: adapter.publish_config((), expected_revision="r", deadline=d),
        lambda: adapter.recover_publication("p", expected_revision="r", deadline=d),
    ]
    for gap in gaps:
        with pytest.raises(HqUnavailable, match="no hq-service route"):
            gap()
    assert not calls


def test_rebind_signs_between_plan_and_apply_without_exposing_a_key():
    phases = []

    def handler(request):
        body = json.loads(request.content)
        phases.append(body["phase"])
        if body["phase"] == "plan":
            return httpx.Response(200, json={"plan": {"head": "h", "config_head": "c"}})
        assert body["signature"] == "sig:h" and body["record"]["config_head"] == "c"
        return httpx.Response(200, json={"revision": "r5"})

    adapter, calls = _adapter(handler)
    head = adapter.rebind(
        {"s": 1},
        signer=lambda rec: "sig:" + rec["head"],
        expected_revision="r4",
        deadline=_later(),
    )
    assert head == "r5" and phases == ["plan", "apply"]
    assert {c.url.path for c in calls} == {f"{PREFIX}/authority/rebind"}


def test_release_policy_route():
    adapter, calls = _adapter(
        lambda r: httpx.Response(200, json={"plan": {"x": 1}, "revision": "r"})
    )
    adapter.release_policy("f", {}, signer=lambda r: "s", expected_revision="r0", deadline=_later())
    assert calls[0].url.path == f"{PREFIX}/release-policy"


# ---- selection ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(hq_port.ROUTE_ENV, raising=False)
    yield
    hq_port.register_sql_adapter(None)


def test_default_route_is_sql_and_switching_is_config_only():
    sentinel = object()
    hq_port.register_sql_adapter(lambda: sentinel)
    assert select_hq_port({"hq": {}}) is sentinel
    assert select_hq_port({"hq": {"route": "sql"}}) is sentinel
    api = {"hq": {"route": "api", "api": {"url": "https://hq.example"}}}
    assert isinstance(select_hq_port(api), HqServiceAdapter)
    api["hq"]["route"] = "sql"  # the only change needed to go back
    assert select_hq_port(api) is sentinel


def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv(hq_port.ROUTE_ENV, "api")
    cfg = {"hq": {"route": "sql", "api": {"url": "https://hq.example"}}}
    assert isinstance(select_hq_port(cfg), HqServiceAdapter)


def test_no_silent_fallback(monkeypatch):
    monkeypatch.setattr("beadhive.hq_service_adapter._load_sql_adapter", lambda: None)
    with pytest.raises(HqRouteError):
        select_hq_port({"hq": {"route": "api"}})  # api unconfigured: not sql
    with pytest.raises(HqRouteError):
        select_hq_port({"hq": {"route": "sql"}})  # no sql adapter: not api
    with pytest.raises(HqRouteError):
        select_hq_port({"hq": {"route": "grpc"}})


def test_unix_socket_config_builds_adapter():
    adapter = adapter_from_config({"hq": {"api": {"unix_socket": "/run/x/hq.sock"}}})
    assert isinstance(adapter, HqServiceAdapter)


def test_hq_route_config_is_host_only_and_validated():
    from beadhive.modules.config.application import partition

    assert "hq.route" in partition.HOST_PREFIXES and "hq.api" in partition.HOST_PREFIXES
    from beadhive.modules.config.contracts import HqConfig

    assert HqConfig().route == "sql"
    assert HqConfig(route="api", api={"url": "https://h"}).api.url == "https://h"
    with pytest.raises(ValueError):
        HqConfig(route="fallback")
    with pytest.raises(ValueError):
        HqConfig(api={"client_cert": "/c.pem"})
