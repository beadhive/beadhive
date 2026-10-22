"""Opt-in host-daemon timer that imports each new PyPI release into the catalog (bh-vfrem.4).

``hq.release_catalog.auto_import`` (default ``false``; ADR section 3, "Automation"). With it on,
a release needs no manual HQ step: every ``interval_seconds`` the daemon lists PyPI, and for each
version NEWER than the newest one already in the catalog it runs the same
:func:`beadhive.hq_release_catalog.import_release` the CLI verb runs, with the same verification
and the same INSERT-only credential. It adds no trust: a version whose bundle does not verify is
logged and skipped, never imported.

It deliberately does nothing on an empty catalog (the first entry is a manual, reviewed
``bh hq release-catalog import``), never imports an older version, never imports a pre-release,
and gives a failing version up after :data:`MAX_ATTEMPTS` ticks until the daemon restarts.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from .hq_release_catalog import (
    BundleVerifier,
    CatalogError,
    HttpSource,
    ReleaseCatalogStore,
    ReleaseSource,
    import_release,
)

__all__ = ["MAX_ATTEMPTS", "AutoImportRefused", "AutoImporter", "build_loop", "pypi_versions"]

_log = logging.getLogger(__name__)
MAX_ATTEMPTS = 24


class AutoImportRefused(ValueError):
    """Auto-import is enabled but cannot run; the daemon refuses to start."""


def pypi_versions(client: Any, url: str = "https://pypi.org/pypi/beadhive/json") -> list[str]:
    """Every non-yanked release version PyPI lists (``client`` is an ``httpx.Client``)."""
    response = client.get(url)
    response.raise_for_status()
    releases = response.json().get("releases", {})
    return [
        version
        for version, files in releases.items()
        if files and not all(item.get("yanked") for item in files)
    ]


@dataclass
class AutoImporter:
    store: ReleaseCatalogStore
    source: ReleaseSource
    verifier: BundleVerifier
    listing: Callable[[], list[str]]
    interval: float
    trusted_root: Path | None = None
    attempts: dict[str, int] = field(default_factory=dict)
    imported: list[str] = field(default_factory=list)
    ticks: int = 0
    errors: int = 0
    _closed: bool = False

    def pending(self) -> list[str]:
        """Versions newer than the newest catalogued one, oldest first."""
        known = {row.version for row in self.store.rows()}
        if not known:
            return []
        newest = max(Version(v) for v in known)
        found = []
        for text in self.listing():
            try:
                version = Version(text)
            except InvalidVersion:
                continue
            if (
                version > newest
                and not version.is_prerelease
                and not version.is_devrelease
                and str(version) == text
                and self.attempts.get(text, 0) < MAX_ATTEMPTS
            ):
                found.append(version)
        return [str(v) for v in sorted(found)]

    def tick(self) -> list[str]:
        try:
            todo = self.pending()
        except Exception:  # noqa: BLE001 - one failed tick never stops the loop
            self.errors += 1
            _log.exception("release catalog auto-import listing failed")
            return []
        self.ticks += 1
        done = []
        for version in todo:
            self.attempts[version] = self.attempts.get(version, 0) + 1
            try:
                result = import_release(
                    version,
                    source=self.source,
                    verifier=self.verifier,
                    store=self.store,
                    trusted_root=self.trusted_root,
                )
            except (CatalogError, OSError, ValueError) as exc:
                self.errors += 1
                _log.warning("release catalog auto-import of %s refused: %s", version, exc)
                break  # never import a later version past a refused earlier one
            done.append(version)
            self.imported.append(version)
            _log.warning("release catalog auto-import %s: %s", version, result["result"])
        return done

    async def run(self) -> None:
        while not self._closed:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(self.interval)

    def component(self) -> Any:
        from .kernel.lifecycle import HostDaemonLifespanComponent, HostDaemonShutdownPhase

        @asynccontextmanager
        async def lifespan(_app: Any):
            task = asyncio.create_task(self.run(), name="release-catalog-auto-import")
            try:
                yield
            finally:
                self._closed = True
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        return HostDaemonLifespanComponent(
            name="release-catalog-auto-import",
            lifespan=lifespan,
            shutdown_phase=HostDaemonShutdownPhase.CANCEL_PROCESSES,
        )


def build_loop(
    cfg: Mapping | None,
    *,
    hq_mode: str,
    store: ReleaseCatalogStore | None = None,
    source: ReleaseSource | None = None,
    verifier: BundleVerifier | None = None,
    listing: Callable[[], list[str]] | None = None,
) -> AutoImporter | None:
    """The loop for the ``hq.release_catalog`` section, or ``None`` unless ``auto_import`` is on.

    Refuses (:class:`AutoImportRefused`) git HQ, a missing operator settings file or catalog
    credential, and a missing ``beadhive[release-verify]`` extra, so the daemon never runs
    without the loop it was configured to run. The keyword seams are for tests."""
    section = ((cfg or {}).get("hq") or {}).get("release_catalog") or {}
    if not section.get("auto_import"):
        return None
    if hq_mode != "dolt-server":
        raise AutoImportRefused(
            "hq.release_catalog.auto_import needs dolt-server HQ (the catalog is a SQL table)"
        )
    if store is None:
        from . import hq_operator_settings

        path = section.get("operator_settings") or os.environ.get(hq_operator_settings.ENV)
        if not path:
            raise AutoImportRefused(
                "hq.release_catalog.auto_import needs hq.release_catalog.operator_settings "
                f"(or {hq_operator_settings.ENV}) naming the catalog writer's settings file"
            )
        try:
            store = hq_operator_settings.release_catalog_store(path)
        except ValueError as exc:
            raise AutoImportRefused(f"release catalog settings refused: {exc}") from None
    if verifier is None:
        from .hq_release_catalog_sigstore import SigstoreBundleVerifier

        try:
            verifier = SigstoreBundleVerifier()
        except CatalogError as exc:
            raise AutoImportRefused(str(exc)) from None
    if source is None or listing is None:
        import httpx

        client = httpx.Client(timeout=30)
        source = source or HttpSource(client)
        listing = listing or (lambda: pypi_versions(client))
    root = section.get("trusted_root")
    return AutoImporter(
        store,
        source,
        verifier,
        listing,
        float(section.get("interval_seconds", 3600.0)),
        Path(root) if root else None,
    )
