"""BH_FRAME_HEARTBEAT=advisory waives only heartbeat freshness (transitional, bh-qlgmm)."""

from types import SimpleNamespace

import pytest

from beadhive import frame_eligibility as fe
from beadhive.host_heartbeat_core import VerifiedObservation


def _frame():
    return SimpleNamespace(
        frame_id="f1",
        capabilities=None,
        role="executor",
        release=None,
        host_id="h1",
        instance_ref="i1",
        beadyard_id="b1",
    )


def _decide(advisory):
    facts = fe.EligibilityFacts(
        VerifiedObservation("stale"), {"state": "active"}, heartbeat_advisory=advisory
    )
    return fe.eligible(_frame(), {}, facts)


def test_stale_heartbeat_fences_by_default():
    predicates = _decide(False).as_dict()["predicates"]
    assert predicates["authenticated_fresh_heartbeat"] is False


def test_advisory_waives_only_freshness_and_warns(capsys):
    decision = _decide(True)
    predicates = decision.as_dict()["predicates"]
    assert predicates["authenticated_fresh_heartbeat"] is True
    # Everything else still fails closed.
    assert predicates["current_frame_incarnation"] is False
    assert predicates["admitted_active"] is False
    assert not decision.allowed
    assert "BH_FRAME_HEARTBEAT=advisory" in capsys.readouterr().err


def test_heartbeat_mode_parsing(monkeypatch):
    monkeypatch.delenv(fe.HEARTBEAT_ENV, raising=False)
    assert fe.heartbeat_mode() == "required"
    monkeypatch.setenv(fe.HEARTBEAT_ENV, "advisory")
    assert fe.heartbeat_mode() == "advisory"
    monkeypatch.setenv(fe.HEARTBEAT_ENV, "off")
    with pytest.raises(fe.EligibilityError):
        fe.heartbeat_mode()
