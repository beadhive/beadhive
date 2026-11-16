"""Contract tests: the adapter against response shapes copied from hq-service (bh-bmb4t).

The fixtures come from hq-service ``api/api.go`` / ``api/api_test.go`` at commit f69779c. Only
four routes exist there: GET /authority and the three signed submissions. Every other adapter
route is provisional until hq-service serves it (bh-hq-3sa, bh-hq-9lh, bh-hq-e9z, bh-hq-mz3).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from beadhive.hq_port import HqRefused, HqUnavailable
from beadhive.hq_service_adapter import PREFIX, HqServiceAdapter

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/hq_service_contract/responses.json").read_text()
)
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
