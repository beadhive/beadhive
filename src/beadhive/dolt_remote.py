"""Scheme-neutral Dolt remote targets: URL family, capabilities, and redaction (bh-b1scz.3).

The HQ descriptor stores a URL, never a provider enum
(docs/design/hq-dolt-remote-descriptor-adr.md). This module derives the *family* and what bh may
do with it from that URL at runtime, so a new provider is one row in ``_FAMILIES`` rather than a
schema change. Pure: no process, network, or configuration access.

An unknown scheme is a typed ``unsupported`` target. It is never treated as Git — the old
"every remote is a Git repository carrying ``refs/dolt/data``" assumption is exactly the
misclassification this module removes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic_core import PydanticCustomError

from .modules.config.contracts import (
    DOLT_REMOTE_S3_QUERY_KEYS,
    DoltRemoteUrlParts,
    dolt_remote_location,
    split_dolt_remote_url,
    validate_dolt_remote_url,
)


class RemoteFamily(StrEnum):
    GIT = "git"
    FILE = "file"
    S3 = "s3"
    AWS = "aws"
    GS = "gs"
    HOSTED = "hosted"
    UNKNOWN = "unknown"


class SupportLevel(StrEnum):
    IMPLEMENTED = "implemented"
    EXTENSION_POINT = "extension-point"
    UNSUPPORTED = "unsupported"


class RemoteOp(StrEnum):
    INSPECT = "inspect"
    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"
    CLONE = "clone"
    PUSH = "push"
    PULL = "pull"


#: Operations that only read or edit the local registration. They never touch the remote, so they
#: are allowed for every family bd itself accepts (removing a stale unknown remote must work).
_LOCAL_OPS = frozenset({RemoteOp.INSPECT, RemoteOp.REMOVE})
_TRANSFER_OPS = frozenset(
    {RemoteOp.ADD, RemoteOp.REPLACE, RemoteOp.CLONE, RemoteOp.PUSH, RemoteOp.PULL}
)


@dataclass(frozen=True)
class RemoteCapabilities:
    """What bh may do with one family. ``read_only_diff`` False means ahead/behind is unknowable
    without a transfer, so health must say ``unknown`` rather than in-sync."""

    family: RemoteFamily
    support: SupportLevel
    object_store: bool = False
    git_ref_fence: bool = False
    read_only_diff: bool = False
    credentials: str = "none"  # none | git | aws | gcp | dolt-creds
    note: str = ""

    def allows(self, op: RemoteOp) -> bool:
        if op in _LOCAL_OPS:
            return True
        return self.support == SupportLevel.IMPLEMENTED and op in _TRANSFER_OPS


_FAMILIES: dict[RemoteFamily, RemoteCapabilities] = {
    RemoteFamily.GIT: RemoteCapabilities(
        RemoteFamily.GIT,
        SupportLevel.IMPLEMENTED,
        git_ref_fence=True,
        read_only_diff=True,
        credentials="git",
        note="Git transport; data rides refs/dolt/data beside the code",
    ),
    RemoteFamily.FILE: RemoteCapabilities(
        RemoteFamily.FILE,
        SupportLevel.IMPLEMENTED,
        object_store=True,
        note="local filesystem chunk store",
    ),
    RemoteFamily.S3: RemoteCapabilities(
        RemoteFamily.S3,
        SupportLevel.IMPLEMENTED,
        object_store=True,
        credentials="aws",
        note="AWS S3 via the standard endpoint (Dolt s3://, no DynamoDB)",
    ),
    RemoteFamily.AWS: RemoteCapabilities(
        RemoteFamily.AWS,
        SupportLevel.IMPLEMENTED,
        object_store=True,
        credentials="aws",
        note="AWS S3 + DynamoDB (Dolt aws://[table:bucket]/db)",
    ),
    RemoteFamily.GS: RemoteCapabilities(
        RemoteFamily.GS,
        SupportLevel.EXTENSION_POINT,
        object_store=True,
        credentials="gcp",
        note="Google Cloud Storage: accepted by Dolt, not yet driven or proven by bh",
    ),
    RemoteFamily.HOSTED: RemoteCapabilities(
        RemoteFamily.HOSTED,
        SupportLevel.EXTENSION_POINT,
        credentials="dolt-creds",
        note="hosted Dolt remotesapi (DoltHub, DoltLab): not yet driven or proven by bh",
    ),
    RemoteFamily.UNKNOWN: RemoteCapabilities(
        RemoteFamily.UNKNOWN,
        SupportLevel.UNSUPPORTED,
        note="unrecognised scheme",
    ),
}

#: An ``s3://`` URL with a custom endpoint addresses an S3-COMPATIBLE service. Dolt accepts it
#: (dolthub/dolt#11571, released in 2.3.2), but a binary accepting a URL is not proof that a
#: provider works (bh-hx39m) — so it stays an extension point until an end-to-end proof lands.
S3_COMPATIBLE = RemoteCapabilities(
    RemoteFamily.S3,
    SupportLevel.EXTENSION_POINT,
    object_store=True,
    credentials="aws",
    note="S3-compatible endpoint: accepted by Dolt, not claimed without a compatibility proof",
)

_SCHEMES: dict[str, RemoteFamily] = {
    "git+ssh": RemoteFamily.GIT,
    "git+https": RemoteFamily.GIT,
    "git+http": RemoteFamily.GIT,
    "git+file": RemoteFamily.GIT,
    "file": RemoteFamily.FILE,
    "s3": RemoteFamily.S3,
    "aws": RemoteFamily.AWS,
    "gs": RemoteFamily.GS,
    "https": RemoteFamily.HOSTED,
    "http": RemoteFamily.HOSTED,
}


@dataclass(frozen=True)
class RemoteTarget:
    """One normalised remote URL and the capabilities its family carries."""

    url: str
    family: RemoteFamily
    capabilities: RemoteCapabilities
    location: str | None = None
    error: str = ""  # a value-free validation code when the URL itself is unusable

    @property
    def redacted(self) -> str:
        return redact_url(self.url)

    @property
    def supported(self) -> bool:
        return not self.error and self.capabilities.support == SupportLevel.IMPLEMENTED

    def allows(self, op: RemoteOp) -> bool:
        if self.error:
            return op in _LOCAL_OPS
        return self.capabilities.allows(op)

    def describe(self) -> str:
        """Operator wording that never calls a non-Git store ``refs/dolt/data``."""
        if self.family == RemoteFamily.GIT:
            return f"git remote {self.redacted} (refs/dolt/data)"
        return f"{self.family.value} remote {self.redacted}"


class UnsupportedRemoteOperation(RuntimeError):
    """A typed refusal: ``op`` is not available for ``target``'s family or support level."""

    def __init__(self, target: RemoteTarget, op: RemoteOp):
        self.target, self.op = target, op
        caps = target.capabilities
        why = target.error or f"{caps.support.value}: {caps.note}"
        super().__init__(
            f"unsupported remote operation: {op.value} for {target.family.value} remote "
            f"{target.redacted} ({why})"
        )


def classify(url: str) -> RemoteTarget:
    """Classify *url* into a family with capabilities. Never raises for a bad URL: an unusable
    URL is an ``unknown``/``unsupported`` target carrying the validation code."""

    try:
        parts = validate_dolt_remote_url(url)
    except PydanticCustomError as exc:
        return RemoteTarget(
            url=str(url),
            family=RemoteFamily.UNKNOWN,
            capabilities=_FAMILIES[RemoteFamily.UNKNOWN],
            error=exc.type,
        )
    family = _SCHEMES.get(parts.scheme, RemoteFamily.UNKNOWN)
    caps = _FAMILIES[family]
    if family == RemoteFamily.S3 and any(k.lower() == "endpoint" for k, _ in parts.query):
        caps = S3_COMPATIBLE
    return RemoteTarget(url=url, family=family, capabilities=caps, location=_location(url))


def _location(url: str) -> str | None:
    return dolt_remote_location(url)


def same_remote(left: str, right: str) -> bool:
    """True when two URLs address the same remote (normalised storage location, else exact
    after scheme/host case folding and a trailing slash)."""

    a, b = _location(left), _location(right)
    if a is not None or b is not None:
        return a == b
    return _fold(left) == _fold(right)


def _fold(url: str) -> str:
    try:
        parts = split_dolt_remote_url(url)
    except PydanticCustomError:
        return url
    return f"{parts.scheme}://{_authority(parts).lower()}{parts.path.rstrip('/')}"


def _authority(parts: DoltRemoteUrlParts) -> str:
    return f"{parts.userinfo}@{parts.host}" if parts.userinfo else parts.host


_MASK = "***"


def redact_url(url: str) -> str:
    """The one rendering of a remote URL for output, errors and logs.

    User info is masked (a Git SSH login name included — it is harmless, but one rule is easier
    to audit than two), every query value outside the S3 addressing allowlist is masked, and a
    fragment is dropped. A string that does not even split is masked whole.
    """

    try:
        parts = split_dolt_remote_url(str(url))
    except PydanticCustomError:
        return _MASK
    host = f"{_MASK}@{parts.host}" if parts.userinfo else parts.host
    rendered = f"{parts.scheme}://{host}{parts.path}"
    if parts.query:
        items = [
            f"{key}={value}" if key.lower() in DOLT_REMOTE_S3_QUERY_KEYS else f"{key}={_MASK}"
            for key, value in parts.query
        ]
        rendered += "?" + "&".join(items)
    return rendered


def capabilities_for(family: RemoteFamily) -> RemoteCapabilities:
    return _FAMILIES[family]


#: Measured on the pinned bd 1.3.0 (ADR evidence item 10): its EMBEDDED Dolt engine rejects
#: ``s3://`` at push time ("unknown url scheme: 's3'") although it registers the URL, while the
#: pinned Dolt 2.3.5 CLI and server speak it. ``aws://`` works in both modes.
S3_EMBEDDED = RemoteCapabilities(
    RemoteFamily.S3,
    SupportLevel.UNSUPPORTED,
    object_store=True,
    credentials="aws",
    note="bd 1.3.0's embedded engine does not speak s3://; use server mode or aws://",
)


def for_store_mode(target: RemoteTarget, *, embedded: bool) -> RemoteTarget:
    """``target`` as the hive's storage mode can actually drive it."""
    if embedded and target.family == RemoteFamily.S3 and not target.error:
        return RemoteTarget(
            url=target.url,
            family=target.family,
            capabilities=S3_EMBEDDED,
            location=target.location,
        )
    return target
