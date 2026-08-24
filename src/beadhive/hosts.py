"""``hosts/<host_id>.yaml`` — the fleet's roster in Factory HQ (bh-ytbb.3).

The manifest side of the multi-host model (``docs/design/multi-host-model-adr.md``,
Amendment 1 §3): one file per host, in :func:`beadhive.hq.scaffold_layout`'s ``hosts/``
directory, keyed by the SAME ``host_id`` :mod:`beadhive.host` mints locally
(``~/.beadhive/host.yaml``'s ``host_id()``) — NOT that file itself, which is host-local and
never synced (see its module docstring). This module reuses that accessor rather than
re-deriving host identity; it never mints or reads ``host.yaml`` directly.

Carries the ``role`` that makes asymmetric TTL renewal answerable (a later bead, ``bh-ytbb.6``
and on, reads it to pick renew/TTL defaults — ``executor`` machines get long tenure,
``transient`` machines get short explicit adoptions, ``viewer`` never becomes primary),
and the ``identity`` mechanism a host's clones use to resolve remote URLs (ssh alias /
``insteadOf`` rewrite / per-repo ``core.sshCommand``) — the fact ``bh-fry5``'s cross-host
identity-drift check wants to diff against instead of investigate by hand.

Schema + read/write/validate ONLY — no ``bh host`` CLI (that's ``bh-ytbb.5``, which consumes
:func:`load`/:func:`save`/:func:`remove`) and no lease/epoch logic (``bh-ytbb.6`` and on).
``capacity`` and ``harnesses`` are deliberately open (free-form dict) placeholders: the plan
doc previews a future ``harness:`` block and a capacity/budget shape, but neither is filed as
a concrete bead in this molecule yet — a later bead can flesh either out without a schema
rewrite here.

Validation follows the same pydantic convention as :mod:`beadhive.config_schema`
(``extra="forbid"`` at every level, closed ``Literal`` sets): :func:`load` raises
:class:`ManifestError` naming the offending key(s) on a malformed manifest — "fails loudly",
never a silent partial read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from ruamel.yaml import YAML

# Same round-trip settings as host.py/config.py's writers. No comment/flow-style preservation
# needed here — unlike config.yaml, this file is written wholesale by `save()`, never
# hand-edited-and-merged.
_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)

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
    from . import log  # lazy: keep this module importable without the log pipeline

    log.get_logger(__name__).warning(
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


class HostManifest(_Section):
    """One host's fleet-visible manifest — ``hosts/<host_id>.yaml`` in HQ."""

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


class ManifestError(ValueError):
    """A ``hosts/<host_id>.yaml`` manifest failed schema validation on read — the message
    names the offending key(s), per this bead's "fails loudly" acceptance bar."""


def hosts_dir(hq_dir: Path) -> Path:
    """The ``hosts/`` directory under a given HQ store root. Purely a path computation — does
    NOT create it; :func:`beadhive.hq.scaffold_layout` is what creates it (+ its README)."""
    return hq_dir / "hosts"


def manifest_path(hq_dir: Path, host_id: str) -> Path:
    """Where one host's manifest lives under a given HQ store root."""
    return hosts_dir(hq_dir) / f"{host_id}.yaml"


def save(hq_dir: Path, manifest: HostManifest) -> Path:
    """Write ``manifest`` to ``hosts/<host_id>.yaml`` under ``hq_dir``, creating the ``hosts/``
    directory if needed. ``manifest`` is already-validated — a :class:`HostManifest` instance
    cannot exist in an invalid shape — so this never writes something :func:`load` would then
    reject."""
    p = manifest_path(hq_dir, manifest.host_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        _yaml.dump(manifest.model_dump(mode="json"), f)
    return p


def remove(hq_dir: Path, host_id: str) -> Path:
    """Delete ``hosts/<host_id>.yaml`` from ``hq_dir`` — the manifest-removal half of
    ``bh host rm`` (bh-salu: a rebuilt/wiped host mints a NEW ``host_id``, so its old
    manifest never goes away on its own — see :mod:`beadhive.host`'s module docstring).

    Raises ``FileNotFoundError`` when no manifest exists for ``host_id`` — mirrors
    :func:`load`'s own contract rather than silently no-op'ing on an already-gone entry. Every
    GATE (live leases, self-removal, staleness) is the CLI layer's job
    (:mod:`beadhive.host_cli`) — this is schema-agnostic file removal only, same split as
    :func:`save`/:func:`load`."""
    p = manifest_path(hq_dir, host_id)
    if not p.exists():
        raise FileNotFoundError(f"no host manifest for {host_id!r} at {p}")
    p.unlink()
    return p


def load(hq_dir: Path, host_id: str) -> HostManifest:
    """Read + VALIDATE ``hosts/<host_id>.yaml``.

    Raises ``FileNotFoundError`` when no manifest exists yet for ``host_id``. Raises
    :class:`ManifestError` — naming the offending key(s) — when the file exists but fails
    schema validation (unknown key, wrong type, a ``role``/identity ``kind`` outside its
    closed set, ...): never a silent partial/best-effort read.
    """
    p = manifest_path(hq_dir, host_id)
    if not p.exists():
        raise FileNotFoundError(f"no host manifest for {host_id!r} at {p}")
    raw = _yaml.load(p.read_text()) or {}
    try:
        return HostManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(_format_error(p, exc)) from exc


def _format_error(path: Path, exc: ValidationError) -> str:
    """Render a pydantic ``ValidationError`` as a loud, specific message naming each offending
    dotted key — the same ``loc``-joining convention :mod:`beadhive.config_validate` uses."""
    lines = [f"malformed host manifest at {path}:"]
    for err in exc.errors():
        dotted = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  `{dotted}`: {err['msg']}")
    return "\n".join(lines)
