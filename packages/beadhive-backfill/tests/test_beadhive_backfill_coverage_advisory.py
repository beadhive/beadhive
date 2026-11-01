"""The advisory reading of a cached coverage state: thresholds per ratio and the never/stale
states. Pure: the cache reads are literals, no clock, no I/O beyond a tmp file."""

from __future__ import annotations

from pathlib import Path

from beadhive_backfill.coverage import (
    CacheCheck,
    CacheState,
    CoverageReport,
    CoverageSettings,
    Ratio,
    check_cache,
    write_coverage_cache,
)
from beadhive_backfill.coverage_advisory import (
    DEFAULT_WARN_BELOW,
    REFRESH_HINT,
    AdvisoryState,
    evaluate,
)

STAMP = "2026-11-01T00:00:00Z"


def report(beads=(6, 10), commits=(9, 10), docs=(7, 10), **kw) -> CoverageReport:
    return CoverageReport(
        computed_at=STAMP,
        inputs_hash="sha256:" + "0" * 64,
        group=None,
        settings=kw.pop("settings", CoverageSettings()),
        beads=Ratio(*beads),
        commits=Ratio(*commits),
        docs=Ratio(*docs),
    )


def unverified(rep: CoverageReport) -> CacheCheck:
    return CacheCheck(CacheState.UNVERIFIED, rep, "inputs not compared")


def test_default_threshold_is_point_six() -> None:
    assert DEFAULT_WARN_BELOW == 0.60


def test_ratios_at_or_above_the_threshold_are_ok() -> None:
    # 0.60 is not below 0.60; strictly-below warns.
    adv = evaluate(unverified(report(beads=(6, 10))), warn_below=0.60)
    assert adv.state is AdvisoryState.OK and adv.below == () and adv.hint == ""


def test_each_ratio_is_judged_on_its_own() -> None:
    adv = evaluate(unverified(report(beads=(5, 10), commits=(9, 10), docs=(1, 10))))
    assert adv.state is AdvisoryState.WARN
    assert [r.key for r in adv.below] == ["closed_beads_with_commits", "docs_linked_to_bead"]
    assert adv.hint == REFRESH_HINT
    line = adv.summary()
    assert "beads with commits 50.0% (5/10) !" in line
    assert "commits with beads 90.0% (9/10)," in line and "docs with beads 10.0% (1/10) !" in line


def test_an_empty_ratio_has_no_value_and_never_warns() -> None:
    adv = evaluate(unverified(report(beads=(0, 0))), warn_below=0.9)
    (empty,) = [r for r in adv.readings if r.total == 0]
    assert empty.value is None and not empty.below and empty.percent == "n/a"


def test_the_threshold_is_configurable() -> None:
    check = unverified(report(beads=(5, 10), commits=(5, 10), docs=(5, 10)))
    assert evaluate(check, warn_below=0.5).state is AdvisoryState.OK
    assert evaluate(check, warn_below=0.51).state is AdvisoryState.WARN
    assert evaluate(check, warn_below=0.0).state is AdvisoryState.OK


def test_a_missing_cache_is_never_computed_with_a_refresh_hint(tmp_path: Path) -> None:
    adv = evaluate(check_cache(tmp_path / "coverage.json"))
    assert adv.state is AdvisoryState.NEVER and adv.readings == ()
    assert adv.detail == "never computed" and adv.hint == REFRESH_HINT
    assert "never computed" in adv.summary() and "--refresh" in adv.summary()


def test_an_unusable_cache_is_stale_and_shows_no_figures(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    path.write_text("{ not json")
    adv = evaluate(check_cache(path))
    assert adv.state is AdvisoryState.STALE and adv.readings == ()
    assert adv.detail.startswith("stale: unusable cache") and adv.hint == REFRESH_HINT


def test_a_cache_version_this_build_does_not_read_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    write_coverage_cache(path, report())
    path.write_text(path.read_text().replace('"version": 1', '"version": 99'))
    assert evaluate(check_cache(path)).state is AdvisoryState.STALE


def test_a_cache_measured_with_other_settings_is_stale() -> None:
    cached = report(settings=CoverageSettings(code_types=("bug",)))
    adv = evaluate(unverified(cached), settings=CoverageSettings())
    assert adv.state is AdvisoryState.STALE and adv.readings == ()
    assert adv.computed_at == STAMP and "code_types/doc_roots" in adv.detail
    same = evaluate(unverified(report()), settings=CoverageSettings(example_limit=1))
    assert same.state is AdvisoryState.OK  # the example limit is not part of what is measured


def test_a_caller_with_the_inputs_gets_its_stale_verdict_honoured() -> None:
    check = CacheCheck(CacheState.STALE, report(), "inputs changed since then")
    adv = evaluate(check)
    assert adv.state is AdvisoryState.STALE and adv.readings == ()
    assert adv.detail == "stale: inputs changed since then"


def test_to_json_carries_the_verdict_and_the_readings() -> None:
    data = evaluate(unverified(report(beads=(1, 10)))).to_json()
    assert data["state"] == "warn" and data["warn_below"] == 0.6 and data["computed_at"] == STAMP
    first = data["ratios"][0]
    assert first == {
        "key": "closed_beads_with_commits",
        "label": "beads with commits",
        "linked": 1,
        "total": 10,
        "ratio": 0.1,
        "below": True,
    }
