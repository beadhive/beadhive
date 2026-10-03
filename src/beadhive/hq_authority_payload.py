"""Pure signed authority projection shared by Git and SQL carriers."""

from __future__ import annotations

from dataclasses import asdict


def authority_payload(authority) -> dict:
    """Preserve the legacy shape; add the HQ binding only when it exists."""
    data = asdict(authority)
    if data["beadyard_id"] is None:
        del data["beadyard_id"]
    return data
