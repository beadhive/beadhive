"""Admission per trust profile (trust-profiles ADR section 2, admission column; bh-sfp9o.4).

``open`` admits a host on registration, ``trusted`` keeps 0.24.0's join auto-admit,
``verified`` needs an operator grant, and ``attested`` needs the grant plus three fresh
authenticated heartbeats, a stale one restarting the count. ``hq.admission_policy``
(``manual`` | ``open``) overrides the knob under the tighten-only rule. Every clock here is
passed in: nothing sleeps or reads the wall clock.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import test_hq_authority_backend as git_hq
import test_hq_authority_fleet_mode as fleet_mode
from beadhive import (
    frame_eligibility,
    hosts,
    hq,
    hq_admission,
    hq_authority_enforce,
    hq_authority_fleet_mode,
    hq_open_admission,
    hq_operator_settings,
    hq_trust_profile,
    trust_profiles,
)
from beadhive import (
    host_heartbeat_core as hb,
)
from beadhive.cli import app
from beadhive.hq_control_plane import ControlPlaneError
from beadhive.modules.config.contracts import HqConfig
from beadhive.modules.config.domain.ports import FleetConfigDocument
from beadhive.trust_profiles import ATTESTED, OPEN, TRUSTED, VERIFIED, Override

ae = hq_authority_enforce
# The real SSH-signed Git HQ, guard provisioned by a trusted operator (key-less pushes allowed).
backend, _prepared_hqs = fleet_mode.backend, fleet_mode._prepared_hqs


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    for name in (ae.MODE_ENV, ae.ENFORCE_ENV, hq_operator_settings.ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ae, "_banner_emitted", False)
    monkeypatch.setattr(ae, "_invalid_emitted", set())
    ae.reset_cache()
    yield
    ae.reset_cache()


def _learn(profile):
    """One authorised read of a fleet publishing `profile`: the profile and its column."""
    hq_trust_profile.record(profile, head="h", via="test")
    ae.record_fleet_default(trust_profiles.resolve(profile).authority_mode, head="h", via="test")


def _fleet(policy=None, profile=None):
    lines = []
    if profile:
        lines.append(f"  trust_profile: {profile}")
    if policy:
        lines.append(f"  admission_policy: {policy}")
    hq_block = "hq:\n" + "\n".join(lines) + "\n" if lines else ""
    return FleetConfigDocument("fleet.yaml", f"schema_version: 1\n{hq_block}managed_repos: []\n")


def _admission(name, *overrides):
    return trust_profiles.resolve(name, overrides).admission


def _policy(value, *, signed=False):
    return Override("admission", "hq.admission_policy", value, "fleet.yaml", signed=signed)


# ---- hq.admission_policy: validated, the profile's default, tighten-only ----------------------


def test_the_fleet_key_validates_manual_or_open_and_defaults_to_the_profile():
    assert HqConfig().admission_policy is None  # unset: the profile supplies it
    assert HqConfig(admission_policy="manual").admission_policy == "manual"
    assert HqConfig(admission_policy="open").admission_policy == "open"
    with pytest.raises(ValidationError):
        HqConfig(admission_policy="auto")
    defaults = {
        name: trust_profiles.resolve(name).admission_policy for name in trust_profiles.PROFILES
    }
    assert defaults == {OPEN: "open", TRUSTED: "manual", VERIFIED: "manual", ATTESTED: "manual"}


def test_the_override_may_only_tighten_unless_signed():
    # Tightening applies.
    assert _admission(OPEN, _policy("manual")) == "operator-granted"
    assert _admission(TRUSTED, _policy("manual")) == "operator-granted"
    # ``manual`` never loosens a profile that already needs a grant.
    assert _admission(VERIFIED, _policy("manual")) == "operator-granted"
    resolved = trust_profiles.resolve(ATTESTED, [_policy("manual")])
    assert resolved.admission == "granted-attested" and not resolved.refusals
    # Loosening is refused (naming the profile) unless the operator key signs it.
    refused = trust_profiles.resolve(VERIFIED, [_policy("open")])
    assert refused.admission == "operator-granted"
    assert "verified trust profile" in refused.refusals[0].message
    assert _admission(VERIFIED, _policy("open", signed=True)) == "registration"
    assert trust_profiles.resolve(TRUSTED, [_policy("bogus")]).refusals


def test_the_fleet_policy_is_learned_tightening_always_loosening_only_signed():
    _learn(TRUSTED)
    snapshot = fleet_mode._snapshot
    assert hq_trust_profile.observe_admission(
        snapshot("a" * 32, _fleet("manual")), authorised=False
    )
    assert ae.trust_profile({}).admission == "operator-granted"
    # A key-less write back to ``open`` (looser) is not learned ...
    keyless = snapshot("b" * 32, _fleet("open"))
    assert not hq_trust_profile.observe_admission(keyless, authorised=False)
    assert ae.trust_profile({}).admission == "operator-granted"
    # ... nor is removing the key, which loosens back to join.
    assert not hq_trust_profile.observe_admission(snapshot("c" * 32, _fleet()), authorised=False)
    # The operator-signed carrier loosens it, recorded as signed.
    assert hq_trust_profile.observe_admission(keyless, authorised=True)
    assert hq_trust_profile.admission_state()["signed"] is True
    assert ae.trust_profile({}).admission == "registration"


def test_doctor_prints_the_effective_admission_rule(capsys):
    from beadhive import doctor

    for name, expected in (
        (OPEN, "registration admits"),
        (TRUSTED, "bh hq authority join"),
        (VERIFIED, "an operator grant admits"),
        (ATTESTED, "3 fresh authenticated heartbeats"),
    ):
        _learn(name)
        doctor._render_trust_profile(doctor._data_trust_profile({}))
        out = capsys.readouterr().out
        assert "    admission: " in out and expected in out, out


# ---- open / trusted / verified: who admits without a grant -----------------------------------


@pytest.fixture
def registration(monkeypatch):
    """The registration hook with join recorded instead of written."""
    from beadhive import host

    calls = []
    monkeypatch.setattr(host, "host_id", lambda: "host-two")
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda *a: "plane")
    monkeypatch.setattr(hq_authority_fleet_mode, "_local_public_key", lambda: "ssh-ed25519 K")
    monkeypatch.setattr(
        hq_open_admission, "join", lambda plane, host_id, key, **_: calls.append(host_id)
    )
    return calls


@pytest.mark.parametrize(
    ("profile", "admits"), [(OPEN, True), (TRUSTED, True), (VERIFIED, False), (ATTESTED, False)]
)
def test_registration_admits_only_where_the_profile_needs_no_grant(registration, profile, admits):
    _learn(profile)
    ae.record_fleet_default("trusted", head="h", via="test")  # even with a trusted column
    hq._open_admission("host-two")
    assert registration == (["host-two"] if admits else [])


def test_trusted_matches_0_24_0_join_auto_admit(registration):
    # 0.24.0: a trusted fleet default with no profile named auto-admits on registration ...
    ae.record_fleet_default("trusted", head="h", via="test")
    assert hq_trust_profile.learned() is None
    hq._open_admission("host-two")
    # ... and so does the named trusted profile, through the same join.
    _learn(TRUSTED)
    hq._open_admission("host-two")
    assert registration == ["host-two", "host-two"]
    assert hq_admission.join_permitted(ae.trust_profile({}))


def test_a_manual_policy_or_a_granting_profile_refuses_join():
    _learn(TRUSTED)
    hq_trust_profile.observe_admission(
        fleet_mode._snapshot("a" * 32, _fleet("manual")), authorised=False
    )
    with pytest.raises(ControlPlaneError, match="admits by operator-granted, not by join"):
        hq_open_admission.join("plane", "host-two", "ssh-ed25519 K")
    _learn(VERIFIED)
    ae.record_fleet_default("trusted", head="h", via="test")
    with pytest.raises(
        ControlPlaneError, match="verified trust profile admits by operator-granted"
    ):
        hq_open_admission.admission_guard()


def _manifest(b):
    return hosts.HostManifest(
        host_id="host-two",
        frame_id="frame-two",
        instance_ref="vm-two",
        release=b["desired"]["release"],
        capabilities=b["desired"]["caps"],
        os="linux",
        arch="x86_64",
        label="two",
        role="executor",
        identity={"kind": "none"},
    )


def _publish_profile(b, profile):
    fleet_mode._base_config(b)
    published = CliRunner().invoke(
        app,
        [
            "hq",
            "authority",
            f"profile-{profile}",
            "--operator-key",
            str(b["operator"]),
            "--confirm",
        ],
    )
    assert published.exit_code == 0, published.output
    assert ae.trust_profile({}).name == profile


def test_open_fleet_admits_a_registered_host_with_no_join_or_grant(backend, monkeypatch, tmp_path):
    from beadhive import host

    b = backend
    _publish_profile(b, OPEN)
    git_hq.key(tmp_path / "frame-two-runtime")
    public = (tmp_path / "frame-two-runtime.pub").read_text()
    manifest = _manifest(b)
    hosts.save(b["op_repo"], manifest)
    monkeypatch.setattr(host, "host_id", lambda: "host-two")
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda *a: b["plane"])
    monkeypatch.setattr(hq_authority_fleet_mode, "_local_public_key", lambda: public)

    hq._open_admission("host-two")  # the registration hook: no join verb, no grant

    _sha, state, _ = b["plane"]._read()
    record = state["frames"]["frame-two"]["active"]
    assert record["state"] == "active" and record["desired"]["declared"] is True
    desired = b["plane"].fetch_config("frame-two", holder_identity="host-two")
    decision = frame_eligibility.eligible(
        manifest, {}, fleet_mode._facts(manifest, desired, record)
    )
    assert dict(decision.predicates)["admitted_active"] is True and decision.allowed


def test_verified_fleet_leaves_an_ungranted_host_not_admitted(backend, monkeypatch, tmp_path):
    from beadhive import host

    b = backend
    _publish_profile(b, VERIFIED)
    git_hq.key(tmp_path / "frame-two-runtime")
    public = tmp_path / "frame-two-runtime.pub"
    hosts.save(b["op_repo"], _manifest(b))
    monkeypatch.setattr(host, "host_id", lambda: "host-two")
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda *a: b["plane"])
    monkeypatch.setattr(hq_authority_fleet_mode, "_local_public_key", public.read_text)

    hq._open_admission("host-two")
    joined = CliRunner().invoke(
        app,
        [
            "hq",
            "authority",
            "join",
            "--frame",
            "host-two",
            "--public-key",
            str(public),
            "--confirm",
        ],
    )
    assert joined.exit_code == 1 and "requires a trusted fleet" in joined.output
    _sha, state, _ = b["plane"]._read()
    assert "frame-two" not in (state.get("frames") or {})


# ---- attested: three fresh authenticated heartbeats, a stale one restarts the count ---------
#
# The count is advanced where each beat lands (the publisher records the streak in the signed
# beat commit) and read statelessly, so it never depends on how often eligibility is read.

TTL = 300.0
T0 = 1_000_000.0


def _streaks(*beats):
    """The streak each beat in ``(seq, observed_at)`` order records, publisher-side."""
    previous, out = None, []
    for seq, at in beats:
        streak = hb.next_streak(previous, seq, at)
        out.append(streak)
        previous = (streak, seq, at, TTL)
    return out


def test_the_publisher_counts_fresh_beats_and_a_stale_gap_restarts():
    assert _streaks((1, T0), (2, T0 + 60), (3, T0 + 120), (4, T0 + 180)) == [1, 2, 3, 4]
    # A beat landing at or past the previous one's TTL follows a stale gap: back to one.
    assert _streaks((1, T0), (2, T0 + 60), (3, T0 + 60 + TTL), (4, T0 + 120 + TTL)) == [1, 2, 1, 2]
    # A sequence that does not advance, or a beat older than the previous one, restarts.
    assert hb.next_streak((5, 7, T0, TTL), 7, T0 + 1) == 1
    assert hb.next_streak((5, 7, T0, TTL), 8, T0 - 1) == 1
    # A replaced beat that verified but recorded no streak (an older publisher) counts as one.
    assert hb.next_streak((0, 1, T0, TTL), 2, T0 + 60) == 2
    assert hb.recorded_streak("frame-lease/v2\n\nAttestation-Streak: 3\n") == 3
    assert hb.recorded_streak("frame-lease/v2\n") == 0


def _observation(streak, *, verified=True, fresh=True, candidate=False, carrier=""):
    return SimpleNamespace(
        attestation_streak=streak,
        verified=verified,
        fresh=fresh,
        candidate=candidate,
        carrier=carrier,
    )


def _admitted(observation):
    facts = frame_eligibility._profile_facts({}, observation=observation)
    stub = frame_eligibility.EligibilityFacts(
        observation,
        {},
        attestation_required=facts.get("attestation_required", 0),
        attested_beats=facts.get("attested_beats", 0),
    )
    return frame_eligibility._admitted_active({"state": "active", "declared": True}, stub)


def test_attested_grant_is_admitted_only_with_three_fresh_authenticated_beats():
    _learn(ATTESTED)
    assert not _admitted(_observation(2))
    assert _admitted(_observation(3))
    # The latest beat itself must be fresh, authenticated and the granted incarnation's.
    assert not _admitted(_observation(3, fresh=False))  # stale: the count is gone
    assert not _admitted(_observation(3, verified=False))
    assert not _admitted(_observation(3, candidate=True))
    # Carriers that record no streak fail closed.
    assert not _admitted(_observation(3, carrier="session"))
    assert not _admitted(None)


@pytest.mark.parametrize("profile", [OPEN, TRUSTED, VERIFIED])
def test_only_attested_counts_beats(profile):
    _learn(profile)
    assert "attestation_required" not in frame_eligibility._profile_facts(
        {}, observation=_observation(0)
    )
    assert _admitted(_observation(0))


def _beats(b, renewals, *, accept=False):
    """Publish one beat per renewal; `accept` also records each as the operator's observer
    would (the trusted receipt that makes the latest beat fresh for a candidate)."""
    for seq, at in enumerate(renewals, start=1):
        renew = datetime.fromtimestamp(at, UTC).isoformat()
        if accept:
            b["accept"](seq, renewTime=renew)
        else:
            git_hq.publish(b, b["lease"](seq, renewTime=renew))


def test_a_frame_read_once_after_three_good_beats_is_admitted(backend):
    """No eligibility read happens between the beats: the single read still sees three."""
    b = backend
    _learn(ATTESTED)
    base = time.time() - 200  # recent enough for the publisher's authority checks
    _beats(b, (base, base + 60, base + 120), accept=True)
    # Read at the operator receipt's own clock (never before it): the last beat is 80 s old.
    observation = b["plane"].observe(b["manifest"])
    assert observation.verified and observation.fresh
    assert observation.attestation_streak == 3
    # The fixture's frame is still a granted candidate; once admitted the same beat counts.
    assert hq_admission.attested_beats(replace(observation, candidate=False)) == 3
    assert _admitted(replace(observation, candidate=False))


def test_a_stale_gap_between_beats_restarts_the_recorded_count(backend):
    b = backend
    _learn(ATTESTED)
    base = time.time() - 1000
    _beats(b, (base, base + 60, base + 60 + TTL, base + 120 + TTL))
    observation = b["plane"].observe(b["manifest"], now=base + 121 + TTL)
    assert observation.attestation_streak == 2
    assert not _admitted(replace(observation, candidate=False))


def test_the_rule_reports_its_policy():
    payload = hq_admission.rule(trust_profiles.resolve(ATTESTED)).as_dict()
    assert payload["attestation_beats"] == 3 and payload["grant_required"]
    assert json.dumps(payload)
