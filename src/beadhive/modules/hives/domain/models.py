"""Typed, transport-neutral contracts for the hive capability."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePath
from types import MappingProxyType
from typing import Any

_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")


def _text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


@dataclass(frozen=True, slots=True)
class HiveIdentity:
    provider: str
    organization: str
    repository: str

    def __post_init__(self) -> None:
        for name in ("provider", "organization", "repository"):
            value = _text(getattr(self, name), name)
            if value in {".", ".."} or _SEGMENT.fullmatch(value) is None:
                raise ValueError(f"{name} must be a canonical hive identity segment")
            object.__setattr__(self, name, value)

    @classmethod
    def parse(cls, value: str) -> HiveIdentity:
        parts = value.split("/")
        if len(parts) != 3:
            raise ValueError("hive identity must be provider/organization/repository")
        return cls(*parts)

    @property
    def canonical_id(self) -> str:
        return f"{self.provider}/{self.organization}/{self.repository}"

    def target_under(self, root: str) -> str:
        base = _text(root, "workspace root")
        return str(PurePath(base) / self.provider / self.organization / self.repository)


@dataclass(frozen=True, slots=True)
class RegisterHiveRequest:
    identity: HiveIdentity
    prefix: str = ""
    kind: str = ""
    upstream: str = ""


@dataclass(frozen=True, slots=True)
class RegisterHiveResult:
    identity: HiveIdentity
    prefix: str
    kind: str
    registered: bool = True

    def __post_init__(self) -> None:
        if self.registered:
            _text(self.prefix, "prefix")


@dataclass(frozen=True, slots=True)
class DiscoverHivesResult:
    candidates: tuple[str, ...]
    registered: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HiveListRequest:
    available: bool = False
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class HiveDiagnostic:
    code: str
    detail: str
    error: bool = False

    def __post_init__(self) -> None:
        _text(self.code, "diagnostic code")
        _text(self.detail, "diagnostic detail")


@dataclass(frozen=True, slots=True)
class HiveIdentityPage:
    source_revision: str | None
    generated_at: int
    freshness_state: str
    freshness_as_of: int | None
    coverage_state: str
    coverage_reason: str | None
    hives: tuple[Mapping[str, Any], ...]
    total: int | None
    limit: int
    truncated: bool
    next_cursor: str | None
    diagnostics: tuple[HiveDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class HiveListResult:
    discovery: DiscoverHivesResult
    page: HiveIdentityPage | None = None
    diagnostics: tuple[HiveDiagnostic, ...] = ()

    @property
    def rows(self) -> tuple[str, ...]:
        return self.discovery.candidates


@dataclass(frozen=True, slots=True)
class HiveStatusRequest:
    hive_id: str = ""


@dataclass(frozen=True, slots=True)
class HiveStatusResult:
    candidates: tuple[str, ...]
    collisions: tuple[Mapping[str, Any], ...]
    violations: tuple[str, ...]
    hives: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class OnboardHiveRequest:
    identity: HiveIdentity
    clone_url: str = ""
    furnish: bool | None = None
    claude: bool = False
    skills: bool = False
    observaloop: bool = False
    agents: bool = False
    opencode: bool = False
    codex: bool = False
    global_grant: bool = False
    plugins: tuple[str, ...] = ()
    force: bool = False
    kind: str = ""
    prefix: str = ""
    yes: bool = False
    dry_run: bool = False
    skip_check: str = ""
    hub_sync: bool | None = None


@dataclass(frozen=True, slots=True)
class OnboardCheck:
    id: str
    label: str
    ok: bool
    detail: str
    overridable: bool
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class OnboardHiveResult:
    identity: HiveIdentity
    target: str
    cloned: bool
    registered: bool
    prefix: str
    synced: bool
    kind: str = ""
    successful: bool = True
    dry_run: bool = False
    checks: tuple[OnboardCheck, ...] = ()
    steps: tuple[str, ...] = ()
    installers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadinessRequest:
    verbose: bool = False
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    label: str
    required: bool
    state: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    hive: str | None
    checks: tuple[ReadinessCheck, ...] = ()
    diagnostics: tuple[HiveDiagnostic, ...] = ()


class RetireScope(StrEnum):
    FLEET = "fleet"
    HOST = "host"


@dataclass(frozen=True, slots=True)
class RetireHiveRequest:
    hive_id: str
    scope: RetireScope = RetireScope.FLEET
    dry_run: bool = False
    backup: bool = False
    confirm: bool = False
    purge: bool = False

    def __post_init__(self) -> None:
        _text(self.hive_id, "hive_id")


@dataclass(frozen=True, slots=True)
class RetireEvent:
    code: str
    facts: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    error: bool = False

    def __post_init__(self) -> None:
        _text(self.code, "retirement event code")
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))


@dataclass(frozen=True, slots=True)
class RetireHiveResult:
    hive_id: str
    scope: RetireScope
    clone_path: str
    dry_run: bool
    unregistered: bool
    archived_to: str | None = None
    purged: bool = False
    plugins_notified: tuple[str, ...] = ()
    successful: bool = True
    events: tuple[RetireEvent, ...] = ()
    details: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
