"""Archive manifest and capabilities (format ADR sections 1, 7 and 11)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ._json import freeze, thaw

FORMAT_VERSION = "0.1.0"
MEDIA_TYPE = "application/vnd.beadhive.beadlog+zip"
FILE_EXTENSION = ".bdlg"
_FORMAT_VERSION = re.compile(r"^0\.1\.(0|[1-9][0-9]*)$")

# Constant for v0.1, written into every manifest so an archive is self-describing.
TIME_SEMANTICS: Mapping[str, Any] = freeze(
    {
        "occurred_at": "valid-time",
        "recorded_at": "source-transaction-time",
        "extracted_at": "extraction-time",
        "timezone": "UTC",
        "order": ["occurred_at", "recorded_at", "id"],
    }
)


class Capability(StrEnum):
    """The closed v0.1 archive capability vocabulary (format ADR section 7)."""

    TOMBSTONES = "tombstones"
    CHECKPOINT_SEEDED = "checkpoint-seeded"
    REDACTION = "redaction"
    CHECKPOINTS = "checkpoints"
    RECORDED_TIME_MONOTONE_LOAD = "recorded-time-monotone-load"
    LAMPORT = "lamport"
    PLAN = "plan"


KNOWN_CAPABILITIES: frozenset[str] = frozenset(str(item) for item in Capability)


class LoaderCapability(StrEnum):
    """What a target store can keep; reported in a loss report (format ADR section 7)."""

    VALID_TIME = "valid-time"
    PER_ELEMENT_VALID_TIME = "per-element-valid-time"
    RECORDED_TIME = "recorded-time"
    RECORDED_TIME_MONOTONE_LOAD = "recorded-time-monotone-load"
    ERASE = "erase"
    METADATA_FALLBACK = "metadata-fallback"


class ManifestError(ValueError):
    """A manifest object this reader cannot interpret (wrong version or malformed)."""


class UnsupportedCapabilityError(ManifestError):
    """The archive requires capabilities this reader does not implement; it must refuse."""

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = tuple(sorted(missing))
        super().__init__("archive requires unsupported capabilities: " + ", ".join(self.missing))


@dataclass(frozen=True)
class Capabilities:
    """``required``: a reader must implement each or refuse; ``optional``: may be ignored."""

    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    def unsupported(self, implemented: Iterable[str] = KNOWN_CAPABILITIES) -> tuple[str, ...]:
        """Required capabilities not in ``implemented``, sorted."""
        have = set(implemented)
        return tuple(sorted(name for name in self.required if name not in have))

    def require(self, implemented: Iterable[str] = KNOWN_CAPABILITIES) -> None:
        """Raise :class:`UnsupportedCapabilityError` unless every required one is implemented."""
        missing = self.unsupported(implemented)
        if missing:
            raise UnsupportedCapabilityError(missing)

    def to_json(self) -> dict[str, list[str]]:
        return {"required": list(self.required), "optional": list(self.optional)}

    @classmethod
    def from_json(cls, value: Any) -> Capabilities:
        if not isinstance(value, Mapping):
            raise ManifestError(f"capabilities must be an object, got {value!r}")
        return cls(tuple(value.get("required") or ()), tuple(value.get("optional") or ()))


@dataclass(frozen=True)
class Generator:
    """The tool that wrote an archive."""

    name: str
    version: str
    uri: str | None = None

    def to_json(self) -> dict[str, str]:
        out = {"name": self.name, "version": self.version}
        if self.uri is not None:
            out["uri"] = self.uri
        return out

    @classmethod
    def from_json(cls, value: Any) -> Generator:
        if not isinstance(value, Mapping):
            raise ManifestError(f"generator must be an object, got {value!r}")
        return cls(value["name"], value["version"], value.get("uri"))


@dataclass(frozen=True)
class Group:
    """A bead group: ``id`` is the durable identity in every ``bead.group``; ``dir`` names
    its part directories."""

    id: str
    dir: str
    aliases: tuple[str, ...] = ()
    description: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "dir": self.dir}
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.description is not None:
            out["description"] = self.description
        return out

    @classmethod
    def from_json(cls, value: Any) -> Group:
        if not isinstance(value, Mapping):
            raise ManifestError(f"a group must be an object, got {value!r}")
        return cls(
            value["id"], value["dir"], tuple(value.get("aliases") or ()), value.get("description")
        )


def check_format_version(version: object) -> str:
    """Return ``version`` if this reader knows it (any ``0.1.PATCH``); raise otherwise."""
    if not isinstance(version, str) or _FORMAT_VERSION.fullmatch(version) is None:
        raise ManifestError(f"unsupported BeadLog format_version {version!r} (reader knows 0.1.x)")
    return version


_NO_REDACTION: Mapping[str, Any] = freeze({"applied": False})
_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Manifest:
    """``MANIFEST.json`` of an archive. Member inventory, counts, redaction and plan details are
    kept as frozen JSON; archive I/O fills and verifies them."""

    format_version: str
    generator: Generator
    groups: tuple[Group, ...]
    capabilities: Capabilities
    redaction: Mapping[str, Any] = field(default=_NO_REDACTION, hash=False)
    files: tuple[Mapping[str, Any], ...] = field(default=(), hash=False)
    counts: Mapping[str, Any] = field(default=_EMPTY, hash=False)
    created_at: str | None = None
    description: str | None = None
    plan: Mapping[str, Any] | None = field(default=None, hash=False)
    annotations: Mapping[str, Any] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        check_format_version(self.format_version)
        set_(self, "groups", tuple(self.groups))
        set_(self, "redaction", freeze(self.redaction))
        set_(self, "files", freeze(tuple(self.files)))
        set_(self, "counts", freeze(self.counts))
        if self.plan is not None:
            set_(self, "plan", freeze(self.plan))
        if self.annotations is not None:
            set_(self, "annotations", freeze(self.annotations))

    def group(self, group_id: str) -> Group | None:
        """The declared group with durable id ``group_id``, if any."""
        return next((group for group in self.groups if group.id == group_id), None)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "format_version": self.format_version,
            "generator": self.generator.to_json(),
        }
        if self.created_at is not None:
            out["created_at"] = self.created_at
        if self.description is not None:
            out["description"] = self.description
        out.update(
            {
                "groups": [group.to_json() for group in self.groups],
                "time_semantics": thaw(TIME_SEMANTICS),
                "capabilities": self.capabilities.to_json(),
                "redaction": thaw(self.redaction),
                "files": thaw(self.files),
                "counts": thaw(self.counts),
            }
        )
        if self.plan is not None:
            out["plan"] = thaw(self.plan)
        if self.annotations is not None:
            out["annotations"] = thaw(self.annotations)
        return out

    @classmethod
    def from_json(cls, value: Any) -> Manifest:
        """Parse a manifest object. Checks the format version and the constant time semantics;
        call ``manifest.capabilities.require()`` before interpreting the archive."""
        if not isinstance(value, Mapping):
            raise ManifestError(f"a manifest must be a JSON object, got {type(value).__name__}")
        check_format_version(value.get("format_version"))
        if thaw(value.get("time_semantics")) != thaw(TIME_SEMANTICS):
            raise ManifestError("time_semantics differ from the v0.1 constant")
        try:
            return cls(
                format_version=value["format_version"],
                generator=Generator.from_json(value["generator"]),
                groups=tuple(Group.from_json(group) for group in value["groups"]),
                capabilities=Capabilities.from_json(value["capabilities"]),
                redaction=value["redaction"],
                files=tuple(value["files"]),
                counts=value["counts"],
                created_at=value.get("created_at"),
                description=value.get("description"),
                plan=value.get("plan"),
                annotations=value.get("annotations"),
            )
        except KeyError as exc:
            raise ManifestError(f"manifest is missing {exc.args[0]!r}") from exc
