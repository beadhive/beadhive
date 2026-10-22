"""Optional release_policy on frame grants and its reviewed publication (bh-vfrem.5).

Fakes only: no live HQ, no network, a fixed clock.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from beadhive import frame_release_policy as policy
from beadhive import hq_authority_guard as guard
from beadhive import hq_operator_settings, release_policy
from beadhive.cli import app
from beadhive.hq_control_plane import ControlPlaneError, GitControlPlane, SqlControlPlane
from beadhive.hq_sql_signatures import fingerprint

NOW = 1000.0


def _public():
    return (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


def make_state(*, release="0.28.4", attested=None, spec=None):
    public = _public()
    receipt = {
        "sequence": 0,
        "sha": "",
        "first_seen": None,
        "consecutive": 0,
        "lease": None,
        "registration": None,
    }
    if attested:
        receipt = {
            "sequence": 1,
            "sha": "a" * 40,
            "first_seen": 900,
            "consecutive": 1,
            "lease": {"release": {"id": attested, "digest": "sha256:" + "2" * 64}},
            "registration": None,
        }
    desired = {
        "declared": True,
        "release": {"id": release, "digest": "sha256:" + "1" * 64},
        "caps": {"arch": "arm64"},
        "profile": "p",
    }
    if spec:
        desired["release_policy"] = release_policy.policy_for(spec)
    record = {
        "authority": {
            "frame_id": "frame",
            "holder_identity": "holder",
            "instance_ref": "vm",
            "key_fingerprint": fingerprint(public),
            "epoch": 3,
            "audience": "fleet",
            "config_revision": "v1",
            "candidate_expires_at": None,
        },
        "public_key": public,
        "state": "active",
        "desired": desired,
        "cordoned": False,
        "drain_deadline": None,
        "receipt": receipt,
    }
    return {
        "domain": guard.DOMAIN,
        "generation": "g",
        "revision": 4,
        "issued_at": 900,
        "expires_at": 1800,
        "frames": {"frame": {"active": record, "candidate": None, "retired": [], "epoch_floor": 3}},
    }


def prep(*args, **kwargs):
    """Most tests are not about reader compatibility: default to accepting unknown readers."""
    kwargs.setdefault("allow_unknown_readers", True)
    return policy.prepare(*args, **kwargs)


def desired_of(state):
    return state["frames"]["frame"]["active"]["desired"]


# -- shorthand normalisation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "stored"),
    [
        ("0.28.x", "==0.28.*"),  # any patch of one minor
        ("~0.28", "==0.28.*"),
        ("0.28.2 - 0.28.6", ">=0.28.2,<=0.28.6"),  # patch range
        ("0.27 - 0.29", ">=0.27,<0.30"),  # minor range
        ("1.x", "==1.*"),  # same major
        ("^1.2", ">=1.2,==1.*"),
        ("^0.28.2", ">=0.28.2,==0.28.*"),  # caret on 0.x keeps the minor fixed
        (">=0.28.2, ==0.28.*", ">=0.28.2,==0.28.*"),  # PEP 440 passes through, canonicalised
        ("==0.28.*,>=0.28.2", ">=0.28.2,==0.28.*"),
    ],
)
def test_operator_forms_normalise_to_canonical_pep440(text, stored):
    assert release_policy.normalize(text) == stored
    assert release_policy.is_canonical(stored)


@pytest.mark.parametrize("text", ["", "x", "*", "^0.0.3", "^0", "not a spec", ">=1.0 junk"])
def test_unbounded_or_invalid_shorthand_is_refused(text):
    with pytest.raises(release_policy.ReleasePolicyError):
        release_policy.normalize(text)


def test_prereleases_are_not_admitted_unless_named():
    assert not release_policy.satisfies("==0.28.*", "0.28.5rc1")
    assert release_policy.satisfies(">=0.28.5rc1,==0.28.*", "0.28.5rc2")


# -- guard validation -------------------------------------------------------------------------


def test_grant_without_policy_validates_unchanged():
    state = make_state()
    guard.validate_state(state)
    assert "release_policy" not in desired_of(state)


def test_grant_with_canonical_policy_validates():
    guard.validate_state(make_state(spec="==0.28.*"))


def test_exact_pin_regression_extra_desired_keys_still_refused():
    state = make_state()
    desired_of(state)["surprise"] = 1
    with pytest.raises(ValueError, match="invalid declared frame policy"):
        guard.validate_state(state)
    state = make_state()
    del desired_of(state)["profile"]
    with pytest.raises(ValueError, match="invalid declared frame policy"):
        guard.validate_state(state)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(spec="==0.29.*"),  # reference release outside the spec
        lambda p: p.update(spec="0.28.x"),  # shorthand is never signed
        lambda p: p.update(spec="==0.28.*,>=0.28.2"),  # not canonical order
        lambda p: p.update(spec=""),
        lambda p: p.update(spec=7),
        lambda p: p.update(spec="garbage"),
        lambda p: p.update(catalog="other"),
        lambda p: p.update(format=2),
        lambda p: p.update(format=True),
        lambda p: p.update(extra=1),
        lambda p: p.pop("catalog"),
    ],
)
def test_tampered_or_invalid_policies_are_refused(mutate):
    state = make_state(spec="==0.28.*")
    mutate(desired_of(state)["release_policy"])
    with pytest.raises(ValueError):
        guard.validate_state(state)


def test_non_dict_policy_is_refused():
    state = make_state()
    desired_of(state)["release_policy"] = "==0.28.*"
    with pytest.raises(ValueError):
        guard.validate_state(state)


def test_git_hq_refuses_policy_fail_closed():
    guard.refuse_release_policy(make_state())
    with pytest.raises(ValueError, match="SQL HQ"):
        guard.refuse_release_policy(make_state(spec="==0.28.*"))
    with pytest.raises(ControlPlaneError, match="SQL control plane"):
        GitControlPlane.release_policy(SimpleNamespace(), "frame", "plan", spec="0.28.x")


# -- plan: echo, digest, downgrade guard ------------------------------------------------------


def test_plan_echoes_stored_spec_and_does_not_advance_epoch():
    original = make_state()
    before = copy.deepcopy(original)
    state, plan = prep(original, "frame", "head", "^0.28.2")
    assert original == before
    assert plan["stored_spec"] == ">=0.28.2,==0.28.*"
    assert plan["epoch_advanced"] is False
    new = state["frames"]["frame"]["active"]
    assert new["authority"] == before["frames"]["frame"]["active"]["authority"]
    assert new["desired"]["release_policy"]["spec"] == ">=0.28.2,==0.28.*"
    assert state["frames"]["frame"]["epoch_floor"] == 3
    guard.validate_state(state)
    assert plan["plan_sha256"] == prep(original, "frame", "head", "^0.28.2")[1]["plan_sha256"]
    assert plan["plan_sha256"] != prep(original, "frame", "head2", "^0.28.2")[1]["plan_sha256"]


def test_policy_must_admit_the_reference_release():
    with pytest.raises(ValueError, match="does not satisfy"):
        prep(make_state(), "frame", "head", "0.29.x")


def test_downgrade_refused_without_flag_and_allowed_with_it():
    # attested 0.29.1 is excluded by ==0.28.*, which still admits lower releases.
    state = make_state(release="0.28.4", attested="0.29.1")
    with pytest.raises(ValueError, match="--allow-downgrade"):
        prep(state, "frame", "head", "0.28.x")
    _state, plan = prep(state, "frame", "head", "0.28.x", allow_downgrade=True)
    assert plan["downgrade"] is True


def test_narrowing_above_the_attested_release_is_not_a_downgrade():
    state = make_state(release="0.30.0", attested="0.29.1")
    _state, plan = prep(state, "frame", "head", ">=0.30")
    assert plan["downgrade"] is False


def test_policy_admitting_the_attested_release_is_not_a_downgrade():
    state = make_state(release="0.29.1", attested="0.29.2")
    assert prep(state, "frame", "head", "0.29.x")[1]["downgrade"] is False


def test_drop_restores_exact_pin_and_unchanged_is_refused():
    state, plan = prep(make_state(spec="==0.28.*"), "frame", "head", "none")
    assert "release_policy" not in desired_of(state)
    assert plan["stored_spec"] is None
    with pytest.raises(ValueError, match="unchanged"):
        prep(make_state(), "frame", "head", "none")
    with pytest.raises(ValueError, match="unchanged"):
        prep(make_state(spec="==0.28.*"), "frame", "head", "~0.28")


def test_unknown_frame_is_refused():
    with pytest.raises(ValueError, match="no active or candidate"):
        prep(make_state(), "other", "head", "0.28.x")


# -- apply through the plane ------------------------------------------------------------------


class FakeOperator:
    def __init__(self, state, head="head"):
        self.state = state
        self.head = head
        self.published = []

    def load(self, deadline=None):
        return self.head, copy.deepcopy(self.state), None, None

    def attested_releases(self, state, *, expected_revision, deadline=None):
        return {}

    def publish(self, state, *, expected_revision, operator_key, deadline=None):
        self.published.append((state, expected_revision, operator_key))
        return "newhead"


def fake_plane(operator):
    return SimpleNamespace(
        _operator=lambda: operator, _operator_deadline=lambda: 1.0, clock=lambda: NOW
    )


def test_apply_publishes_cas_signed_without_epoch_advance(monkeypatch):
    monkeypatch.setattr("beadhive.hq_authority_enforce.enforced", lambda: True)
    operator = FakeOperator(make_state())
    plane = fake_plane(operator)
    plan = policy.release_policy(plane, "frame", "plan", spec="0.28.x", allow_unknown_readers=True)
    done = policy.release_policy(
        plane,
        "frame",
        "apply",
        spec="0.28.x",
        allow_unknown_readers=True,
        plan_sha256=plan["plan_sha256"],
        operator_key="key",
        confirm=True,
    )
    assert done["revision"] == "newhead"
    state, expected, key = operator.published[0]
    assert (expected, key) == ("head", "key")
    assert state["revision"] == 5
    assert state["frames"]["frame"]["active"]["authority"]["epoch"] == 3
    assert desired_of(state)["release_policy"]["spec"] == "==0.28.*"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"confirm": False},
        {"operator_key": ""},
        {"plan_sha256": "sha256:" + "0" * 64},
    ],
)
def test_apply_requires_confirm_key_and_exact_digest(monkeypatch, kwargs):
    monkeypatch.setattr("beadhive.hq_authority_enforce.enforced", lambda: True)
    operator = FakeOperator(make_state())
    plane = fake_plane(operator)
    plan = policy.release_policy(plane, "frame", "plan", spec="0.28.x", allow_unknown_readers=True)
    args = {"plan_sha256": plan["plan_sha256"], "operator_key": "key", "confirm": True, **kwargs}
    with pytest.raises(ValueError, match="apply requires"):
        policy.release_policy(
            plane, "frame", "apply", spec="0.28.x", allow_unknown_readers=True, **args
        )
    assert operator.published == []


def test_sql_plane_exposes_release_policy():
    assert hasattr(SqlControlPlane, "release_policy")


# -- CLI --------------------------------------------------------------------------------------


class CliPlane:
    def __init__(self):
        self.calls = []

    def release_policy(self, frame, verb, **kwargs):
        self.calls.append((frame, verb, kwargs))
        return {"stored_spec": "==0.28.*"}


def test_cli_plan_and_apply_route_to_the_plane(monkeypatch):
    plane = CliPlane()
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda _s=None: plane)
    run = CliRunner().invoke(
        app,
        ["hq", "authority", "release-policy", "plan", "--frame", "frame", "--spec", "0.28.x"],
    )
    assert run.exit_code == 0, run.output
    assert json.loads(run.stdout) == {"stored_spec": "==0.28.*"}
    run = CliRunner().invoke(
        app,
        [
            "hq", "authority", "release-policy", "apply", "--frame", "frame", "--spec", "0.28.x",
            "--plan-sha256", "sha256:x", "--confirm", "--operator-key", "k", "--allow-downgrade",
        ],
    )  # fmt: skip
    assert run.exit_code == 0, run.output
    _frame, verb, kwargs = plane.calls[1]
    assert verb == "apply"
    assert kwargs["allow_downgrade"] is True
    assert kwargs["plan_sha256"] == "sha256:x"


def test_cli_refuses_missing_verb_or_frame(monkeypatch):
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda _s=None: CliPlane())
    run = CliRunner().invoke(app, ["hq", "authority", "release-policy", "--frame", "frame"])
    assert run.exit_code == 1
    run = CliRunner().invoke(app, ["hq", "authority", "release-policy", "plan"])
    assert run.exit_code == 1


# -- reader-version guard ---------------------------------------------------------------------


def fleet(*attested):
    """A state with one active frame per attested release (None: nothing attested)."""
    state = make_state(release="0.30.0")
    template = state["frames"]["frame"]["active"]
    state["frames"] = {}
    for index, release in enumerate(attested):
        record = copy.deepcopy(template)
        record["authority"]["frame_id"] = f"f{index}"
        record["authority"]["holder_identity"] = f"holder{index}"
        record["authority"]["key_fingerprint"] = fingerprint(_public())
        record["public_key"] = _public()
        record["authority"]["key_fingerprint"] = fingerprint(record["public_key"])
        record["receipt"] = make_state(attested=release)["frames"]["frame"]["active"]["receipt"]
        state["frames"][f"f{index}"] = {
            "active": record,
            "candidate": None,
            "retired": [],
            "epoch_floor": 3,
        }
    return state


def test_reader_floor_is_the_first_release_shipping_this_slice():
    assert policy.MIN_RELEASE_POLICY_READER == "0.29.0"


def test_mixed_version_fleet_refuses_and_names_the_old_frames():
    state = fleet("0.29.0", "0.28.4", "0.30.1")
    with pytest.raises(ValueError, match=r"f1 \(0\.28\.4\)") as caught:
        policy.prepare(state, "f0", "head", ">=0.29")
    assert "f0" not in str(caught.value).split(":")[-1]


def test_flag_never_waives_a_known_old_reader():
    state = fleet("0.30.0", "0.28.4", None)
    with pytest.raises(ValueError, match=r"f1 \(0\.28\.4\).*no override"):
        policy.prepare(state, "f0", "head", ">=0.29", allow_unknown_readers=True)
    # the flag still waives a fleet whose only problem is an unknown reader
    policy.prepare(fleet("0.30.0", None), "f0", "head", ">=0.29", allow_unknown_readers=True)


def test_unparseable_release_is_unknown_and_waivable():
    state = fleet("0.30.0", "0.30.0")
    state["frames"]["f1"]["active"]["receipt"]["lease"]["release"]["id"] = "not-a-version"
    with pytest.raises(ValueError, match="unknown"):
        policy.prepare(state, "f0", "head", ">=0.29")
    policy.prepare(state, "f0", "head", ">=0.29", allow_unknown_readers=True)


def test_all_current_fleet_passes():
    _state, plan = policy.prepare(fleet("0.29.0", "0.30.1"), "f0", "head", ">=0.29")
    assert plan["stored_spec"] == ">=0.29"


def test_unknown_reader_refuses_without_the_flag_and_passes_with_it():
    state = fleet("0.30.0", None)
    with pytest.raises(ValueError, match=r"f1 \(no attested release\)"):
        policy.prepare(state, "f0", "head", ">=0.29")
    policy.prepare(state, "f0", "head", ">=0.29", allow_unknown_readers=True)


def test_candidate_frames_count_as_readers():
    state = fleet("0.30.0", "0.30.0")
    entry = state["frames"]["f1"]
    entry["candidate"], entry["active"] = entry["active"], None
    entry["candidate"]["state"] = "pending"
    entry["candidate"]["receipt"] = make_state(attested="0.28.0")["frames"]["frame"]["active"][
        "receipt"
    ]
    with pytest.raises(ValueError, match="f1"):
        policy.prepare(state, "f0", "head", ">=0.29")


def test_dropping_a_policy_is_never_blocked_by_old_readers():
    state = fleet("0.30.0", "0.28.4", None)
    desired_of_f0 = state["frames"]["f0"]["active"]["desired"]
    desired_of_f0["release_policy"] = release_policy.policy_for(">=0.29")
    new, plan = policy.prepare(state, "f0", "head", "none")
    assert plan["stored_spec"] is None
    assert "release_policy" not in new["frames"]["f0"]["active"]["desired"]


def test_cli_passes_allow_unknown_readers(monkeypatch):
    plane = CliPlane()
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda _s=None: plane)
    run = CliRunner().invoke(
        app,
        ["hq", "authority", "release-policy", "plan", "--frame", "f", "--spec", "0.28.x",
         "--allow-unknown-readers"],
    )  # fmt: skip
    assert run.exit_code == 0, run.output
    assert plane.calls[0][2]["allow_unknown_readers"] is True
