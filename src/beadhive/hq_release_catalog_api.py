"""The signed release catalog on a ``route: api`` host (bh-lejfy, design decision D3).

The same store surface as :class:`beadhive.hq_release_catalog.ReleaseCatalogStore` (``rows``,
``add``, ``revoke``), over the HQ port instead of the ``hq.sql.release_catalog`` binding, so the
import and verify verbs run unchanged: ``rows`` is ``GET /release-catalog/{catalog}`` and ``add``
is ``POST /release-catalog/{catalog}/import/{plan,apply}``, signed client-side with the operator
key under the catalog domain. Nothing here opens SQL. Revoke has an hq-service route
(``.../revoke/{plan,apply}``) that is not routed until 0.41.0.
"""

from __future__ import annotations

import time

from .hq_api_operator import signer
from .hq_port import HqError
from .hq_release_catalog import (
    CATALOG,
    CatalogEntry,
    CatalogError,
    CatalogRow,
    plan_append,
)

DEADLINE_S = 15.0


def _row(item: dict) -> CatalogRow:
    try:
        return CatalogRow(
            str(item["catalog"]),
            str(item["version"]),
            str(item["measured_digest"]),
            str(item["status"]),
            str(item["algorithm"]),
            str(item["wheel_sha256"]),
            str(item["commit"]),
            str(item.get("bundle") or "").encode(),
            str(item.get("reason") or ""),
            float(item["recorded_at"]),
        )
    except (KeyError, TypeError, ValueError):
        raise CatalogError("hq-service catalog row is malformed") from None


class ApiReleaseCatalogStore:
    def __init__(self, port, *, operator_key=None, deadline_s: float = DEADLINE_S):
        self._port, self._key, self._deadline_s = port, operator_key, deadline_s

    def _deadline(self) -> float:
        return time.monotonic() + self._deadline_s

    def rows(self, version: str | None = None) -> list[CatalogRow]:
        try:
            items = self._port.read_release_catalog(CATALOG, deadline=self._deadline())
        except HqError as exc:
            raise CatalogError(f"release catalog read unavailable: {exc}") from None
        rows = [_row(item) for item in items]
        return [r for r in rows if version is None or r.version == version]

    def add(self, entry: CatalogEntry) -> str:
        """Append ``entry`` unless it is already active: ``"inserted"`` or ``"unchanged"``."""
        if plan_append(self.rows(entry.version), entry) == "noop":
            return "unchanged"
        try:
            bundle = entry.bundle.decode()
        except UnicodeDecodeError:
            raise CatalogError(
                "the release bundle is not text; hq-service stores it as text"
            ) from None
        request = {
            "version": entry.version,
            "measured_digest": entry.measured_digest,
            "wheel_sha256": entry.wheel_sha256,
            "commit": entry.commit,
            "bundle": bundle,
        }
        try:
            self._port.import_release_catalog(
                entry.catalog,
                request,
                signer=signer(self._key, catalog=True),
                deadline=self._deadline(),
            )
        except HqError as exc:
            raise CatalogError(f"release catalog import refused: {exc}") from None
        return "inserted"

    def revoke(self, version: str, reason: str) -> list[str]:
        raise CatalogError(
            "revoke has no hq-service route on an hq.route api host until 0.41.0; "
            "run it on the SQL host"
        )
