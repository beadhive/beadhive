"""work-setting resolution — demo_cmd follows the per-rig > global > default tiers (work_value)."""

from __future__ import annotations

from beadhive import config


def test_demo_cmd_default_empty_when_unset():
    assert config.demo_cmd({}, None) == ""
    assert config.demo_cmd({"work": {}}, {}) == ""


def test_demo_cmd_global_then_per_rig_override():
    cfg = {"work": {"demo_cmd": "just demo"}}
    # global wins when the rig has no override
    assert config.demo_cmd(cfg, {}) == "just demo"
    # per-rig entry overrides the global
    assert config.demo_cmd(cfg, {"work": {"demo_cmd": "make run"}}) == "make run"


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


# ---- work.dispatch.* accessors (per-rig > global > default, one level deeper) ----


def test_dispatch_mode_default_and_override():
    # default-when-unset
    assert config.dispatch_mode({}, None) == "fanout"
    assert config.dispatch_mode({"work": {"dispatch": {}}}, {}) == "fanout"
    # per-rig override beats the global default
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
    # default is advisory (advise), not a blanket block (bead .39)
    assert config.dispatch_reviewer_cross_seat({}, None) == "advise"
    glob = {"work": {"dispatch": {"reviewer_cross_seat": "hard"}}}
    assert config.dispatch_reviewer_cross_seat(glob, {}) == "hard"
    # per-rig override wins over global
    rig = {"work": {"dispatch": {"reviewer_cross_seat": "hard"}}}
    assert config.dispatch_reviewer_cross_seat({"work": {"dispatch": {}}}, rig) == "hard"
    # unknown value falls back to advise
    bad = {"work": {"dispatch": {"reviewer_cross_seat": "x"}}}
    assert config.dispatch_reviewer_cross_seat(bad, {}) == "advise"


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
