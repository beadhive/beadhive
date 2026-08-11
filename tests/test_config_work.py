"""work-setting resolution — demo_cmd follows the per-hive > global > default tiers (work_value)."""

from __future__ import annotations

from beadhive import config


def test_demo_cmd_default_empty_when_unset():
    assert config.demo_cmd({}, None) == ""
    assert config.demo_cmd({"work": {}}, {}) == ""


def test_demo_cmd_global_then_per_hive_override():
    cfg = {"work": {"demo_cmd": "just demo"}}
    # global wins when the hive has no override
    assert config.demo_cmd(cfg, {}) == "just demo"
    # per-hive entry overrides the global
    assert config.demo_cmd(cfg, {"work": {"demo_cmd": "make run"}}) == "make run"


def test_claim_authority_default_local_and_per_hive_override():
    """work.identity.authority (bh-ejlq): default `local` (the only ClaimAuthority tier shipped
    today), per-hive overrides global, matching the release.conflict_estimator layering shape."""
    assert config.claim_authority({}, None) == "local"
    glob = {"work": {"identity": {"mode": "agent", "authority": "local"}}}
    assert config.claim_authority(glob, {}) == "local"
    hive = {"work": {"identity": {"authority": "signed-token"}}}
    assert config.claim_authority(glob, hive) == "signed-token"  # per-hive wins over global


def test_validate_cmd_default_and_per_phase():
    assert config.validate_cmd({}, None) == "just check"  # hard default
    cfg = {"work": {"validate_cmd": "just check", "validate": {"molecule": "just check-all"}}}
    assert config.validate_cmd(cfg, {}, "molecule") == "just check-all"  # per-phase override
    assert config.validate_cmd(cfg, {}, "submit") == "just check"  # unset phase → validate_cmd


def test_validate_cmd_main_gate_prefers_phase_main_variant():
    cfg = {"work": {"validate_cmd": "just check", "validate": {"merge-main": "just check-all"}}}
    # ad-hoc bead → main: main_gate prefers the `-main` variant
    assert config.validate_cmd(cfg, {}, "merge", main_gate=True) == "just check-all"
    # molecule member → mol/<epic>: plain phase, falls through to validate_cmd
    assert config.validate_cmd(cfg, {}, "merge", main_gate=False) == "just check"
    # main_gate falls back to the plain phase when no `-main` key exists
    cfg2 = {"work": {"validate_cmd": "just check", "validate": {"merge": "just test"}}}
    assert config.validate_cmd(cfg2, {}, "merge", main_gate=True) == "just test"


# ---- validate_cmd "is it named" (bh-l44i) — "does it look like it runs tests" moved to
# validate_probe.probe_validate_cmd (see tests/test_validate_probe.py): a resolve, not a guess.


def test_validate_cmd_is_configured_false_when_unset():
    assert config.validate_cmd_is_configured({}, None) is False
    assert config.validate_cmd_is_configured({"work": {}}, {}) is False


def test_validate_cmd_is_configured_true_for_global_or_per_hive_override():
    glob = {"work": {"validate_cmd": "just check"}}
    assert config.validate_cmd_is_configured(glob, {}) is True  # named, even if same text
    hive = {"work": {"validate_cmd": "make check"}}
    assert config.validate_cmd_is_configured({}, hive) is True


# ---- work.landing / work.push_remote (the pr landing mode, bh-v0wu) ----


def test_work_landing_default_local_and_override():
    # default-when-unset — landing=local must be byte-identical to pre-feature behavior
    assert config.work_landing({}, None) == "local"
    assert config.work_landing({"work": {}}, {}) == "local"
    # global then per-hive override
    glob = {"work": {"landing": "pr"}}
    assert config.work_landing(glob, {}) == "pr"
    assert config.work_landing(glob, {"work": {"landing": "local"}}) == "local"
    # unknown values fall back to local
    assert config.work_landing({"work": {"landing": "bogus"}}, {}) == "local"


def test_push_remote_default_origin_and_override():
    assert config.push_remote({}, None) == "origin"
    glob = {"work": {"push_remote": "upstream"}}
    assert config.push_remote(glob, {}) == "upstream"
    assert config.push_remote(glob, {"work": {"push_remote": "fork"}}) == "fork"


# ---- kind=external (contribution) push target + PR base (bh-uxam.2) --------


def test_push_remote_forces_origin_for_external_hive_ignoring_any_override():
    """The fork onboarding forked+cloned us write access to (bh-uxam.1) is always `origin` —
    a `work.push_remote` override (meant for same-repo-family knobs like `landing: pr`) must
    never redirect a contribution's push at `upstream`, which stays pull-only."""
    glob = {"work": {"push_remote": "upstream"}}
    entry = {"kind": "external", "work": {"push_remote": "upstream"}}
    assert config.push_remote(glob, entry) == "origin"
    assert config.push_remote({}, {"kind": "external"}) == "origin"


def test_pr_base_defaults_to_integration_branch():
    assert config.pr_base({}, None) == "main"
    cfg = {"work": {"integration_branch": "develop"}}
    assert config.pr_base(cfg, {}) == "develop"
    # per-hive override still layers as usual
    assert config.pr_base(cfg, {"work": {"integration_branch": "trunk"}}) == "trunk"


# ---- work.dispatch.* accessors (per-hive > global > default, one level deeper) ----


def test_dispatch_mode_default_and_override():
    # default-when-unset
    assert config.dispatch_mode({}, None) == "fanout"
    assert config.dispatch_mode({"work": {"dispatch": {}}}, {}) == "fanout"
    # per-hive override beats the global default
    glob = {"work": {"dispatch": {"mode": "collapsed"}}}
    assert config.dispatch_mode(glob, {}) == "collapsed"
    assert config.dispatch_mode(glob, {"work": {"dispatch": {"mode": "auto"}}}) == "auto"
    # unknown value falls back to fanout
    assert config.dispatch_mode({"work": {"dispatch": {"mode": "bogus"}}}, {}) == "fanout"


def test_dispatch_max_depth_default_and_override():
    assert config.dispatch_max_depth({}, None) == 2
    glob = {"work": {"dispatch": {"max_depth": 1}}}
    assert config.dispatch_max_depth(glob, {}) == 1
    assert config.dispatch_max_depth(glob, {"work": {"dispatch": {"max_depth": 0}}}) == 0
    # out-of-range clamps to 2
    assert config.dispatch_max_depth({"work": {"dispatch": {"max_depth": 5}}}, {}) == 2


def test_dispatch_max_beads_per_session_default_and_override():
    assert config.dispatch_max_beads_per_session({}, None) == 8
    glob = {"work": {"dispatch": {"max_beads_per_session": 4}}}
    assert config.dispatch_max_beads_per_session(glob, {}) == 4
    assert (
        config.dispatch_max_beads_per_session(
            glob, {"work": {"dispatch": {"max_beads_per_session": 12}}}
        )
        == 12
    )


def test_dispatch_auto_budget_default_and_override():
    assert config.dispatch_auto_budget({}, None) == 8
    glob = {"work": {"dispatch": {"auto_budget": 3}}}
    assert config.dispatch_auto_budget(glob, {}) == 3
    assert config.dispatch_auto_budget(glob, {"work": {"dispatch": {"auto_budget": 16}}}) == 16


def test_dispatch_review_mode_default_and_override():
    assert config.dispatch_review_mode({}, None) == "self"
    glob = {"work": {"dispatch": {"review_mode": "fresh"}}}
    assert config.dispatch_review_mode(glob, {}) == "fresh"
    # unknown value falls back to self
    assert config.dispatch_review_mode({"work": {"dispatch": {"review_mode": "x"}}}, {}) == "self"


def test_dispatch_reviewer_cross_seat_default_and_override():
    # default is `hard` (bh-e5kv): self-approval of a type:human gate is blocked deterministically,
    # not merely advised — a rig opts back into the advisory-only behavior explicitly.
    assert config.dispatch_reviewer_cross_seat({}, None) == "hard"
    glob = {"work": {"dispatch": {"reviewer_cross_seat": "advise"}}}
    assert config.dispatch_reviewer_cross_seat(glob, {}) == "advise"
    # per-hive override wins over global
    hive = {"work": {"dispatch": {"reviewer_cross_seat": "advise"}}}
    assert config.dispatch_reviewer_cross_seat({"work": {"dispatch": {}}}, hive) == "advise"
    # unknown value falls back to hard (fail closed)
    bad = {"work": {"dispatch": {"reviewer_cross_seat": "x"}}}
    assert config.dispatch_reviewer_cross_seat(bad, {}) == "hard"


def test_dispatch_max_concurrency_default_override_and_clamp():
    """The ONE concurrency cap. Default 2, per-hive overridable, and values below 1 CLAMP TO 1
    — a 0 here can never mean "unlimited" (the deleted `max_seats_in_flight` read it that way,
    which is precisely why two keys for one cap was unsafe rather than merely redundant)."""
    assert config.dispatch_max_concurrency({}, None) == 2
    glob = {"work": {"dispatch": {"max_concurrency": 4}}}
    assert config.dispatch_max_concurrency(glob, {}) == 4
    assert (
        config.dispatch_max_concurrency(glob, {"work": {"dispatch": {"max_concurrency": 2}}}) == 2
    )
    assert config.dispatch_max_concurrency({"work": {"dispatch": {"max_concurrency": 0}}}, {}) == 1


def test_dispatch_max_run_seconds_default_and_override():
    """The ONE per-run wall-time cap. Default 1800s; 0 disables it."""
    assert config.dispatch_max_run_seconds({}, None) == 1800.0
    glob = {"work": {"dispatch": {"max_run_seconds": 900}}}
    assert config.dispatch_max_run_seconds(glob, {}) == 900
    assert (
        config.dispatch_max_run_seconds(glob, {"work": {"dispatch": {"max_run_seconds": 300}}})
        == 300
    )
    assert config.dispatch_max_run_seconds({"work": {"dispatch": {"max_run_seconds": 0}}}, {}) == 0


def test_the_dead_cap_accessors_are_gone():
    """`dispatch_caps.py` was a second decision core with zero production callers, behind four
    config keys for two caps with OPPOSITE zero-sentinel semantics. One set of keys, one
    spelling, one sentinel rule — assert the other set cannot quietly return."""
    assert not hasattr(config, "dispatch_max_seats_in_flight")
    assert not hasattr(config, "dispatch_max_run_wall_time_seconds")


def test_dispatch_review_mode_paired_falls_back_to_fresh_with_warning(monkeypatch):
    # paired is out of scope (depends on the resumable-agent spike): it must fall back
    # to fresh WITH a warning, never silently no-op.
    warnings: list[tuple] = []

    class _Logger:
        def warning(self, event, **kw):
            warnings.append((event, kw))

    monkeypatch.setattr("beadhive.log.get_logger", lambda *_a, **_k: _Logger())

    result = config.dispatch_review_mode({"work": {"dispatch": {"review_mode": "paired"}}}, {})

    assert result == "fresh"
    assert [e for e, _ in warnings] == ["review_mode_paired_fallback"]
    assert warnings[0][1]["requested"] == "paired"
    assert warnings[0][1]["effective"] == "fresh"
