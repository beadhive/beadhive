"""BH_HQ_AUTHORITY_ENFORCE: UNSUPPORTED dev-only per-host switch (bh-6pqul).

Default (unset / ``true``) keeps HQ runtime authority fail-closed exactly as before; ``false``
waives authority expiry, config binding and the authority-derived eligibility predicates with a
loud banner; any other value is an error. The SQL read-boundary halves live in
``tests/test_hq_sql_signed_liveness.py`` and the Git backend half in
``tests/test_hq_authority_backend.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from typer import Exit
from typer.testing import CliRunner

from beadhive import (
    config,
    doctor,
    guard,
    host,
    host_lease,
    hosts,
    hq_authority_enforce,
    hq_control_plane,
    registry,
)
from beadhive import frame_eligibility as policy
from beadhive.cli import app
from beadhive.host_heartbeat_core import HeartbeatLease, VerifiedObservation
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_document_validation import DocumentValidationError, validate_settings_mapping

ENV = hq_authority_enforce.ENFORCE_ENV
RELEASE = {"id": "release", "digest": "sha256:" + "1" * 64}
CAPS = dict(
    isolation="kvm", trust_zone="self-hosted", arch="x86_64", harnesses=["codex"], max_sessions=2
)


@pytest.fixture(autouse=True)
def _fresh_banner(monkeypatch):
    monkeypatch.setattr(hq_authority_enforce, "_banner_emitted", False)


@pytest.fixture
def frame():
    return hosts.HostManifest(
        host_id="host",
        frame_id="frame",
        state="active",
        release=RELEASE,
        capabilities=CAPS,
        instance_ref="vm",
        label="fixture",
        os="linux",
        arch="x86_64",
        role="executor",
        identity={"kind": "none"},
    )


@pytest.fixture
def observation():
    lease = HeartbeatLease(
        frame_id="frame",
        holderIdentity="host",
        instance_ref="vm",
        key_id="key",
        epoch=9,
        audience="fleet",
        config_revision="config",
        seq=1,
        renewTime=datetime.now(UTC).isoformat(),
        release=RELEASE,
        state_seen="active",
        conformance={
            "profile": "profile",
            "status": "conformant",
            "checks": [{"id": "required", "status": "pass"}],
        },
        report_digest="sha256:" + "2" * 64,
    )
    return VerifiedObservation("fresh", True, True, 1.0, lease)


# Admitted desired state that has drifted from this frame: cordoned, a different release pin,
# capability set and conformance profile — what a stale or config-unbound authority looks like.
DRIFTED = dict(
    declared=True,
    state="parked",
    cordoned=True,
    release={"id": "other", "digest": "sha256:" + "3" * 64},
    caps={**CAPS, "max_sessions": 9},
    profile="other-profile",
    authority={"holder_identity": "host", "instance_ref": "vm"},
)


# ---- parsing ------------------------------------------------------------------------------


def test_default_and_explicit_values(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert hq_authority_enforce.enforced() and hq_authority_enforce.status() == "enabled"
    for value in ("", "  ", "true", " true "):
        monkeypatch.setenv(ENV, value)
        assert hq_authority_enforce.enforced()
    monkeypatch.setenv(ENV, "false")
    assert not hq_authority_enforce.enforced()
    assert hq_authority_enforce.status() == "disabled"


@pytest.mark.parametrize("value", ["False", "TRUE", "0", "1", "off", "no", "disabled"])
def test_invalid_value_errors_never_falls_back(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    with pytest.raises(hq_authority_enforce.AuthorityEnforcementError, match=ENV):
        hq_authority_enforce.enforced()
    with pytest.raises(hq_authority_enforce.AuthorityEnforcementError):
        policy.load_facts(SimpleNamespace(), hq_dir=".", cfg={})


# ---- eligibility predicates -----------------------------------------------------------------


def test_enforced_drifted_authority_fails_closed_exactly_as_before(frame, observation):
    decision = policy.eligible(frame, {}, policy.EligibilityFacts(observation, DRIFTED))
    assert not decision.allowed
    assert decision.reason == (
        "admitted_active, not_cordoned, release_matches, conformance_pass, "
        "capabilities_match_admission"
    )
    assert "enforcement" not in decision.as_dict() and decision.waived == ()


def test_disabled_waives_authority_predicates_with_warning(frame, observation, capsys):
    facts = policy.EligibilityFacts(observation, DRIFTED, authority_waived=True)
    decision = policy.eligible(frame, {}, facts)
    assert decision.allowed
    payload = decision.as_dict()
    assert payload["enforcement"] == "disabled"
    assert set(payload["waived"]) == {
        "admitted_active",
        "not_cordoned",
        "release_matches",
        "conformance_pass",
        "capabilities_match_admission",
    }
    assert f"{ENV}=false" in capsys.readouterr().err


def test_disabled_unavailable_authority_waived_but_identity_and_heartbeat_hold(frame, observation):
    unavailable = policy.EligibilityFacts(observation, {}, available=False, authority_waived=True)
    decision = policy.eligible(frame, {}, unavailable)
    assert decision.allowed and "authority_available" in decision.waived
    # The frame's own signed lease must still name this manifest's incarnation.
    foreign = observation.lease.model_copy(update={"instance_ref": "other-vm"})
    stolen = policy.EligibilityFacts(
        VerifiedObservation("fresh", True, True, 1.0, foreign), {}, authority_waived=True
    )
    assert "current_frame_incarnation" in policy.eligible(frame, {}, stolen).reason
    # The heartbeat follows BH_FRAME_HEARTBEAT, not this switch.
    stale = policy.EligibilityFacts(
        VerifiedObservation("stale", True, False, 1.0, observation.lease),
        {},
        authority_waived=True,
    )
    assert policy.eligible(frame, {}, stale).reason == "authenticated_fresh_heartbeat"
    # Non-authority predicates are unchanged.
    viewer = frame.model_copy(update={"role": "viewer"})
    assert policy.eligible(viewer, {}, unavailable).reason == "executor_role"


# ---- sandbox: `bh plan file` / `bh work claim` intake boundary ------------------------------


class _ExpiredAuthorityPlane:
    """A selected HQ whose authority is expired and config-unbound: strict reads refuse."""

    config_backend = "sql"

    def __init__(self, frame, observation):
        self.frame, self.observation = frame, observation

    def load_host_manifest(self, host_id):
        return self.frame

    def read_eligibility(self, manifest, *, now=None):
        if hq_authority_enforce.enforced():
            raise hq_control_plane.ControlPlaneError(
                "protected HQ runtime authority expired or inconsistent"
            )
        return "head", DRIFTED, self.observation

    def read_hive_lease(self, prefix, *, holder_identity=None):
        return HostLease(
            host_id="host",
            label="host",
            epoch=1,
            adopted_at=now_stamp(),
            expires_at=now_stamp(datetime.now(UTC).timestamp() + 3600),
        )


@pytest.fixture
def sandbox(frame, observation, tmp_path, monkeypatch):
    plane = _ExpiredAuthorityPlane(frame, observation)
    monkeypatch.setattr(config, "hq_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "load_host", lambda: {"host": {"frame_id": "frame"}, "hq": {}})
    monkeypatch.setattr(host, "host_id", lambda: "host")
    monkeypatch.setattr(hq_control_plane, "control_plane", lambda *a, **k: plane)
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(
        registry, "entry_for_dir", lambda cfg, directory: {"prefix": "bh", "repo": "bh"}
    )
    monkeypatch.setattr(host_lease, "renew_if_due", lambda *a, **k: None)
    return {"cfg": {}, "dir": tmp_path}


@pytest.mark.parametrize("value", [None, "true"])
def test_expired_authority_refuses_claim_and_plan_file_when_enforced(sandbox, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(ENV, value)
    with pytest.raises(policy.EligibilityError, match="authority_available"):
        policy.require_intake("bh", cfg=sandbox["cfg"], hive_dir=sandbox["dir"])  # work claim
    with pytest.raises(Exit):
        guard.guard_primary("bh", cfg=sandbox["cfg"], verb="plan file")  # plan file


def test_expired_authority_claim_and_plan_file_proceed_when_disabled(sandbox, monkeypatch, capsys):
    monkeypatch.setenv(ENV, "false")
    decision = policy.require_intake("bh", cfg=sandbox["cfg"], hive_dir=sandbox["dir"])
    assert decision.allowed and decision.enforcement_disabled
    assert {"admitted_active", "not_cordoned", "release_matches"} <= set(decision.waived)
    guard.guard_primary("bh", cfg=sandbox["cfg"], verb="plan file")
    assert "authority predicates waived" in capsys.readouterr().err


def test_invalid_value_refuses_claim(sandbox, monkeypatch):
    monkeypatch.setenv(ENV, "maybe")
    with pytest.raises(hq_authority_enforce.AuthorityEnforcementError):
        policy.require_intake("bh", cfg=sandbox["cfg"], hive_dir=sandbox["dir"])


# ---- operator surfaces ----------------------------------------------------------------------


def _status_plane(monkeypatch):
    from beadhive import hq_authority_expiry

    monkeypatch.setattr(hq_control_plane, "control_plane", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(
        hq_authority_expiry,
        "authority_status",
        lambda plane, **k: {"revision": "r", "authority_ready": False},
    )


def test_every_command_prints_banner_once_and_status_reports_disabled(monkeypatch):
    _status_plane(monkeypatch)
    monkeypatch.setenv(ENV, "false")
    # `bh hq authority status` always prints JSON (it has no separate --json flag).
    result = CliRunner().invoke(app, ["hq", "authority", "status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["enforcement"] == "disabled"
    assert result.stderr.count("HQ authority enforcement DISABLED on this host") == 1


def test_enforced_status_reports_enabled_without_banner(monkeypatch):
    _status_plane(monkeypatch)
    result = CliRunner().invoke(app, ["hq", "authority", "status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["enforcement"] == "enabled"
    assert "DISABLED" not in result.stderr


def test_invalid_value_errors_at_the_cli(monkeypatch):
    _status_plane(monkeypatch)
    monkeypatch.setenv(ENV, "off")
    result = CliRunner().invoke(app, ["hq", "authority", "status"])
    assert result.exit_code == 2
    assert ENV in result.stderr


def test_banner_is_emitted_once_per_process(monkeypatch, capsys):
    monkeypatch.setenv(ENV, "false")
    assert hq_authority_enforce.emit_banner() and hq_authority_enforce.emit_banner()
    assert capsys.readouterr().err.count("DISABLED") == 1


def test_doctor_warns_only_when_disabled(monkeypatch, tmp_path):
    def warnings():
        return doctor._data_warnings({}, tmp_path, [], set(), set(), set(), set())

    assert not [w for w in warnings() if ENV in w]
    monkeypatch.setenv(ENV, "false")
    assert any("UNSUPPORTED" in w and ENV in w for w in warnings())


# ---- no new HOST key ------------------------------------------------------------------------


def test_host_yaml_needs_no_new_key_and_schema_stays_strict(monkeypatch):
    host_doc = {"host": {"frame_id": "frame"}, "hq": {"remote": "git@example:hq.git"}}
    monkeypatch.setenv(ENV, "false")
    validate_settings_mapping(host_doc, scope="host")
    # The switch is deliberately NOT a HOST key: older strict readers would reject one.
    with pytest.raises(DocumentValidationError):
        validate_settings_mapping(
            {**host_doc, "hq": {**host_doc["hq"], "authority": {"enforce": False}}},
            scope="host",
        )
