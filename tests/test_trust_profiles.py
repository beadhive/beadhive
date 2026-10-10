"""Fleet trust profiles (trust-profiles ADR section 2, bh-bc2rj).

``hq.trust_profile`` is a fleet key: absent means ``verified`` (0.24.0's behaviour). Each
profile resolves five knobs and a capability ceiling; per-knob host overrides may only require
more trust unless the operator key signs them. Raising trust is learned only from an
operator-authorised config read, lowering always; on SQL HQ a key-less loosening fences signed
frames instead of taking effect.
"""

from __future__ import annotations

import pytest

import test_hq_authority_trusted as trusted_hq
from beadhive import (
    capabilities,
    frame_eligibility,
    hq_authority_enforce,
    hq_authority_fleet_mode,
    hq_operator_settings,
    hq_trust_profile,
    trust_profiles,
)
from beadhive.capabilities import ADMIN, AUTHOR, CLAIM, EXECUTE, PLACE, PUBLISH
from beadhive.hq_control_plane import ControlPlaneError
from beadhive.hq_hive_policy import config_head_tolerated
from beadhive.hq_sql_runtime import SqlRuntimeError
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot
from beadhive.trust_profiles import ATTESTED, OPEN, TRUSTED, VERIFIED, Override

ae = hq_authority_enforce
NOW = trusted_hq.NOW
HEAD_CONFIG = trusted_hq.HEAD_CONFIG
sql = trusted_hq.sql


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    for name in (ae.MODE_ENV, ae.ENFORCE_ENV, hq_operator_settings.ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ae, "_banner_emitted", False)
    ae.reset_cache()
    yield
    ae.reset_cache()


def _learn(profile):
    """What one authorised read of a fleet publishing `profile` teaches a host: the profile and
    its authority column."""
    hq_trust_profile.record(profile, head="h", via="test")
    column = trust_profiles.resolve(profile).authority_mode
    ae.record_fleet_default(column, head="h", via="test")


def _fleet(profile=None, default=None):
    hq = ""
    if profile or default:
        hq = "hq:\n"
        hq += f"  trust_profile: {profile}\n" if profile else ""
        hq += f"  default_authority_mode: {default}\n" if default else ""
    return FleetConfigDocument("fleet.yaml", f"schema_version: 1\n{hq}managed_repos: []\n")


def _snapshot(revision, *documents):
    return FleetConfigSnapshot(
        backend_identity="config",
        commit_revision=revision,
        generation="cgen",
        fetched_at=NOW,
        valid_until=NOW + 3600,
        documents=tuple(documents),
    )


# ---- the profile table --------------------------------------------------------------------

#: ADR section 2, one row per profile: authority, admission, placement, write policy, tenure.
TABLE = {
    OPEN: ("unsigned", "registration", "self", "open", "ceiling"),
    TRUSTED: ("trusted", "join", "self-or-director", "forward", "failover"),
    VERIFIED: ("signed", "operator-granted", "director", "forward", "bounded"),
    ATTESTED: ("signed-expiring", "granted-attested", "director-failover", "forward", "ttl"),
}


@pytest.mark.parametrize("name", trust_profiles.PROFILES)
def test_each_profile_resolves_the_five_knobs_per_the_adr_table(name):
    resolved = trust_profiles.resolve(name)
    authority, admission, placement, write_policy, tenure = TABLE[name]
    assert resolved.authority == authority
    assert resolved.admission == admission
    assert resolved.placement == placement
    assert resolved.write_policy == write_policy
    assert resolved.tenure.kind == tenure
    assert resolved.outcomes == ()


@pytest.mark.parametrize(
    ("name", "mode", "admission", "self_place", "fallback", "latency", "lapse"),
    [
        (OPEN, "trusted", "open", True, False, "next successful refresh", {AUTHOR, CLAIM}),
        (TRUSTED, "trusted", "manual", True, True, "one refresh interval", {AUTHOR, CLAIM}),
        (VERIFIED, "signed", "manual", False, True, "one refresh interval", set()),
        (ATTESTED, "signed", "manual", False, True, "immediate", set()),
    ],
)
def test_derived_dimensions_per_profile(
    name, mode, admission, self_place, fallback, latency, lapse
):
    resolved = trust_profiles.resolve(name)
    assert resolved.authority_mode == mode
    assert resolved.admission_policy == admission
    assert resolved.self_placement is self_place
    assert resolved.director_fallback is fallback
    assert resolved.revocation_latency == latency
    assert {c for c in capabilities.REGISTRY if resolved.continues_after_lapse(c)} == lapse
    assert resolved.failover_loop is (name == ATTESTED)
    assert resolved.lab_posture is (name == OPEN)


def test_absent_profile_is_verified_by_default():
    resolved = trust_profiles.resolve(None)
    assert resolved.name == VERIFIED and resolved.source == "default"
    assert resolved.doctor_line() == "profile verified (default)"
    assert hq_trust_profile.learned() is None
    assert ae.trust_profile({}).name == VERIFIED


def test_profiles_are_ordered_least_to_most_trust_required():
    assert trust_profiles.looser(OPEN, TRUSTED) and trust_profiles.looser(TRUSTED, VERIFIED)
    assert trust_profiles.looser(VERIFIED, ATTESTED) and not trust_profiles.looser(OPEN, OPEN)
    with pytest.raises(trust_profiles.UnknownProfile, match="verified"):
        trust_profiles.get("paranoid")


def test_tenure_per_profile():
    assert trust_profiles.resolve(OPEN).tenure.seconds(600) == 86400  # 24 h ceiling, not ∞
    assert trust_profiles.resolve(TRUSTED).tenure.seconds(3600) == 3600
    assert trust_profiles.resolve(VERIFIED).tenure.seconds(3600) == 3600
    attested = trust_profiles.resolve(ATTESTED).tenure
    assert attested.seconds(3600, heartbeat_ttl_s=90) == 90 and not attested.offline_grace
    bounded = trust_profiles.resolve(
        VERIFIED, [Override("tenure", "hq.tenure", 1800, "host.yaml")]
    ).tenure
    assert bounded.seconds(3600) == 1800 and bounded.seconds(900) == 900


# ---- the capability ceiling ---------------------------------------------------------------

ALL = frozenset(capabilities.REGISTRY)


@pytest.mark.parametrize(
    ("name", "operator_key", "director", "removed"),
    [
        (OPEN, False, False, set()),
        (TRUSTED, False, False, set()),
        (VERIFIED, False, False, {ADMIN, PLACE}),
        (VERIFIED, True, False, {PLACE}),
        (VERIFIED, False, True, {ADMIN}),
        (VERIFIED, True, True, set()),
        (ATTESTED, False, False, {ADMIN, PLACE}),
        (ATTESTED, True, True, set()),
    ],
)
def test_capability_ceiling_per_profile(name, operator_key, director, removed):
    ceiling = trust_profiles.resolve(name).ceiling(operator_key=operator_key, director=director)
    assert set(ceiling(ALL)) == removed


@pytest.mark.parametrize("name", trust_profiles.PROFILES)
def test_only_open_grants_admin_by_hq_write_access(name):
    resolved = trust_profiles.resolve(name)
    expected = {ADMIN} if name == OPEN else set()
    assert set(resolved.grants(hq_write_access=True)) == expected
    assert resolved.grants(hq_write_access=False) == frozenset()


def _frame(roles):
    class Frame:
        role = roles[0]

    Frame.roles = roles
    return Frame()


@pytest.mark.parametrize(
    ("name", "roles", "operator", "write", "expected"),
    [
        # verified removes self-placement and admin without the operator key.
        (VERIFIED, ["executor"], False, False, {AUTHOR, CLAIM, EXECUTE, PUBLISH}),
        (VERIFIED, ["operator"], False, False, {AUTHOR}),
        (VERIFIED, ["operator"], True, True, {AUTHOR, ADMIN}),
        # trusted removes nothing: the roles alone decide.
        (TRUSTED, ["executor"], False, False, {AUTHOR, CLAIM, EXECUTE, PUBLISH, PLACE}),
        (TRUSTED, ["planner"], False, True, {AUTHOR}),
        # open: any host with HQ write access holds admin, whatever its roles.
        (OPEN, ["planner"], False, True, {AUTHOR, ADMIN}),
        (OPEN, ["viewer"], False, False, set()),
    ],
)
def test_effective_capabilities_through_eligibility_facts(
    monkeypatch, name, roles, operator, write, expected
):
    _learn(name)
    monkeypatch.setattr(hq_trust_profile, "holds_operator_key", lambda: operator)
    monkeypatch.setattr(hq_trust_profile, "hq_write_access", lambda cfg=None: write)
    assert hq_trust_profile.OPERATOR_SETTINGS_ENV == hq_operator_settings.ENV
    facts = frame_eligibility.EligibilityFacts(None, {}, **frame_eligibility._profile_facts({}))
    assert frame_eligibility.granted_capabilities(_frame(roles), facts) == expected


# ---- attested: author needs a fresh authenticated heartbeat -------------------------------


def _decision(adds):
    predicates = tuple(
        (name, name != trust_profiles.FRESH_HEARTBEAT) for name in capabilities.FRAME_PREDICATES
    )
    return frame_eligibility.EligibilityDecision(
        predicates, granted=frozenset({AUTHOR, CLAIM}), capability_adds=adds
    )


@pytest.mark.parametrize("name", trust_profiles.PROFILES)
def test_author_needs_a_fresh_heartbeat_only_under_attested(name):
    decision = _decision(trust_profiles.resolve(name).capability_adds())
    author = decision.for_capability(AUTHOR)
    claim = decision.for_capability(CLAIM)
    if name == ATTESTED:
        assert not author.allowed and author.reason == trust_profiles.FRESH_HEARTBEAT
        assert not claim.allowed  # claim builds on author, so it inherits the predicate
    else:
        assert author.allowed and claim.allowed
        consulted = dict(author.predicates)
        assert trust_profiles.FRESH_HEARTBEAT not in consulted
        # identity, admission and cordon only
        assert {"current_frame_incarnation", "admitted_active", "not_cordoned"} <= set(consulted)


def test_attested_facts_carry_the_heartbeat_predicate(monkeypatch):
    _learn(ATTESTED)
    facts = frame_eligibility._profile_facts({})
    assert facts["capability_adds"] == ((AUTHOR, (trust_profiles.FRESH_HEARTBEAT,)),)
    assert trust_profiles.FRESH_HEARTBEAT in capabilities.frame_predicates(
        AUTHOR, adds=facts["capability_adds"]
    )
    assert trust_profiles.FRESH_HEARTBEAT not in capabilities.frame_predicates(AUTHOR)


# ---- per-knob overrides -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "override"),
    [
        (VERIFIED, Override("placement", "hq.placement_mode", "self", "host.yaml")),
        (VERIFIED, Override("write_policy", "hive.write_policy", "open", "host.yaml")),
        (TRUSTED, Override("write_policy", "hive.write_policy", "open", "host.yaml")),
        (OPEN, Override("tenure", "hq.tenure", 2 * 86400, "host.yaml")),
    ],
)
def test_an_unsigned_loosening_override_is_refused_naming_the_profile(name, override):
    resolved = trust_profiles.resolve(name, [override])
    assert resolved.knobs == trust_profiles.resolve(name).knobs  # the profile's value stays
    (refusal,) = resolved.refusals
    assert f"the {name} trust profile" in refusal.message
    assert override.key in refusal.message
    assert "override refused" in resolved.doctor_line()


@pytest.mark.parametrize(
    ("name", "override", "knob", "value"),
    [
        (TRUSTED, Override("placement", "hq.placement_mode", "director", "host.yaml"),
         "placement", "director"),
        (VERIFIED, Override("write_policy", "hive.write_policy", "fenced", "host.yaml"),
         "write_policy", "fenced"),
        (OPEN, Override("write_policy", "hive.write_policy", "forward", "host.yaml"),
         "write_policy", "forward"),
        (OPEN, Override("tenure", "hq.tenure", 3600, "host.yaml"), "tenure", 3600.0),
    ],
)  # fmt: skip
def test_a_tightening_override_is_accepted(name, override, knob, value):
    resolved = trust_profiles.resolve(name, [override])
    assert resolved.refusals == ()
    effective = resolved.knobs[knob]
    assert (effective.ceiling_s if knob == "tenure" else effective) == value
    assert resolved.overrides[0].effect == "tightened"
    assert f"{knob} overridden to" in resolved.doctor_line()


def test_a_signed_override_may_loosen_and_an_equal_one_changes_nothing():
    signed = Override("placement", "hq.placement_mode", "self", "fleet.yaml", signed=True)
    assert trust_profiles.resolve(VERIFIED, [signed]).placement == "self-or-director"
    equal = Override("placement", "hq.placement_mode", "director", "host.yaml")
    resolved = trust_profiles.resolve(ATTESTED, [equal])
    assert resolved.placement == "director-failover" and resolved.overrides[0].effect == (
        "unchanged"
    )


def test_an_invalid_override_value_is_refused():
    bad = Override("write_policy", "hive.write_policy", "anything", "host.yaml")
    (refusal,) = trust_profiles.resolve(VERIFIED, [bad]).refusals
    assert "must be one of" in refusal.message


def test_host_overrides_read_from_host_yaml():
    cfg = {"hq": {"placement_mode": "self", "tenure": 600}, "hive": {"write_policy": "fenced"}}
    keys = [override.key for override in hq_trust_profile.host_overrides(cfg)]
    assert keys == ["hq.placement_mode", "hive.write_policy", "hq.tenure"]
    resolved = ae.trust_profile(cfg)
    assert resolved.name == VERIFIED
    assert [o.override.key for o in resolved.refusals] == ["hq.placement_mode"]
    assert resolved.write_policy == "fenced" and resolved.tenure.ceiling_s == 600


def test_doctor_prints_the_profile_and_every_override_on_one_line():
    _learn(TRUSTED)
    data = hq_trust_profile.doctor_data(ae.trust_profile({"hq": {"placement_mode": "director"}}))
    assert data["level"] == "ok"
    assert data["line"] == (
        "profile trusted; placement overridden to director (hq.placement_mode, host.yaml)"
    )
    assert "\n" not in data["line"]


def test_doctor_prints_the_lab_posture_under_open_and_warns_on_a_refusal():
    _learn(OPEN)
    data = hq_trust_profile.doctor_data(ae.trust_profile({"hq": {"tenure": 3 * 86400}}))
    assert data["level"] == "warn"
    assert data["notes"][0] == hq_trust_profile.LAB_POSTURE
    assert "single-operator lab posture" in data["notes"][0]
    assert "the open trust profile" in data["notes"][1]


def test_an_explicit_signed_authority_under_open_turns_the_lab_posture_off():
    hq_trust_profile.record(OPEN, head="h", via="test")  # authority column stays signed
    resolved = ae.trust_profile({})
    assert resolved.authority == "signed" and not resolved.lab_posture
    assert resolved.grants(hq_write_access=True) == frozenset()


def test_the_explicit_authority_mode_reads_as_a_signed_override_of_the_first_column():
    ae.record_fleet_default("trusted", head="h", via="test")  # 0.24.0 mode-trusted
    resolved = ae.trust_profile({})
    assert resolved.name == VERIFIED and resolved.authority == "trusted"
    assert resolved.authority_mode == "trusted"
    assert "authority overridden to trusted (hq.default_authority_mode" in resolved.doctor_line()


# ---- the fleet key: carrier, learning and publication -------------------------------------


def test_the_fleet_key_is_fleet_owned_and_overrides_are_host_owned():
    from beadhive.modules.config import contracts
    from beadhive.modules.config.application import partition

    assert partition.partition_of("hq.trust_profile") == partition.FLEET
    for key in ("hq.placement_mode", "hq.tenure", "hive.write_policy"):
        assert partition.partition_of(key) == partition.HOST
    assert contracts.HqConfig().trust_profile == VERIFIED
    with pytest.raises(ValueError):
        contracts.HqConfig(trust_profile="lab")
    with pytest.raises(ValueError):
        contracts.HqConfig(placement_mode="anywhere")


def test_committed_authority_column_follows_the_profile_unless_set_explicitly():
    assert ae.committed_fleet_default([_fleet()]) == "signed"  # 0.24.0 unchanged
    assert ae.committed_fleet_default([_fleet(default="trusted")]) == "trusted"
    assert ae.committed_fleet_default([_fleet(OPEN)]) == "trusted"
    assert ae.committed_fleet_default([_fleet(TRUSTED)]) == "trusted"
    assert ae.committed_fleet_default([_fleet(VERIFIED)]) == "signed"
    assert ae.committed_fleet_default([_fleet(ATTESTED)]) == "signed"
    assert ae.committed_fleet_default([_fleet(TRUSTED, default="signed")]) == "signed"
    assert hq_trust_profile.committed([_fleet("bogus")]) is None


def test_learning_raises_only_when_authorised_and_always_lowers():
    raised = _snapshot("b" * 32, _fleet(TRUSTED))
    assert hq_trust_profile.observe(raised, authorised=False) == VERIFIED
    assert hq_trust_profile.learned() is None  # a key-less HQ write does not raise the fleet
    assert ae.observe_fleet_default(raised, authorised=True) == "trusted"
    assert hq_trust_profile.learned() == TRUSTED  # learned beside the authority column
    assert hq_trust_profile.state()["config_head"] == "b" * 32
    lowered = _snapshot("c" * 32, _fleet(ATTESTED))
    assert hq_trust_profile.observe(lowered, authorised=False) == ATTESTED
    assert hq_trust_profile.observe(raised, authorised=False) == ATTESTED


def test_with_fleet_default_signed_stays_explicit_under_a_trusted_profile():
    documents = (_fleet(TRUSTED),)
    lowered = hq_authority_fleet_mode.with_fleet_default(documents, "signed")
    assert ae.committed_fleet_default(lowered) == "signed"
    assert hq_trust_profile.committed(lowered) == TRUSTED
    plain = hq_authority_fleet_mode.with_fleet_default((_fleet(default="trusted"),), "signed")
    assert "default_authority_mode" not in plain[0].content


def test_with_profile_sets_and_verified_removes_the_key():
    published = hq_authority_fleet_mode.with_profile((_fleet(),), TRUSTED)
    assert hq_trust_profile.committed(published) == TRUSTED
    back = hq_authority_fleet_mode.with_profile(published, VERIFIED)
    assert "trust_profile" not in back[0].content


class _Store:
    def __init__(self, plane):
        self.plane = plane

    def load_snapshot(self):
        return self.plane.snapshot

    def publish_snapshot(self, documents, *, expected_revision):
        assert expected_revision == self.plane.snapshot.commit_revision
        self.plane.published.append(documents)
        self.plane.snapshot = _snapshot("p" * 32, *documents)
        return self.plane.snapshot


class _GitPlane:
    def __init__(self, profile=None):
        self.snapshot = _snapshot("a" * 32, _fleet(profile))
        self.published = []
        self.keys = []

    def config_store(self, operator_key=None):
        self.keys.append(operator_key)
        return _Store(self)


def test_raising_needs_the_operator_key_once_and_lowering_is_taken():
    plane = _GitPlane()
    with pytest.raises(ControlPlaneError, match="from verified to trusted needs the operator key"):
        hq_authority_fleet_mode.set_profile(plane, TRUSTED)
    assert plane.published == []
    result = hq_authority_fleet_mode.set_profile(plane, TRUSTED, operator_key="/k")
    assert result["published"] and result["profile"] == TRUSTED
    assert hq_trust_profile.learned() == TRUSTED and ae.fleet_default() == "trusted"
    assert plane.keys[-1] == "/k"
    # Now a trusted fleet: lowering is taken key-less (trusted mode publishes key-less).
    lowered = hq_authority_fleet_mode.set_profile(plane, VERIFIED)
    assert lowered["published"] and hq_trust_profile.learned() == VERIFIED
    assert ae.committed_fleet_default(plane.snapshot) == "signed"
    with pytest.raises(ControlPlaneError, match="unknown trust profile"):
        hq_authority_fleet_mode.set_profile(plane, "lab", operator_key="/k")


def test_profile_action_needs_confirm_and_shows_without_a_value(monkeypatch):
    plane = _GitPlane(TRUSTED)
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda *_a: plane)
    monkeypatch.setattr(hq_authority_fleet_mode, "refresh", lambda _plane: None)
    with pytest.raises(ControlPlaneError, match="requires --confirm"):
        hq_authority_fleet_mode.profile_cmd(ATTESTED)
    shown = hq_authority_fleet_mode.profile_cmd("")
    assert shown["committed_profile"] == TRUSTED and shown["profile"] == VERIFIED


# ---- the relevance set and SQL HQ ---------------------------------------------------------


def test_a_loosened_profile_is_frame_relevant_and_a_tightened_one_is_tolerated():
    bound = _snapshot("a" * 32, _fleet())
    tighter = _snapshot("b" * 32, _fleet(ATTESTED))
    looser = _snapshot("c" * 32, _fleet(OPEN))
    same_column = _snapshot("d" * 32, _fleet(VERIFIED))
    assert config_head_tolerated(bound, tighter, {}, valid_until=NOW + 60, now=NOW)
    assert config_head_tolerated(bound, same_column, {}, valid_until=NOW + 60, now=NOW)
    assert not config_head_tolerated(bound, looser, {}, valid_until=NOW + 60, now=NOW)
    assert not config_head_tolerated(tighter, bound, {}, valid_until=NOW + 60, now=NOW)


def test_sql_keyless_loosening_fences_a_signed_frame_and_is_not_learned(sql, monkeypatch):
    crossref = ("config", "cgen", HEAD_CONFIG)
    bound = _snapshot(HEAD_CONFIG, _fleet())
    moved = _snapshot("b" * 32, _fleet(OPEN))  # written by a key-less HQ writer
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: moved)
    tolerated = sql.authority._tolerated_bound_config

    def tolerate(cursor, crossref, current, policies, valid_until):
        # The real predicate, with the signed H0 standing in for the verified committed read.
        if config_head_tolerated(bound, current, policies, valid_until=valid_until, now=NOW):
            return bound
        return None

    assert tolerated is not None
    monkeypatch.setattr(sql.authority, "_tolerated_bound_config", tolerate)
    with pytest.raises(SqlRuntimeError, match="not bound"):
        sql.authority.bound_config_at(sql.cursor, crossref, policies={}, valid_until=NOW + 60)
    assert hq_trust_profile.learned() is None and ae.fleet_default() == "signed"
    assert ae.enforced()

    # A key-less TIGHTENING is tolerated and learned (lowering always takes effect).
    stricter = _snapshot("c" * 32, _fleet(ATTESTED))
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: stricter)
    snapshot, head = sql.authority.bound_config_at(
        sql.cursor, crossref, policies={}, valid_until=NOW + 60
    )
    assert snapshot is bound and head == "c" * 32
    assert hq_trust_profile.learned() == ATTESTED


def test_sql_operator_signed_binding_raises_the_profile(sql, monkeypatch):
    bound = _snapshot(HEAD_CONFIG, _fleet(TRUSTED))  # the head the signed authority names
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: bound)
    sql.authority.bound_config_at(sql.cursor, ("config", "cgen", HEAD_CONFIG))
    assert hq_trust_profile.learned() == TRUSTED
    assert ae.fleet_default() == "trusted" and ae.mode() == "trusted"


def test_the_profile_rides_in_the_authority_action(monkeypatch):
    import json

    from typer.testing import CliRunner

    from beadhive.cli import app

    plane = _GitPlane()
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda *_a: plane)
    monkeypatch.setattr(hq_authority_fleet_mode, "refresh", lambda _plane: None)
    runner = CliRunner()
    refused = runner.invoke(app, ["hq", "authority", "profile-attested"])
    assert refused.exit_code == 1 and "requires --confirm" in refused.output
    keyless = runner.invoke(app, ["hq", "authority", "profile-open", "--confirm"])
    assert keyless.exit_code == 1 and "needs the operator key" in keyless.output
    shown = runner.invoke(app, ["hq", "authority", "profile"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output.strip().splitlines()[-1])["profile"] == VERIFIED


def test_doctor_section_renders_the_one_line(capsys):
    from beadhive import doctor

    _learn(OPEN)
    data = doctor._data_trust_profile({})
    doctor._render_trust_profile(data)
    out = capsys.readouterr().out
    assert "# HQ trust profile" in out
    assert "✓ profile open" in out
    assert "single-operator lab posture" in out


# ---- trusted-mode frames honour the committed profile as-is; signed frames need a signature --


@pytest.mark.parametrize("raised", [TRUSTED, OPEN])
@pytest.mark.parametrize("how", ["pin", "fleet"])
def test_trusted_mode_frame_learns_an_unsigned_raise(monkeypatch, raised, how):
    if how == "pin":
        monkeypatch.setenv(ae.MODE_ENV, "trusted")
    else:
        ae.record_fleet_default("trusted", head="h", via="test")  # 0.24.0 mode-trusted fleet
    assert not ae.enforced() and hq_trust_profile.current() == VERIFIED
    unsigned = _snapshot("b" * 32, _fleet(raised))
    ae.observe_fleet_default(unsigned, authorised=False, via="sql")
    assert hq_trust_profile.learned() == raised
    assert ae.trust_profile({}).name == raised


@pytest.mark.parametrize("raised", [TRUSTED, OPEN])
def test_signed_mode_frame_ignores_an_unsigned_raise_and_takes_a_signed_one(raised):
    assert ae.enforced()
    unsigned = _snapshot("b" * 32, _fleet(raised))
    ae.observe_fleet_default(unsigned, authorised=False, via="sql")
    assert hq_trust_profile.learned() is None and ae.trust_profile({}).name == VERIFIED
    ae.observe_fleet_default(unsigned, authorised=True, via="sql")
    assert hq_trust_profile.learned() == raised


@pytest.mark.parametrize("raised", [TRUSTED, OPEN])
def test_sql_trusted_frame_learns_an_unbound_raise(sql, monkeypatch, raised):
    monkeypatch.setenv(ae.MODE_ENV, "trusted")
    moved = _snapshot("b" * 32, _fleet(raised))  # a key-less publication, head not bound
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: moved)
    snapshot, head = sql.authority.bound_config_at(sql.cursor, ("config", "cgen", HEAD_CONFIG))
    assert snapshot is moved and head == "b" * 32
    assert hq_trust_profile.learned() == raised
    assert ae.trust_profile({}).name == raised


@pytest.mark.parametrize("raised", [TRUSTED, OPEN])
def test_sql_signed_frame_fences_the_same_unsigned_raise_and_accepts_the_signed_one(
    sql, monkeypatch, raised
):
    crossref = ("config", "cgen", HEAD_CONFIG)
    bound = _snapshot(HEAD_CONFIG, _fleet())
    moved = _snapshot("b" * 32, _fleet(raised))
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: moved)

    def tolerate(cursor, crossref, current, policies, valid_until):
        if config_head_tolerated(bound, current, policies, valid_until=valid_until, now=NOW):
            return bound
        return None

    monkeypatch.setattr(sql.authority, "_tolerated_bound_config", tolerate)
    with pytest.raises(SqlRuntimeError, match="not bound"):
        sql.authority.bound_config_at(sql.cursor, crossref, policies={}, valid_until=NOW + 60)
    assert hq_trust_profile.learned() is None
    signed = _snapshot(HEAD_CONFIG, _fleet(raised))  # the head the signed authority names
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: signed)
    sql.authority.bound_config_at(sql.cursor, crossref)
    assert hq_trust_profile.learned() == raised


# ---- bh hq why: the profile layer -----------------------------------------------------------


def test_hq_why_profile_layer_reports_a_refused_override_and_the_signed_fix():
    from beadhive import config, hq_why, hq_why_layer_profile

    assert "profile" in [layer.name for layer in hq_why.layers()]
    ok = hq_why_layer_profile.check_profile(hq_why.WhyContext("tt", "author", cfg={}))
    assert ok.status == hq_why.SKIPPED and "profile verified" in ok.summary
    ctx = hq_why.WhyContext("tt", "author", cfg={"hq": {"placement_mode": "self"}})
    verdict = hq_why_layer_profile.check_profile(ctx)
    assert verdict.status == hq_why.OK  # the write still runs under the profile
    assert verdict.code == "profile_override_refused"
    assert "hq.placement_mode=self" in verdict.summary and "verified" in verdict.summary
    assert verdict.detail["knob"] == "placement" and verdict.detail["profile"] == VERIFIED
    assert verdict.command == (
        f"{config.BINARY_ALIAS} hq authority profile-trusted --operator-key <key> --confirm"
    )
    tenure = Override("tenure", "hq.tenure", 3 * 86400, "host.yaml")
    assert hq_why_layer_profile.admitting_profile(tenure) == ATTESTED  # refused only by open
    assert (
        hq_why_layer_profile.admitting_profile(
            Override("write_policy", "hive.write_policy", "open", "host.yaml")
        )
        == OPEN
    )
