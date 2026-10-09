"""Pure fleet host manifest models shared by Git and SQL authority carriers.

The historic ``beadhive.hosts`` module reexports these exact classes and constants.
Filesystem YAML load/save stays there; runtime signature verification imports this
module without pulling the host/log/config composition graph into its model path.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The closed role set (docs/design/multi-host-model-adr.md, Amendment 1 §3) — a later bead
# (bh-ytbb.6+) reads this to pick renew/TTL defaults.
#
# ONE AXIS, and naming it wrong cost a day (bh-7ztwe): a role says how readily and how long a
# host holds a hive's HOST LEASE, which is what unlocks the write verbs (assign/claim/submit/
# merge, `bh plan file`). Reads are never gated, so every role can look at everything.
#
#   executor   an always-on machine that OWNS repos — long stable tenure, 4x the baseline TTL.
#   transient  comes and goes for a task and releases on exit, CI-runner-shaped; baseline TTL.
#   viewer     never primary. A human's laptop: talk to the supervisor, keep a checkout for
#              navigation and local indexing, never modify or submit.
HOST_ROLES: tuple[str, ...] = ("executor", "transient", "viewer")

# Deprecated spellings, kept resolving so a rename does not strand every already-registered
# host at clone time — an HQ manifest carries the role STRING, and that is the one failure mode
# this rename must not have. Warned on use, removed in a later release.
#
# The old names were replaced because each was wrong rather than merely long, `worker` most of
# all: it named the ONE role that can do no work (it cannot claim, submit or merge), and the
# word was already taken twice over — bd's worker-on-an-issue lease, and "the agent doing the
# work" throughout work.py's prose. Three independent readers hit it in one day and all drew
# the same wrong conclusion; v0.8.0 shipped documentation stating the exact opposite.
DEPRECATED_ROLE_ALIASES: dict[str, str] = {
    "primary-default": "executor",
    "adopt-on-demand": "transient",
    "worker": "viewer",
}


def canonical_role(role: str) -> str:
    """`role` with a deprecated spelling resolved to its current name, warning on use.

    An unknown role passes through UNTOUCHED — validation belongs to the caller (the manifest
    model's ``Literal``, or ``host init``'s explicit check), and quietly absorbing a typo here
    would turn it into a silent default."""
    current = DEPRECATED_ROLE_ALIASES.get(role)
    if current is None:
        return role
    structlog.get_logger("beadhive.hosts").warning(
        "deprecated_host_role",
        deprecated=role,
        replacement=current,
        reason=(
            "host role renamed (bh-7ztwe) — the old names still resolve but will be removed in "
            "a later release; re-record it with "
            "`bh host init --role " + current + " --force`"
        ),
    )
    return current


# The identity mechanisms bh has seen in practice (bh-fry5's motivating incident: a host-wide
# SSH `insteadOf` rewrite silently changed which signing key a subset of repos pushed under).
# "none" names the common vanilla case explicitly, rather than leaving it unset/ambiguous.
IDENTITY_MECHANISM_KINDS: tuple[str, ...] = ("none", "ssh_alias", "insteadOf", "core_sshCommand")


class _Section(BaseModel):
    """Base for every manifest sub-model: forbid unknown keys (config_schema.py's convention),
    so a stale/typo'd key fails validation instead of silently vanishing on read."""

    model_config = ConfigDict(extra="forbid")


class IdentityMechanism(_Section):
    """How this host's git clones resolve remote URLs — the fact bh-fry5's cross-host
    identity-drift check diffs against instead of investigating by hand (SSH'ing in,
    reverse-engineering a downstream tool's identity matcher, ...). ``kind`` is the closed set
    of mechanisms bh has seen in practice; ``value`` is the mechanism's concrete configuration
    (the alias hostname, the insteadOf rewrite rule, or the sshCommand string) so drift shows
    up as a text diff, not just a kind mismatch."""

    kind: Literal["none", "ssh_alias", "insteadOf", "core_sshCommand"] = Field(
        ...,
        description=(
            "none (vanilla — no identity-affecting rewrite) | ssh_alias (~/.ssh/config Host "
            "alias) | insteadOf (git config url.<x>.insteadOf rewrite) | core_sshCommand "
            "(per-repo core.sshCommand override)."
        ),
    )
    value: str = Field(
        "",
        description=(
            "The mechanism's concrete value — alias hostname, insteadOf rewrite rule, or "
            "sshCommand string. Empty for kind=none."
        ),
    )


FRAME_STATES = ("pending", "active", "draining", "drained", "parked", "quarantined", "retired")
FRAME_ISOLATIONS = ("kvm", "microvm", "container", "workstation")
FRAME_TRUST_ZONES = ("self-hosted", "vendor-hosted")


class FrameRelease(_Section):
    """The installed frame release, following the v1alpha1 release shape."""

    id: str
    digest: str


class FrameCapabilities(_Section):
    """Routing facts; per-host harness configuration remains a separate policy block."""

    isolation: Literal["kvm", "microvm", "container", "workstation"]
    trust_zone: Literal["self-hosted", "vendor-hosted"]
    arch: str
    harnesses: list[str]
    max_sessions: int = Field(ge=0)


class HostManifest(_Section):
    """One host's fleet-visible manifest — ``hosts/<host_id>.yaml`` in HQ."""

    frame_id: str | None = Field(None, description="Stable inventory-owned frame identity.")
    beadyard_id: str | None = Field(
        None, description="Canonical HQ instance UUID binding for an enrolled frame."
    )

    @field_validator("beadyard_id")
    @classmethod
    def _canonical_beadyard_id(cls, value):
        if value is not None:
            from .beadyard_identity import parse_id

            return parse_id(value)
        return value

    state: Literal[
        "pending", "active", "draining", "drained", "parked", "quarantined", "retired"
    ] = "active"
    release: FrameRelease | None = None
    capabilities: FrameCapabilities | None = None
    execution_hives: list[str] | None = Field(
        None,
        description="Optional hive prefixes this frame may execute; authoring routes are separate.",
    )
    instance_ref: str | None = Field(None, description="Substrate-owned instance identity.")

    @model_validator(mode="before")
    @classmethod
    def _new_frame_starts_pending(cls, value):
        """New frames require admission; legacy hosts retain their active default.

        The disk reader supplies active for historical manifests missing state, so
        constructing a new frame and reading a legacy record remain distinct.
        """
        if isinstance(value, dict) and value.get("frame_id") and "state" not in value:
            return {**value, "state": "pending"}
        return value

    host_id: str = Field(
        ..., description="The host_id this manifest is keyed by (beadhive.host.host_id())."
    )
    label: str = Field(..., description="Human label (mirrors host.yaml's label at mint time).")
    os: str = Field(..., description="Operating system, e.g. darwin | linux | windows.")
    arch: str = Field(..., description="CPU architecture, e.g. arm64 | x86_64.")
    role: Literal["executor", "transient", "viewer"] = Field(
        ...,
        description=(
            "executor (always-on machine that owns repos, long stable tenure) | transient "
            "(comes and goes for a task, releases on exit — CI-runner-shaped) | viewer (never "
            "primary — a human's laptop: reads, navigates and indexes locally, never submits)."
        ),
    )

    @field_validator("role", mode="before")
    @classmethod
    def _resolve_deprecated_role(cls, v):
        """Accept the pre-bh-7ztwe spellings on READ, so the rename does not strand hosts
        already registered in HQ. Runs BEFORE the ``Literal`` check, which is the whole point:
        a manifest written by v0.8.0 says ``worker``, and a v0.8.1 clone must still parse it
        rather than failing validation on a word it wrote itself."""
        return canonical_role(v) if isinstance(v, str) else v

    identity: IdentityMechanism = Field(
        ..., description="How this host's clones resolve remote URLs — see IdentityMechanism."
    )
    capacity: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Capacity/budget knobs. Deliberately open/free-form — a later bead defines the "
            "concrete keys (e.g. a token budget, concurrent-session limits) without a schema "
            "rewrite here."
        ),
    )
    harnesses: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Placeholder for a future per-host harness config block (previewed by the plan "
            "doc, not yet filed as a concrete bead in this molecule) — free-form until that "
            "model lands."
        ),
    )
    remote_only_hives: list[str] = Field(
        default_factory=list,
        description=(
            "Hive prefixes intentionally not cloned on this host. This is host-local "
            "placement intent, not a change to the fleet-wide managed_repos registry."
        ),
    )
