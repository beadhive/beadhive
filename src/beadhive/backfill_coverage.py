"""Root wiring of the link-coverage metrics (``docs/BACKFILL.md`` "Coverage").

The measurement, the inputs hash and the cache file live in :mod:`beadhive_backfill.coverage`;
this module only supplies what is BeadHive-specific: the ``backfill.coverage`` settings, where a
hive's cached state lives, and the real readers behind the three injected reads (the bd store
through a :class:`~beadhive_backfill.StoreReader`, ``git log`` and the doc roots).

It deliberately imports nothing from :mod:`beadhive.backfill_cli`, so any applied pass can call
:func:`refresh_after_apply` (the seam later passes reuse) without an import cycle.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from beadhive_backfill import StoreReader, system_clock
from beadhive_backfill.coverage import (
    CommitRecord,
    CoverageReport,
    CoverageSettings,
    measure,
    write_coverage_cache,
)
from beadlog import BeadEvent
from beadlog_sources.commits import read_commits, require_full_history
from beadlog_sources.docs import discover_docs

from . import registry
from .modules.config.contracts import BackfillCoverageConfig

CACHE_FILE = "coverage.json"
_STATE_DIRS = ("state", "backfill")


def coverage_section(cfg: Mapping[str, Any]) -> Mapping[str, Any]:
    """The raw ``backfill.coverage`` mapping of a loaded config (empty when absent)."""
    section = (cfg.get("backfill") or {}).get("coverage") or {}
    return section if isinstance(section, Mapping) else {}


def coverage_settings(cfg: Mapping[str, Any]) -> CoverageSettings:
    """The measured types, doc roots and example limit (the contract defaults when unset)."""
    model = BackfillCoverageConfig.model_validate(dict(coverage_section(cfg)))
    return CoverageSettings(
        code_types=tuple(model.code_types),
        doc_roots=tuple(model.doc_roots),
        example_limit=model.example_limit,
    )


def refresh_enabled(cfg: Mapping[str, Any]) -> bool:
    """Whether applied passes refresh the cache (``backfill.coverage.enabled``, off by default)."""
    return coverage_section(cfg).get("enabled") is True


def state_path(entry: Mapping[str, Any] | None, cwd: Path, home: Path) -> Path:
    """The hive's cached coverage state under the bh state dir:
    ``<home>/state/backfill/<hive key>/coverage.json``.

    The key is the registered ``<group>/<org>/<repo>`` triplet (``entry``); a directory that is
    no registered hive keys on its resolved path, so two such directories never share a cache."""
    key = registry.hive_key(entry) if entry is not None else f"path/{Path(cwd).resolve()}"
    return Path(home).joinpath(*_STATE_DIRS, registry.sanitize(key), CACHE_FILE)


class HiveReaders:
    """The real :class:`~beadhive_backfill.coverage.CoverageReaders` of one hive.

    ``beads`` is one export through the injected store reader, ``commits`` one ``git log`` (a
    shallow clone is refused: truncated history would understate every commit ratio) and ``docs``
    one directory walk."""

    def __init__(
        self, cwd: Path, *, reader: StoreReader, ref: str, settings: CoverageSettings
    ) -> None:
        self._cwd = Path(cwd)
        self._reader = reader
        self._ref = ref
        self._settings = settings

    def beads(self) -> Iterable[BeadEvent]:
        return self._reader.export()

    def commits(self) -> list[CommitRecord]:
        require_full_history(self._cwd)
        return list(read_commits(self._cwd, self._ref))

    def docs(self) -> list[str]:
        return discover_docs(self._cwd, self._settings.doc_roots)


def hive_readers(
    cwd: Path, *, reader: StoreReader, ref: str, settings: CoverageSettings
) -> HiveReaders:
    """The readers for the hive at ``cwd`` (the seam tests replace)."""
    return HiveReaders(cwd, reader=reader, ref=ref, settings=settings)


def measure_hive(
    cwd: Path,
    *,
    reader: StoreReader,
    ref: str,
    settings: CoverageSettings,
    group: str | None,
) -> CoverageReport:
    """Read the hive once (bd export, git log, doc walk) and measure it, stamped now."""
    readers = hive_readers(cwd, reader=reader, ref=ref, settings=settings)
    return measure(readers, settings, clock=system_clock, group=group)


def refresh_cache(
    cwd: Path,
    *,
    reader: StoreReader,
    ref: str,
    settings: CoverageSettings,
    group: str | None,
    path: Path,
) -> CoverageReport:
    """Measure the hive and rewrite its cache at ``path``; the single refresh path of
    ``coverage --refresh`` and of every applied pass."""
    report = measure_hive(cwd, reader=reader, ref=ref, settings=settings, group=group)
    write_coverage_cache(path, report)
    return report


__all__ = [
    "CACHE_FILE",
    "HiveReaders",
    "coverage_section",
    "coverage_settings",
    "hive_readers",
    "measure_hive",
    "refresh_cache",
    "refresh_enabled",
    "state_path",
]
