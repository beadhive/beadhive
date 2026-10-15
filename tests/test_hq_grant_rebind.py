"""Re-binding a frame grant left on an old config revision (bh-2chzr).

`bh hq authority rebind` re-signs the authority but keeps every grant's ``config_revision``, so
a grant could stay on an old revision while adopt refused it. `rebind --frame <F>` is the
documented repair; `bh host eligible --hive` reports the binding as ``hive_policy_bound``.
Fakes only: no SQL server, no live authority, no wall clock.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from beadhive import host_cli, hq_operator_settings
from beadhive.cli import app
from beadhive.hq_control_plane import ControlPlaneError, GitControlPlane, SqlControlPlane

NOW = 1_791_400_000.0


def _record(frame, host, revision):
    return {
        "authority": {
            "frame_id": frame,
            "holder_identity": host,
            "instance_ref": f"vm-{frame}",
            "key_fingerprint": f"SHA256:{frame}",
            "epoch": 3,
            "audience": "fleet",
            "config_revision": revision,
            "candidate_expires_at": None,
        },
        "state": "active",
        "cordoned": False,
    }


class FakeOperator:
    def __init__(self, state):
        self.state = state
        self.published = []

    def load(self, *, deadline=None):
        return "head-1", self.state, ("sql", "gen", "cfg"), {}

    def publish(self, state, *, expected_revision, operator_key, deadline=None):
        self.published.append((state, expected_revision, operator_key))
        return "head-2"


def _sql_plane(monkeypatch, state, policies):
    plane = SqlControlPlane({}, clock=lambda: NOW)
    operator = FakeOperator(state)
    monkeypatch.setattr(plane, "_operator", lambda: operator)
    monkeypatch.setattr(plane, "_operator_deadline", lambda: 0.0)
    monkeypatch.setattr(
        plane, "config_store", lambda **_k: SimpleNamespace(load_snapshot=lambda: "snapshot")
    )
    monkeypatch.setattr(
        "beadhive.hq_hive_policy.project_hive_policies",
        lambda snapshot, *, valid_until, now: policies,
    )
    return plane, operator


def _state():
    return {
        "revision": 7,
        "issued_at": NOW - 100,
        "expires_at": 4102444800,
        "frames": {
            "xeno-mac": {"active": _record("xeno-mac", "host-x", "3fhtuca3"), "candidate": None},
            "factory": {"active": _record("factory", "host-f", "5newrev5"), "candidate": None},
        },
    }


def test_rebind_frame_moves_only_that_grant_to_the_current_policy_revision(monkeypatch):
    original = _state()
    plane, operator = _sql_plane(monkeypatch, original, {"bh-app": {"config_revision": "5newrev5"}})
    assert plane.renew(expected="head-1", operator_key="k", frame="xeno-mac") == "head-2"
    published, expected, key = operator.published[0]
    assert (expected, key, published["revision"]) == ("head-1", "k", 8)
    rebound = published["frames"]["xeno-mac"]["active"]["authority"]
    assert rebound["config_revision"] == "5newrev5"
    # The incarnation binding is untouched; only the policy revision moved.
    before = original["frames"]["xeno-mac"]["active"]["authority"]
    assert {k: v for k, v in rebound.items() if k != "config_revision"} == {
        k: v for k, v in before.items() if k != "config_revision"
    }
    assert before["config_revision"] == "3fhtuca3"  # the loaded state is not mutated
    assert published["frames"]["factory"] == original["frames"]["factory"]


def test_plain_rebind_preserves_every_grant_revision(monkeypatch):
    plane, operator = _sql_plane(monkeypatch, _state(), {"bh-app": {"config_revision": "5newrev5"}})
    plane.renew(expected="head-1", operator_key="k")
    published = operator.published[0][0]
    assert published["frames"]["xeno-mac"]["active"]["authority"]["config_revision"] == "3fhtuca3"


@pytest.mark.parametrize(
    ("policies", "match"),
    [
        ({}, "no managed hive carries a frame_policy"),
        (
            {"a": {"config_revision": "r1"}, "b": {"config_revision": "r2"}},
            "different frame_policy.config_revision values",
        ),
    ],
)
def test_rebind_frame_refuses_an_ambiguous_target_and_publishes_nothing(
    monkeypatch, policies, match
):
    plane, operator = _sql_plane(monkeypatch, _state(), policies)
    with pytest.raises(ControlPlaneError, match=match):
        plane.renew(expected="head-1", operator_key="k", frame="xeno-mac")
    assert operator.published == []


def test_rebind_frame_refuses_an_unknown_frame(monkeypatch):
    plane, operator = _sql_plane(monkeypatch, _state(), {"a": {"config_revision": "r1"}})
    with pytest.raises(ControlPlaneError, match="no active or candidate grant"):
        plane.renew(expected="head-1", operator_key="k", frame="nope")
    assert operator.published == []


def test_signed_mode_rebind_frame_still_needs_the_operator_key(monkeypatch):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    from beadhive import hq_authority_enforce

    hq_authority_enforce.reset_cache()
    plane, operator = _sql_plane(monkeypatch, _state(), {"a": {"config_revision": "r1"}})
    with pytest.raises(ControlPlaneError, match="operator-key"):
        plane.renew(expected="head-1", operator_key="", frame="xeno-mac")
    assert operator.published == []
    hq_authority_enforce.reset_cache()


def test_git_rebind_frame_is_refused():
    plane = GitControlPlane.__new__(GitControlPlane)
    with pytest.raises(ControlPlaneError, match="SQL HQ frame grant"):
        plane.renew(expected="x", operator_key="k", frame="xeno-mac")


def test_rebind_cli_passes_frame_through(monkeypatch):
    seen = []

    class Plane:
        authority_max_duration_s = None

        def renew(self, **kwargs):
            seen.append(kwargs)
            return "rev"

    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda *_a, **_k: Plane())
    monkeypatch.delenv("BH_HQ_OPERATOR_SETTINGS", raising=False)
    base = ["hq", "authority", "rebind", "--confirm", "--operator-key", "k"]
    assert CliRunner().invoke(app, [*base, "--frame", "xeno-mac"]).exit_code == 0
    assert CliRunner().invoke(app, base).exit_code == 0
    assert seen[0]["frame"] == "xeno-mac"
    assert "frame" not in seen[1]


# ---- bh host eligible --hive: the binding as its own predicate ------------------------------


def _payload():
    return {"eligible": True, "predicates": {"current_hive_lease_holder": True}, "reason": "ok"}


def test_eligible_reports_a_mismatched_binding_as_a_failing_predicate(monkeypatch):
    binding = {
        "bound": False,
        "frame_id": "xeno-mac",
        "slot": "active",
        "policy_config_revision": "5newrev5",
        "grant_config_revision": "3fhtuca3",
        "repair": "bh hq authority rebind --frame xeno-mac ...",
    }
    plane = SimpleNamespace(config_backend="sql", hive_policy_binding=lambda *a, **k: binding)
    monkeypatch.setattr("beadhive.hq_control_plane.control_plane", lambda *_a: plane)
    payload = _payload()
    host_cli._hive_policy_predicate(payload, "/hq", "bh-app", "host-x")
    assert payload["predicates"]["hive_policy_bound"] is False
    assert payload["eligible"] is False
    assert payload["reason"].endswith("hive_policy_bound")
    assert payload["hive_policy_binding"]["repair"].startswith("bh hq authority rebind")


def test_eligible_passes_a_bound_grant_and_skips_git_hq(monkeypatch):
    bound = SimpleNamespace(
        config_backend="sql", hive_policy_binding=lambda *a, **k: {"bound": True}
    )
    monkeypatch.setattr("beadhive.hq_control_plane.control_plane", lambda *_a: bound)
    payload = _payload()
    host_cli._hive_policy_predicate(payload, "/hq", "bh-app", "host-x")
    assert payload["predicates"]["hive_policy_bound"] is True and payload["eligible"]
    git = SimpleNamespace(config_backend="git")
    monkeypatch.setattr("beadhive.hq_control_plane.control_plane", lambda *_a: git)
    payload = _payload()
    host_cli._hive_policy_predicate(payload, "/hq", "bh-app", "host-x")
    assert "hive_policy_bound" not in payload["predicates"]


def test_eligible_fails_closed_when_the_binding_cannot_be_read(monkeypatch):
    def unreadable(*_a, **_k):
        raise ControlPlaneError("qualified SQL hive policy unavailable")

    plane = SimpleNamespace(config_backend="sql", hive_policy_binding=unreadable)
    monkeypatch.setattr("beadhive.hq_control_plane.control_plane", lambda *_a: plane)
    payload = _payload()
    host_cli._hive_policy_predicate(payload, "/hq", "bh-app", "host-x")
    assert payload["predicates"]["hive_policy_bound"] is False
    assert json.dumps(payload)
