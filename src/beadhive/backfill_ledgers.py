"""LedgerDirectory adapters: the workspace's bd routes and the HQ hive registry.

Root-side adapters for the neutral ``beadlog_sources.ledger`` port (backfill-history-pipeline
ADR, phase B2). A ledger is a named bd store; its id set is one bulk ``bd export`` read through
:func:`beadhive.bd.export_jsonl` (the read backfill already uses; never a per-bead loop).

Ledger names come from registration only (bh-ear6v): the registry's hive prefix, or the name an
explicit route is registered under. A token whose prefix merely looks like a registered
ledger's, or names an unregistered ledger, is not in any registered id set and stays
unresolved. Each id set is read at most once per run (:class:`CachedLedgerDirectory`).

An offline or unreachable ledger is a **warning, not an error**: its id set is empty (its
tokens stay unresolved), the failure is kept as a :class:`LedgerWarning` on
:attr:`WarningLedgerDirectory.warnings`, and it is logged at WARNING. The caller surfaces it.
A failed read is also cached, so an unreachable ledger is attempted once per run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beadlog_sources import CachedLedgerDirectory, LedgerDirectory
from beadlog_sources.bd import parse_export

from . import bd, registry

logger = logging.getLogger(__name__)

IdReader = Callable[[Path], Iterable[str]]


@dataclass(frozen=True)
class LedgerWarning:
    """One ledger whose id set could not be read; its tokens are unresolved."""

    ledger: str
    reason: str

    def __str__(self) -> str:
        return f"ledger {self.ledger!r} unavailable: {self.reason}"


def bd_export_ids(cwd: Path) -> list[str]:
    """The ids of the bd store at ``cwd``: one ``bd export`` read. Raises ``OSError`` on failure."""
    if not Path(cwd).is_dir():
        raise OSError(f"no checkout at {cwd}")
    text = bd.export_jsonl(cwd, all_=True)
    if text is None:
        raise OSError(f"bd export failed in {cwd}")
    return [str(row["id"]) for row in parse_export(text)]


class WarningLedgerDirectory(CachedLedgerDirectory):
    """A cached directory whose unreadable ledgers degrade to empty with a recorded warning."""

    def __init__(self, sources: Mapping[str, Path], read_ids: IdReader = bd_export_ids) -> None:
        self._warnings: list[LedgerWarning] = []
        loaders = {name: self._guarded(name, path, read_ids) for name, path in sources.items()}
        super().__init__(loaders)
        self.sources: dict[str, Path] = dict(sources)

    @property
    def warnings(self) -> tuple[LedgerWarning, ...]:
        """Warnings for the ledgers read so far (a ledger is read on first query)."""
        return tuple(self._warnings)

    def load_all(self) -> tuple[LedgerWarning, ...]:
        """Read every registered ledger now, so :attr:`warnings` is complete."""
        for name in self.ledgers():
            self.ids(name)
        return self.warnings

    def _guarded(self, name: str, path: Path, read_ids: IdReader) -> Callable[[], Iterable[str]]:
        def load() -> Iterable[str]:
            try:
                return list(read_ids(path))
            except Exception as exc:  # any unreadable ledger degrades, never aborts the run
                warning = LedgerWarning(name, str(exc) or type(exc).__name__)
                self._warnings.append(warning)
                logger.warning("%s", warning)
                return ()

        return load


def bd_routes_directory(
    routes: Mapping[str, Path | str], read_ids: IdReader = bd_export_ids
) -> WarningLedgerDirectory:
    """Adapter (a): explicitly registered bd routes, ``ledger name -> bd store directory``."""
    return WarningLedgerDirectory({name: Path(path) for name, path in routes.items()}, read_ids)


def hive_registry_directory(
    cfg: Mapping[str, Any], read_ids: IdReader = bd_export_ids
) -> WarningLedgerDirectory:
    """Adapter (b): the HQ hive registry, ``prefix -> hive`` for every registered hive.

    The Factory HQ singleton is excluded (:func:`beadhive.registry.hives`). The ledger name is
    the registered prefix, never derived from a token.
    """
    sources = {
        str(e["prefix"]): registry.hive_dir(e) for e in registry.hives(cfg) if e.get("prefix")
    }
    return WarningLedgerDirectory(sources, read_ids)


def ledger_directory_for(
    hive: str,
    cfg: Mapping[str, Any],
    *,
    routes: Mapping[str, Path | str] | None = None,
    read_ids: IdReader = bd_export_ids,
) -> WarningLedgerDirectory:
    """The directory for backfilling ``hive`` (a registered prefix or hive id).

    Registry hives plus any explicit ``routes`` (a route overrides a registry hive of the same
    name). ``hive`` must be registered: it is the local ledger, so an unregistered hive raises
    rather than silently resolving nothing. Returns a :class:`LedgerDirectory`; check
    ``.warnings`` (after :meth:`~WarningLedgerDirectory.load_all` or a run) for degraded ledgers.
    """
    entry = registry.resolve_hive(cfg, hive)
    sources = dict(hive_registry_directory(cfg, read_ids).sources)
    sources.setdefault(str(entry["prefix"]), registry.hive_dir(entry))
    for name, path in (routes or {}).items():
        sources[name] = Path(path)
    return WarningLedgerDirectory(sources, read_ids)


__all__ = [
    "LedgerDirectory",
    "LedgerWarning",
    "WarningLedgerDirectory",
    "bd_export_ids",
    "bd_routes_directory",
    "hive_registry_directory",
    "ledger_directory_for",
]
