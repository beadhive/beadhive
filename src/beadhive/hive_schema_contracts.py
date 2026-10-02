"""Pure contract for a hive's observed Beads schema record.

The ``schema_version`` field is an observed Beads migration count. It is not the
Beadhive settings document format version.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class HiveSchemaRecord(BaseModel):
    """One hive's last-observed bd schema version in Factory HQ."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., description="Repo-group path segment (registry.py's `provider`).")
    org: str = Field(..., description="Org segment of the hive's identity triplet.")
    repo: str = Field(..., description="Repo segment of the hive's identity triplet.")
    schema_version: int = Field(
        ..., description="The real bd/Dolt migration-count integer (e.g. 59), not a decoy field."
    )
    dolt_mode: str = Field(
        "", description="bd's reported engine mode at observation time (embedded/server/...)."
    )
    observed_at: str = Field(
        ..., description="UTC timestamp (see _TIMESTAMP_FMT) the probe actually ran."
    )
    observed_by_host: str = Field(
        "", description="host_id (beadhive.host.host_id()) of the host that ran the probe."
    )
    observed_by_bd_version: str = Field(
        "", description="`bd --version` output of the bd binary that produced this observation."
    )


__all__ = ("HiveSchemaRecord",)
