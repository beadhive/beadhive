"""bh-ktw0o: a committed lease proposal never degrades to a generic connection error.

Fakes only: no SQL, no HQ, no real host state. HOME/BH_HOME stay sandboxed throughout.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from beadhive import host, host_adopt, host_fence, host_lease
from beadhive import hq_sql_runtime as rt
from beadhive.hq_control_plane import ControlPlaneError, HqLeaseUnknown, SqlControlPlane
from beadhive.hq_sql_transport import SqlTransportError

RID = "00000000-0000-0000-0000-0000000000aa"
SHA = "b" * 64


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bh"))


def _plane(monkeypatch, runtime, *, propose=None, timeout=5.0):
    plane = object.__new__(SqlControlPlane)
    plane.settings = {"runtime": {"operation_timeout": timeout}}
    monkeypatch.setattr(host, "signing_key", lambda: "/fixture/key")
    monkeypatch.setattr(
        plane,
        "propose_hive_lease",
        propose or (lambda *_a, **_k: (RID, SHA, SimpleNamespace(principal="p"), "fleet")),
    )
    monkeypatch.setattr(plane, "_runtime_authority", lambda: runtime)
    return plane


def _publish(plane):
    return plane.publish_hive_lease("bh", object(), expected="CAS-0", operation="adopt")


class _Runtime:
    def __init__(self, *results):
        self.results = list(results)

    def read_public_result(self, *_a, **_k):
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_final_poll_deadline_is_pending_with_provenance(monkeypatch):
    plane = _plane(
        monkeypatch, _Runtime(rt.SqlRuntimeDeadline("HQ runtime operation deadline exceeded"))
    )
    with pytest.raises(HqLeaseUnknown) as out:
        _publish(plane)
    exc = out.value
    assert (exc.request_id, exc.request_sha256, exc.expected_revision) == (RID, SHA, "CAS-0")
    assert exc.reason == "deadline"
    assert RID in str(exc) and SHA in str(exc) and "CAS-0" in str(exc)
    assert "NOT evidence that HQ is unreachable" in str(exc)
    assert "connection unavailable" not in str(exc)


def test_open_failure_after_budget_spent_is_deadline_not_unreachable(monkeypatch):
    authority = rt.SqlRuntimeAuthority({"runtime": {"operation_timeout": 0.01}}, broker=object())

    def slow_connect(*_a, deadline, **_k):
        time.sleep(0.03)
        raise SqlTransportError("connect failed")

    monkeypatch.setattr(rt, "connect", slow_connect)
    with pytest.raises(rt.SqlRuntimeDeadline, match="deadline exceeded"):
        authority._open()
    plane = _plane(monkeypatch, authority)
    plane.settings["runtime"]["operation_timeout"] = 0.01
    with pytest.raises(HqLeaseUnknown) as out:
        _publish(plane)
    assert out.value.reason == "deadline"


def test_transport_loss_after_submit_is_pending_transport(monkeypatch):
    plane = _plane(monkeypatch, _Runtime(rt.SqlRuntimeUnavailable("public result unavailable")))
    with pytest.raises(HqLeaseUnknown) as out:
        _publish(plane)
    assert out.value.reason == "transport"
    assert out.value.request_id == RID and out.value.expected_revision == "CAS-0"


def test_raw_transport_error_with_secret_text_is_redacted(monkeypatch):
    plane = _plane(monkeypatch, _Runtime(SqlTransportError("password=hunter2 refused")))
    with pytest.raises(HqLeaseUnknown) as out:
        _publish(plane)
    assert "hunter2" not in str(out.value) and out.value.__cause__ is None


def test_pre_submit_transport_failure_stays_distinct(monkeypatch):
    def propose(*_a, **_k):
        raise rt.SqlRuntimeUnavailable("verified HQ runtime connection unavailable")

    plane = _plane(monkeypatch, _Runtime(), propose=propose)
    with pytest.raises(rt.SqlRuntimeUnavailable, match="connection unavailable") as out:
        _publish(plane)
    assert not isinstance(out.value, HqLeaseUnknown)


def test_open_genuine_transport_failure_before_budget_is_unavailable(monkeypatch):
    authority = rt.SqlRuntimeAuthority({"runtime": {"operation_timeout": 30}}, broker=object())

    def refuse(*_a, **_k):
        raise SqlTransportError("connection refused")

    monkeypatch.setattr(rt, "connect", refuse)
    with pytest.raises(rt.SqlRuntimeUnavailable, match="connection unavailable"):
        authority._open()


def test_accepted_returns_revision_and_rejected_is_not_pending(monkeypatch):
    assert _publish(_plane(monkeypatch, _Runtime(None, ("accepted", "rev-1")))) == "rev-1"
    with pytest.raises(ControlPlaneError, match="rejected") as out:
        _publish(_plane(monkeypatch, _Runtime(("rejected", None))))
    assert not isinstance(out.value, HqLeaseUnknown)


def test_cli_adopt_reports_fence_and_proposal_persisted(monkeypatch, tmp_path):
    hive = tmp_path / "hive"
    (hive / ".git").mkdir(parents=True)
    from beadhive import frame_eligibility

    monkeypatch.setattr(frame_eligibility, "require_eligible", lambda *a, **k: None)
    monkeypatch.setattr(host_fence, "read_fence", lambda *a, **k: (None, None))
    monkeypatch.setattr(host_fence, "install_fence", lambda *a, **k: "fence-sha")
    monkeypatch.setattr(host_lease, "read", lambda *a, **k: None)

    def adopt(*_a, **_k):
        raise HqLeaseUnknown(RID, SHA, "CAS-0", "deadline")

    monkeypatch.setattr(host_lease, "adopt", adopt)
    with pytest.raises(host_adopt.AdoptHalfDone) as out:
        host_adopt.adopt(
            prefix="ah",
            hive_remote="origin",
            hq_remote="origin",
            hive_cwd=hive,
            hq_cwd=tmp_path,
            host_id="h1",
            label="l",
        )
    text = str(out.value)
    assert "remote fence = yes (epoch 1)" in text
    assert f"request {RID}" in text and f"sha256:{SHA}" in text and "CAS-0" in text
    assert "Do NOT re-run adopt yet" in text
    assert "bh host list --lease-hive ah" in text


def test_observed_rejected_row_is_a_definite_rejection_naming_the_reason(monkeypatch):
    plane = _plane(monkeypatch, _Runtime(None, ("rejected", "reject:policy_mismatch")))
    with pytest.raises(ControlPlaneError) as out:
        _publish(plane)
    assert not isinstance(out.value, HqLeaseUnknown)
    text = str(out.value)
    assert "reason=policy_mismatch" in text and RID in text and "NOT an HQ outage" in text


def test_rejected_row_without_or_with_hostile_reason_is_redacted(monkeypatch):
    for stored in (None, "password=hunter2", "reject:password=hunter2 refused"):
        with pytest.raises(ControlPlaneError) as out:
            _publish(_plane(monkeypatch, _Runtime(("rejected", stored))))
        assert "reason=unspecified" in str(out.value) and "hunter2" not in str(out.value)


def test_no_row_still_pends_unknown(monkeypatch):
    plane = _plane(monkeypatch, _Runtime(rt.SqlRuntimeDeadline("deadline")))
    with pytest.raises(HqLeaseUnknown):
        _publish(plane)
