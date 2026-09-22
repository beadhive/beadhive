"""The trivial-change policy short-circuit (bh-xg1r9).

Every test here pins a SAFETY property. The policy exists to trade coverage for latency on
changes that cannot affect behavior, so the interesting assertions are all about the cases
where it must decline to act.
"""

from __future__ import annotations

import pytest

from beadhive.modules.config.contracts import AttestConfig, AttestKeyConfig, AttestTrivialConfig
from beadhive.selective_validation import (
    TRIVIAL_NO,
    TRIVIAL_UNKNOWN,
    TRIVIAL_YES,
    path_verdict,
    trivial_selection,
)

KEYS = tuple(
    AttestKeyConfig(name=n, cmd=f"just attest-{n}")
    for n in ("docs", "unit", "integration", "stateful")
)


def attest(*, enabled=True, paths=("docs/*.md",), keys=("docs",)):
    return AttestConfig(
        keys=list(KEYS),
        trivial=AttestTrivialConfig(enabled=enabled, paths=list(paths), keys=list(keys)),
    )


# ---- path verdict ------------------------------------------------------------


def test_all_paths_matching_is_yes():
    assert path_verdict(attest(), ("docs/design/a.md", "docs/b.md")) == TRIVIAL_YES


def test_one_unmatched_path_is_no():
    """A single code file restores the normal route: the policy is all-or-nothing."""
    assert path_verdict(attest(), ("docs/a.md", "src/beadhive/work.py")) == TRIVIAL_NO


def test_unconfigured_is_unknown_not_no():
    """An unconfigured hive never looked; that is different from having judged and refused."""
    assert path_verdict(attest(enabled=False), ("docs/a.md",)) == TRIVIAL_UNKNOWN
    assert path_verdict(attest(paths=()), ("docs/a.md",)) == TRIVIAL_UNKNOWN
    assert path_verdict(attest(keys=()), ("docs/a.md",)) == TRIVIAL_UNKNOWN


def test_no_changed_paths_is_unknown():
    assert path_verdict(attest(), ()) == TRIVIAL_UNKNOWN


# ---- selection ---------------------------------------------------------------


def test_trivial_change_runs_only_the_configured_keys():
    selected, record = trivial_selection(attest(), ("docs/a.md",), KEYS)
    assert [k.name for k in selected] == ["docs"]
    assert record["applied"] is True and record["ran"] == ["docs"]


def test_non_trivial_change_takes_the_normal_route():
    selected, record = trivial_selection(attest(), ("src/beadhive/work.py",), KEYS)
    assert selected is None and record["applied"] is False


@pytest.mark.parametrize("enabled", [False, True])
def test_policy_never_skips_every_key(enabled):
    """trivial.keys naming nothing real must fall through, never skip the whole gate."""
    selected, record = trivial_selection(
        attest(enabled=enabled, keys=("nonexistent",)), ("docs/a.md",), KEYS
    )
    assert selected is None
    assert record["applied"] is False


# ---- the two judges must agree -----------------------------------------------


def test_triage_no_vetoes_globs_that_are_too_broad():
    """THE misconfiguration this guards: globs say trivial, the change is not."""
    selected, record = trivial_selection(
        attest(paths=("**",)), ("src/beadhive/work.py",), KEYS, triage=lambda _p: TRIVIAL_NO
    )
    assert selected is None
    assert "too broad" in record["disagreement"]


def test_triage_yes_cannot_skip_on_its_own():
    """A triage alone can never skip: globs that are too narrow are a missed win, not a risk."""
    selected, record = trivial_selection(
        attest(paths=("nope/*",)), ("docs/a.md",), KEYS, triage=lambda _p: TRIVIAL_YES
    )
    assert selected is None
    assert "too narrow" in record["disagreement"]


def test_agreement_applies_the_fast_path():
    selected, record = trivial_selection(
        attest(), ("docs/a.md",), KEYS, triage=lambda _p: TRIVIAL_YES
    )
    assert [k.name for k in selected] == ["docs"]
    assert record["triage_verdict"] == TRIVIAL_YES


def test_triage_unknown_still_allows_the_glob_fast_path():
    """UNKNOWN is not a veto; it is the absence of a second opinion."""
    selected, _ = trivial_selection(
        attest(), ("docs/a.md",), KEYS, triage=lambda _p: TRIVIAL_UNKNOWN
    )
    assert [k.name for k in selected] == ["docs"]


def test_triage_that_raises_is_unknown_and_never_fails_the_run():
    def boom(_paths):
        raise RuntimeError("network down")

    selected, record = trivial_selection(attest(), ("docs/a.md",), KEYS, triage=boom)
    assert [k.name for k in selected] == ["docs"]
    assert "network down" in record["triage_error"]


def test_triage_returning_garbage_is_treated_as_unknown():
    selected, record = trivial_selection(attest(), ("docs/a.md",), KEYS, triage=lambda _p: "maybe")
    assert [k.name for k in selected] == ["docs"]
    assert "unknown triage verdict" in record["triage_error"]


# ---- config degradation ------------------------------------------------------


def test_absent_trivial_section_defaults_to_off():
    """Today's behavior is the default: a hive must opt in to trade coverage for latency."""
    cfg = AttestConfig(keys=list(KEYS))
    assert cfg.trivial.enabled is False
    assert path_verdict(cfg, ("docs/a.md",)) == TRIVIAL_UNKNOWN


def test_fnmatch_star_crosses_directory_separators():
    """Pinned because it is the misconfiguration this policy is most likely to suffer.

    These are fnmatch patterns, not pathlib globs: '*' crosses '/', so 'docs/*' silently
    admits 'docs/schemas/thing.json' -- a checked-in schema that code may well load. The
    semantic triage's veto is what makes that survivable.
    """
    assert path_verdict(attest(), ("docs/deeply/nested/a.md",)) == TRIVIAL_YES
    assert path_verdict(attest(paths=("docs/*",)), ("docs/schemas/thing.json",)) == TRIVIAL_YES
    assert path_verdict(attest(paths=("docs/*",)), ("src/beadhive/work.py",)) == TRIVIAL_NO
