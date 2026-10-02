"""One frame proposal deadline covers all stages and an uncertain acknowledgment."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from beadhive import host
from beadhive.hq_control_plane import ControlPlaneError, HqLeaseUnknown, SqlControlPlane
from beadhive.hq_sql_operator import SqlOperatorError, SqlRuntimeOperator


@pytest.mark.parametrize("late_stage", ["proposal", "result"])
def test_hive_proposal_composed_deadline_preserves_original_request(monkeypatch, late_stage):
    plane = object.__new__(SqlControlPlane)
    plane.settings = {"runtime": {"operation_timeout": 0.02}}
    monkeypatch.setattr(host, "signing_key", lambda: "/fixture/key")
    request_id = "00000000-0000-0000-0000-000000000001"
    request_sha256 = "a" * 64
    binding = SimpleNamespace(principal="frame_a")
    seen = []

    def propose(*_args, deadline, **_kwargs):
        seen.append(deadline)
        if late_stage == "proposal":
            time.sleep(0.04)
        return request_id, request_sha256, binding, "fleet"

    class Runtime:
        def read_public_result(self, *_args, deadline, **_kwargs):
            seen.append(deadline)
            time.sleep(0.04)
            return "accepted", "new-revision"

    monkeypatch.setattr(plane, "propose_hive_lease", propose)
    monkeypatch.setattr(plane, "_runtime_authority", lambda: Runtime())
    with pytest.raises(HqLeaseUnknown) as outcome:
        plane.publish_hive_lease("bh", object(), expected="original-CAS", operation="adopt")
    assert outcome.value.request_id == request_id
    assert outcome.value.request_sha256 == request_sha256
    assert outcome.value.expected_revision == "original-CAS"
    assert len(seen) == (1 if late_stage == "proposal" else 2)
    assert all(deadline == seen[0] for deadline in seen)


def test_operator_renew_carries_one_budget_through_load_and_publication(monkeypatch):
    plane = object.__new__(SqlControlPlane)
    plane.settings = {"authority_writer": {"operation_timeout": 0.02}}
    plane.clock = time.time
    seen = []

    class Operator:
        def load(self, *, deadline):
            seen.append(deadline)
            time.sleep(0.04)
            return "original", {"revision": 1, "issued_at": 1, "expires_at": 2}, (), {}

        def publish(self, _state, *, expected_revision, operator_key, deadline):
            seen.append(deadline)
            assert expected_revision == "original"
            assert operator_key == "/fixture/operator-key"
            if time.monotonic() >= deadline:
                raise SqlOperatorError("authority writer deadline exceeded")
            raise AssertionError("renewal used a fresh publication budget")

    monkeypatch.setattr(plane, "_operator", lambda: Operator())
    with pytest.raises(ControlPlaneError, match="SQL authority renewal unavailable"):
        plane.renew(expected="original", operator_key="/fixture/operator-key")
    assert len(seen) == 2 and seen[0] == seen[1]


def test_expired_operator_budget_never_opens_a_new_connection(monkeypatch):
    from beadhive import hq_sql_operator

    monkeypatch.setattr(
        hq_sql_operator, "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("expired operation reopened SQL")
        ),
    )
    operator = SqlRuntimeOperator(
        {"runtime": None, "authority_writer": {"operation_timeout": 10}},
        broker=object(),
    )
    with pytest.raises(SqlOperatorError, match="deadline exceeded"):
        operator.load(deadline=time.monotonic() - 1)
