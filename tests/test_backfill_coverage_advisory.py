"""The advisory coverage check of ``bh hive ready`` and ``bh doctor`` (docs/BACKFILL.md
"Coverage"): off by default, a single cache read when on, never a failure, never a recompute.

The cache is a real file under a tmp bh home; the recompute paths (``hive_readers``, bd export,
``git log``, doc walk) are a counting fake that must stay at zero. No clock and no live hive.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from beadhive import backfill_cli, backfill_coverage, doctor, hive_ready
from beadhive.modules.config.contracts import BackfillCoverageConfig
from beadhive_backfill.coverage import (
    CoverageReport,
    CoverageSettings,
    Ratio,
    write_coverage_cache,
)

ENTRY = {"provider": "github", "org": "octo", "repo": "widgets", "prefix": "wd", "kind": "personal"}
REFRESH = "bh backfill coverage --refresh"
STAMP = "2026-11-01T00:00:00Z"


class Forbidden:
    """Counts every attempt at a recompute (bd export, git log, doc walk, full measure)."""

    NAMES = (
        "hive_readers",
        "measure_hive",
        "refresh_cache",
        "read_commits",
        "require_full_history",
        "discover_docs",
    )

    def __init__(self, monkeypatch) -> None:
        self.calls: Counter[str] = Counter()
        for name in self.NAMES:
            monkeypatch.setattr(backfill_coverage, name, self._trap(name))
        monkeypatch.setattr(backfill_cli, "store_loader", self._trap("store_loader"))

    def _trap(self, name: str):
        def trapped(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            raise AssertionError(f"{name} must not run for the advisory check")

        return trapped


class CacheReads:
    """Counts reads of the cache file through the one reader the advisory uses."""

    def __init__(self, monkeypatch) -> None:
        self.count = 0
        real = backfill_coverage.check_cache

        def counting(*args: Any, **kwargs: Any):
            self.count += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(backfill_coverage, "check_cache", counting)


def _cfg(*, enabled: bool | None = None, **coverage: Any) -> dict[str, Any]:
    section = dict(coverage)
    if enabled is not None:
        section["enabled"] = enabled
    return {"managed_repos": [ENTRY], "backfill": {"coverage": section} if section else {}}


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _cache(home: Path, *, beads=(8, 10), commits=(9, 10), docs=(7, 10), **kw: Any) -> Path:
    report = CoverageReport(
        computed_at=STAMP,
        inputs_hash="sha256:" + "0" * 64,
        group=None,
        settings=kw.pop("settings", CoverageSettings()),
        beads=Ratio(*beads),
        commits=Ratio(*commits),
        docs=Ratio(*docs),
    )
    path = backfill_coverage.state_path(ENTRY, home, home)
    write_coverage_cache(path, report)
    return path


# -- config ----------------------------------------------------------------------------------


def test_warn_below_defaults_to_point_six_per_ratio_and_is_bounded() -> None:
    assert BackfillCoverageConfig().warn_below == 0.60
    assert backfill_coverage.warn_below({}) == 0.60
    assert backfill_coverage.warn_below(_cfg(warn_below=0.25)) == 0.25
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError):
            BackfillCoverageConfig(warn_below=bad)


# -- disabled: the cache is never read -------------------------------------------------------


@pytest.mark.parametrize("cfg", [_cfg(), _cfg(enabled=False), _cfg(enabled=False, warn_below=0.9)])
def test_disabled_neither_surface_reads_the_cache(home, monkeypatch, cfg) -> None:
    _cache(home)
    reads, forbidden = CacheReads(monkeypatch), Forbidden(monkeypatch)

    assert hive_ready._link_coverage_check(cfg, ENTRY) == []
    assert doctor._data_link_coverage(cfg) == []

    assert reads.count == 0 and not forbidden.calls
    assert backfill_coverage.advisory_for(cfg, ENTRY, home, home) is None


def test_disabled_doctor_renders_nothing(home, capsys) -> None:
    doctor._render_link_coverage(doctor._data_link_coverage(_cfg()))
    assert capsys.readouterr().out == ""


# -- enabled: ratios, thresholds, hints ------------------------------------------------------


def test_enabled_good_ratios_are_an_ok_line_from_one_cache_read(home, monkeypatch) -> None:
    _cache(home, beads=(8, 10), commits=(9, 10), docs=(7, 10))
    reads, forbidden = CacheReads(monkeypatch), Forbidden(monkeypatch)

    (check,) = hive_ready._link_coverage_check(_cfg(enabled=True), ENTRY)

    assert (check.label, check.required, check.state) == ("link coverage", False, "ok")
    assert "beads with commits 80.0% (8/10)" in check.detail and "!" not in check.detail
    assert reads.count == 1 and not forbidden.calls


def test_ratios_below_the_threshold_warn_and_never_require(home, monkeypatch) -> None:
    _cache(home, beads=(2, 10), commits=(9, 10), docs=(1, 10))
    forbidden = Forbidden(monkeypatch)

    (check,) = hive_ready._link_coverage_check(_cfg(enabled=True), ENTRY)

    assert (check.required, check.state) == (False, "warn")
    assert "beads with commits 20.0% (2/10) !" in check.detail
    assert "commits with beads 90.0% (9/10)," in check.detail
    assert "docs with beads 10.0% (1/10) !" in check.detail
    assert REFRESH in check.detail and not forbidden.calls


def test_the_configured_threshold_moves_the_warning(home) -> None:
    _cache(home, beads=(8, 10), commits=(8, 10), docs=(8, 10))
    assert (
        hive_ready._link_coverage_check(_cfg(enabled=True, warn_below=0.8), ENTRY)[0].state == "ok"
    )
    assert (
        hive_ready._link_coverage_check(_cfg(enabled=True, warn_below=0.81), ENTRY)[0].state
        == "warn"
    )


def test_a_missing_cache_is_never_computed_with_the_refresh_hint(home, monkeypatch) -> None:
    forbidden = Forbidden(monkeypatch)

    (check,) = hive_ready._link_coverage_check(_cfg(enabled=True), ENTRY)

    assert (check.required, check.state) == (False, "off")
    assert "never computed" in check.detail and REFRESH in check.detail
    assert not forbidden.calls


def test_a_corrupt_cache_is_stale_with_the_refresh_hint(home) -> None:
    path = _cache(home)
    path.write_text("{ not json")

    (check,) = hive_ready._link_coverage_check(_cfg(enabled=True), ENTRY)

    assert (check.required, check.state) == (False, "warn")
    assert check.detail.startswith("stale: unusable cache") and REFRESH in check.detail


def test_a_cache_measured_with_other_settings_is_stale_and_shows_no_figures(home) -> None:
    _cache(home, settings=CoverageSettings(code_types=("bug",)))

    (check,) = hive_ready._link_coverage_check(_cfg(enabled=True), ENTRY)

    assert check.state == "warn" and check.detail.startswith("stale:")
    assert "%" not in check.detail and REFRESH in check.detail


# -- bh doctor -------------------------------------------------------------------------------


def test_doctor_rows_carry_the_cached_ratios_per_hive_and_warn_below(home, monkeypatch, capsys):
    _cache(home, beads=(2, 10), commits=(9, 10), docs=(8, 10))
    forbidden = Forbidden(monkeypatch)

    rows = doctor._data_link_coverage(_cfg(enabled=True))
    doctor._render_link_coverage(rows)
    out = capsys.readouterr().out

    (row,) = rows
    assert row["hive"] == "github/octo/widgets" and row["state"] == "warn"
    assert [r["below"] for r in row["ratios"]] == [True, False, False]
    assert "# Link coverage (cached)" in out
    assert "github/octo/widgets beads with commits 20.0% (2/10) !" in out
    assert REFRESH in out and not forbidden.calls


def test_doctor_reports_a_never_computed_hive_and_skips_hq(home, monkeypatch, capsys) -> None:
    cfg = _cfg(enabled=True)
    cfg["managed_repos"].append(
        {"provider": "local", "org": "factory", "repo": "hq", "prefix": "hq", "kind": "hq"}
    )
    forbidden = Forbidden(monkeypatch)

    rows = doctor._data_link_coverage(cfg)
    doctor._render_link_coverage(rows)

    (row,) = rows
    assert row["state"] == "never" and row["ratios"] == [] and row["hint"].endswith("--refresh`")
    assert "never computed" in capsys.readouterr().out and not forbidden.calls
