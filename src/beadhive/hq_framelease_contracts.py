"""Transport-neutral signed FrameLease payload and validation contract.

Git and SQL carriers both authenticate the same canonical payload.  Keeping its
model here lets carrier code depend on domain data without loading the authority
control-plane composition module.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .host_manifest_contracts import FrameRelease

DOMAIN = "beadhive/frame-heartbeat/v1"


class HeartbeatError(ValueError):
    """Malformed observation or unavailable publication authority."""


class ConformanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    status: Literal["pass", "fail", "not-applicable"]
    evidence: str | None = Field(default=None, max_length=1024)


class HeartbeatConformance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile: str = Field(min_length=1)
    status: Literal["unknown", "conformant", "non-conformant"]
    checks: list[ConformanceCheck]


class HeartbeatLease(BaseModel):
    """Sender projection of the pinned canonical FrameLease spec."""

    model_config = ConfigDict(extra="forbid", strict=True)
    domain: str = DOMAIN
    audience: str = Field(min_length=1)
    frame_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    holderIdentity: str = Field(min_length=1)
    instance_ref: str = Field(min_length=1)
    key_id: str = Field(min_length=1)
    epoch: int = Field(ge=0)
    config_revision: str = Field(min_length=1)
    seq: int = Field(ge=1)
    renewTime: str
    leaseDurationSeconds: int = Field(default=300, ge=1, le=900)
    intervalSeconds: int = Field(default=60, ge=1)
    release: FrameRelease
    toplevel: str | None = None
    image: str | None = None
    generation: int = Field(default=0, ge=0)
    state_seen: Literal[
        "pending", "active", "draining", "drained", "parked", "quarantined", "retired"
    ]
    conformance: HeartbeatConformance
    free_sessions: int = Field(default=0, ge=0)
    report_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bounds(self):
        if self.domain != DOMAIN or self.leaseDurationSeconds < 3 * self.intervalSeconds:
            raise ValueError("wrong heartbeat domain or ttl below three intervals")
        if self.toplevel and self.image:
            raise ValueError("toplevel and image are mutually exclusive")
        if not self.release.id or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.release.digest):
            raise ValueError("release requires an id and sha256 digest")
        if self.image and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image):
            raise ValueError("image requires a sha256 digest")
        if self.toplevel == "":
            raise ValueError("toplevel must be nonempty")
        _ = self.observed_at
        return self

    @property
    def observed_at(self) -> float:
        dt = datetime.fromisoformat(self.renewTime.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("renewTime requires an explicit timezone")
        return dt.timestamp()
