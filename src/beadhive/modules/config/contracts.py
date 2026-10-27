"""Canonical typed contracts for Beadhive configuration.

Single source of truth for keys, types, defaults, enums and field descriptions. This is a
VALIDATION + SCHEMA layer only: it does NOT replace :mod:`beadhive.config`'s ~40 getters or
its ruamel round-trip read/write path (comments + flow-style ``managed_repos`` entries must
survive edits, which a plain pydantic model can't preserve) — those keep working unchanged.
``BeadhiveConfig`` exists so a loaded config can be *checked* against it (bh-5cgm.2's
``validate_config()``), and so the shape is discoverable (bh-5cgm.4's ``bh config schema``).

Versioning (docs/design/config-schema-versioning.md, when it lands — see epic bh-5cgm for the
interim design write-up): ``schema_version`` is a single monotonic integer; ``SCHEMA_VERSION``
is the current one, stamped into fresh configs by the template + ``bh config init``. A version
bump is reserved for a genuinely breaking change (rename/removal/type change needing a
transform); an additive field with a sane default does NOT bump it — so every field here is
optional with a default, and every non-trivial section is a nested sub-model, to keep bumps
rare. ``env_nested_delimiter="__"`` makes any field overridable as ``BH_SECTION__KEY`` (or
``BH_SECTION__SUBSECTION__KEY``); ``nested_model_default_partial_update=True`` means a partial
override (e.g. only ``BH_WORK__MAX_COMMITS``) merges onto the section's defaults instead of
re-deriving the whole sub-model from env alone and wiping its sibling fields.
``extra="forbid"`` at every level (top-level and nested) catches an unknown/stale/typo'd key
instead of silently ignoring it.
"""

from __future__ import annotations

import datetime
import difflib
import json
import os
import re
import types
import typing
from collections.abc import Mapping, Sequence
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    TypeAdapter,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError, PydanticUndefined
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from ...complexity import ComplexityTier, tier_names
from ...kernel.daemon.contracts.config import HostDaemonConfig

#: ISO-8601 duration parser for `WorkConfig.ledger_ttl` — pydantic's own, so `P1D` / `PT30M`
#: need no hand-rolled grammar (`config.duration_seconds` uses the same adapter at read time).
_TIMEDELTA = TypeAdapter(datetime.timedelta)
_HTTP_ENDPOINT = TypeAdapter(AnyHttpUrl)

#: The one placeholder `WorkConfig.validate_subset` must carry, defined here because this module
#: imports nothing of bh's: `converge.PLACEHOLDER` and `config._validate`'s write-path check are
#: both this constant, so the rule cannot drift between the schema, the setter, and the reader.
SUBSET_PLACEHOLDER = "{tests}"

# First official schema version. Bump only for a breaking change (rename/removal/type change
# that needs a transform) — see the module docstring.
SCHEMA_VERSION = 1

# ``beads`` predates the typed model. The canonical compatibility model below
# validates its live engine selector while preserving opaque historical sibling
# keys; the inventory name remains explicit for compatibility callers.
CONFIG_SECTION_COMPATIBILITY_ALIASES = frozenset({"beads"})

# Canonical worktree-safety taxonomy.  The scanner re-exports these names for compatibility,
# while typed configuration and the generated schema own their defaults here.
DEFAULT_PRECIOUS_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.db",
    "*.sqlite*",
    "*.duckdb",
    "dumps/**",
    "data/**",
    "*.dump",
    "*.sql",
    "downloads/**",
    "outputs/**",
    "*.pem",
    "*.key",
)
BD_BACKUP_STORE_GLOBS: tuple[str, ...] = (".beads/backup/**", ".beads/backup.*/**")
DEFAULT_JUNK_GLOBS: tuple[str, ...] = (
    *BD_BACKUP_STORE_GLOBS,
    "node_modules/**",
    ".venv/**",
    "__pycache__/**",
    "*.pyc",
    ".pytest_cache/**",
    ".ruff_cache/**",
    "dist/**",
    "build/**",
    ".mypy_cache/**",
)
DEFAULT_PRECIOUS_MIN_BYTES = 1024 * 1024


class _Section(BaseModel):
    """Base for every nested config section: forbid unknown keys, same as the top level."""

    model_config = ConfigDict(extra="forbid")


# ---- worktrees ---------------------------------------------------------------


class WorktreeInitRule(_Section):
    """One declarative post-create worktree rule (``worktrees.init`` / a hive's
    ``worktree_init``)."""

    run: str = Field(..., description="Shell command to run in the new worktree.")
    if_exists: str | None = Field(
        None, description="Glob in the new worktree; rule only runs when it matches."
    )
    verify: bool = Field(
        False,
        description="Also run this rule in the throwaway verify checkout before validate_cmd.",
    )


_DEFAULT_WORKTREE_INIT = [
    # `*mise.toml`, not `.mise.toml`: mise accepts BOTH spellings and its own docs now favour the
    # undotted one, so the dotted-only predicate never matched a modern repo and `mise trust` never
    # ran there. Every recipe routed through `mise exec` then died on "not trusted" inside the
    # ephemeral verify checkout, and `bh work submit` reported that as a validation failure — so it
    # read as a broken change rather than an unprovisioned machine. Five agents diagnosed it
    # independently and escalated rather than editing global config: correct restraint, five
    # escalations, one glob (bh-ggfr).
    #
    # pathlib's `*` DOES match a leading dot (shell globbing does not), so one pattern covers both
    # spellings, and `any()` means the rule still fires exactly once when a repo carries both. It
    # would also match a stray `custommise.toml` — accepted over a second rule, which would run
    # `mise trust` twice. mise's NESTED locations (`mise/config.toml`, `.config/mise.toml`) stay
    # uncovered: one glob per rule, and they were not the reported failure.
    WorktreeInitRule(if_exists="*mise.toml", run="mise trust", verify=True),
    WorktreeInitRule(if_exists="pyproject.toml", run="uv sync", verify=True),
    WorktreeInitRule(
        if_exists="justfile",
        run=(
            "sh -c 'if just --show setup >/dev/null 2>&1; then just setup; "
            'else echo "just setup: not configured in this repo"; fi\''
        ),
    ),
]


class WorktreesConfig(_Section):
    """Managed worktrees — a shadow tree outside $GIT_WORKSPACE mirroring
    <group>/<org>/<repo>."""

    ephemeral: bool = Field(
        True,
        description=(
            "True (default): worktrees live in an OS temp dir, session-scoped + disposable, "
            "no sandbox grant needed (falls back to the disk root when the temp dir is tmpfs, "
            "unless allow_tmpfs is set). False: persistent worktrees under `path`."
        ),
    )
    allow_tmpfs: bool = Field(
        False,
        description=(
            "Opt in to a RAM-backed (tmpfs/ramfs) worktree root, including verify-* clean "
            "checkouts. Off by default: provisioning on tmpfs is refused and the default root "
            "is disk-backed, because per-tree venvs/caches would consume unreclaimable RAM. "
            "Env: BH_WORKTREES_ALLOW_TMPFS=1."
        ),
    )
    max_live: int = Field(
        64,
        ge=0,
        description=(
            "Per-host ceiling on live worktrees (bead trees and verify-* checkouts). Provisioning "
            "a new one at the cap first reclaims clean, merged trees, then refuses with a message "
            "if it is still full. 0 disables the cap. Env: BH_WORKTREES_MAX_LIVE."
        ),
    )
    ram_warn_gib: float = Field(
        4.0,
        ge=0,
        description=(
            "`bh doctor` warns that a host with no swap (SwapTotal=0) is holding more than this "
            "many GiB in RAM-backed storage (tmpfs/ramfs: worktrees, validation checkouts, "
            "TMPDIR, bh state). It also flags a tmpfs worktree root that is not opted in via "
            "allow_tmpfs and bh state dirs on RAM. 0 turns these doctor warnings off."
        ),
    )
    reclaim_on_merge: bool = Field(
        True,
        description=(
            "Remove a bead's worktree after a clean `work merge` / `work finish` (the branch "
            "stays as the durable artifact). A worktree with uncommitted changes is always kept. "
            "`work merge --rm` forces it for one call; set false to keep trees."
        ),
    )
    path: str | None = Field(
        None, description="Persistent worktree root (ephemeral=false only); $BH_WORKTREES wins."
    )
    manager: Literal["native"] = Field(
        "native",
        description=(
            "The one worktree manager that creates/attaches/removes this fleet's worktrees. "
            "native (git) is the default and only value; anything else is refused."
        ),
    )
    bead_branch: str = Field(
        "bead/{kind}/{id}",
        description="Branch suffix template for a bead worktree ({kind}=epic|issue, {id}=bead id).",
    )
    session_branch: str = Field(
        "session/{ts}-{rand}", description="Branch suffix template for an ad-hoc session worktree."
    )
    rmdir_empty: bool = Field(
        True, description="On rm/prune, remove now-empty triplet dirs up to (never including) root."
    )
    init: list[WorktreeInitRule] = Field(
        default_factory=lambda: list(_DEFAULT_WORKTREE_INIT),
        description="Global post-create provisioning rules, run before a hive's worktree_init.",
    )
    toolchain: str | list[str] | None = Field(
        None, description="Declared toolchain name(s) this repo uses (knowledge-only; bh-d0kb)."
    )
    toolchains: dict[str, dict[str, str]] | None = Field(
        None,
        description="Per-name toolchain registry overrides (replaces, not merges, a template).",
    )


# ---- work (integration-plane driver) -----------------------------------------


class DispatchConfig(_Section):
    """How the dispatcher dispatches ready beads (``work.dispatch``)."""

    mode: Literal["fanout", "collapsed", "auto"] = Field(
        "fanout",
        description="fanout (one dev/bead) | collapsed (batch beads) | auto (choose by budget).",
    )
    max_depth: Literal[0, 1, 2] = Field(
        2, description="Max nesting depth for sub-agent dispatch: 0 (none) | 1 | 2."
    )
    max_beads_per_session: int = Field(
        8, description="Max beads a collapsed session may hold before fanning out."
    )
    auto_budget: int = Field(
        8, description="Work budget (m-sized-beads) an auto-mode session may absorb."
    )
    review_mode: Literal["self", "fresh", "paired"] = Field(
        "self",
        description=(
            "Who reviews a dispatched bead: self (developer self-reviews) | fresh "
            "(separate reviewer) | paired (not yet wired; falls back to fresh)."
        ),
    )
    max_action_retries: int = Field(
        2,
        description=(
            "Loop-breaker: escalate once a bead's own event record already shows N identical "
            "failed attempts of the same action (count DERIVED from event beads, never stored)."
        ),
    )
    reviewer_cross_seat: Literal["advise", "hard"] = Field(
        "hard",
        description=(
            "Self-approval policy: hard (default, bh-e5kv — block self-approval of a "
            "type:human review gate) | advise (warn, allow — explicit opt-out)."
        ),
    )
    # ---- local runtime tier (bh-c6dk.5) ----
    # In-process caps and timings for `bh work loop`. Deliberately under the EXISTING
    # work.dispatch section rather than a parallel one (bh-c6dk.5's design field), and
    # deliberately in-process only: all of these describe THIS loop process's own children, so
    # they reset on restart by design (loop-ownership-and-execution-memory-adr.md Decision 2).
    poll_interval: float = Field(
        5.0,
        description=(
            "Seconds the local runtime sleeps between poll passes. Gate latency is bounded by "
            "this (work-runtime-tiers-adr.md Limitation 1); a push doorbell is out of scope."
        ),
    )
    max_concurrency: int = Field(
        2,
        description=(
            "Max seat processes the local runtime keeps in flight at once. In-process cap, "
            "reset on restart by design; never persisted. This is the ONLY concurrency cap "
            "(`max_seats_in_flight` and its `dispatch_caps` decision core were a second, "
            "never-called implementation with the opposite zero-sentinel and are gone): values "
            "below 1 CLAMP TO 1, so there is no way to spell 'unlimited' and no way to "
            "accidentally spell 'never dispatch anything'."
        ),
    )
    max_run_seconds: float = Field(
        1800.0,
        description=(
            "Per-run wall-time cap: a seat still running after this is cancelled through the "
            "CANCEL ladder (docs/design/work-runtime-tiers-adr.md Amendment 2 §5). In-process, "
            "reset on restart. 0 disables the cap — the one place a 0 sentinel is used, and it "
            "means unlimited."
        ),
    )
    terminate_grace: float = Field(
        5.0,
        description=(
            "Seconds between the reaper's group SIGTERM and its SIGKILL. The loop polls until "
            "the group is actually gone rather than assuming either signal worked."
        ),
    )
    envelope_grace: float = Field(
        3.0,
        description=(
            "Seconds the loop holds the child's stdout pipe after signalling it, waiting for "
            "the priced envelope BEFORE reaping the group (measured at ~0.63s, bh-a7so.7 §4)."
        ),
    )
    seat_command: str = Field(
        "",
        description=(
            "Command template the local runtime spawns for a seat, e.g. 'bh-{role}'. Empty "
            "(default) resolves to 'bh-{role}' on PATH. Shell-split, {role} substituted."
        ),
    )
    seat_bundle: str = Field(
        "",
        description=(
            "Seat bundle passed to every spawned seat as '--bundle <path>' — the seat's "
            "permission mode and tool roster. Empty (default) resolves to the bundle shipped "
            "with bh. '-' passes no bundle, leaving the seat default-closed (permission mode "
            "'plan', every Bash call refused), which no write seat can complete an action "
            "under. A '--bundle' already present in seat_command wins over this. Not passed "
            "when 'bh work loop --seat-binary' substitutes a different binary."
        ),
    )


class BeadsConfig(_Section):
    """How the migrated work/planning cohorts reach Beads (``work.beads``, bh-m36pc)."""

    route: Literal["api", "api+cli-fallback", "cli"] = Field(
        "api",
        description=(
            "Which Beads route the migrated cohorts (assign / claim / resume / abandon / submit "
            "state, plan file, ready / schedule / claim-next, dispatch reads) may take. api "
            "(default): the supervised Beads service only; when no capable session can be "
            "opened the command fails closed, naming `bh host beads start --hive <hive>`. "
            "api+cli-fallback: the API when a capable session opens, else the bd CLI route "
            "selected before the first Beads operation (never a retry). cli: always the bd CLI "
            "route. An unknown value is refused, never defaulted."
        ),
    )
    explicit_routing: bool = Field(
        False,
        description=(
            "Declare bd's own routing on the hive (bh-5lpl9.1): onboard sets `routing.mode: "
            "explicit` and `routing.default: .` in .beads/config.yaml when absent, and "
            "`bh doctor` lists hives where either is unset or differs. Off by default."
        ),
    )


class ConflictConfig(_Section):
    """Merge-conflict resolution policy (``work.conflict``)."""

    union_globs: list[str] = Field(
        default_factory=list,
        description="Globs naming append-only files eligible for union conflict resolution.",
    )


class DevIdentity(_Section):
    """One ``dev/<name>`` seat's identity overrides (``work.identity.devs[dev/<name>]``)."""

    name: str | None = Field(None, description="git user.name override for this seat.")
    email: str | None = Field(None, description="Author email override for this seat.")
    signing_key: str | None = Field(None, description="SSH signing key override for this seat.")
    sign: bool = Field(False, description="commit.gpgsign override for this seat.")


class IdentityConfig(_Section):
    """Agent identity stamped into each worktree at claim/assign (``work.identity``)."""

    mode: Literal["agent", "supervised"] | None = Field(
        None,
        description=(
            "agent (stamp name/email/signing_key below) | supervised (inherit the human's "
            "existing git config unchanged). Defaults to agent when any field is set, else "
            "supervised."
        ),
    )
    name: str | None = Field(None, description="git user.name + bd --actor.")
    email: str | None = Field(None, description="Stable author email (attribution).")
    signing_key: str | None = Field(None, description="SSH signing key (path or literal).")
    sign: bool = Field(False, description="commit.gpgsign.")
    devs: dict[str, DevIdentity] = Field(
        default_factory=dict,
        description="Per-developer identity overrides keyed by dev/<name>, layered over the base.",
    )
    crews: dict[str, DevIdentity] = Field(
        default_factory=dict,
        description="Deprecated alias of `devs` (devs wins on collision).",
    )
    authority: Literal["local"] = Field(
        "local",
        description=(
            "Named ClaimAuthority `bh work claim`/`submit` use to mint + resolve the acting seat "
            "(claim_authority.py). `local` (default, only tier shipped today) is LOCAL-TRUST "
            "ONLY — no spoof resistance; see the module docstring for the anti-spoof tiers "
            "tracked in spike bh-zspz."
        ),
    )


_MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:@+-]*$")
_ENDPOINT_PROFILE = re.compile(r"^(?:profile:)?[A-Za-z][A-Za-z0-9._-]*$")


class RoutingTierConfig(_Section):
    """One model route and the inclusive complexity interval it can serve.

    The model identifier deliberately validates syntax, not a provider catalogue: providers and
    model releases change independently of Beadhive. Endpoint authentication and TLS policy do
    not belong in this entry; ``endpoint`` only selects an HTTP(S) gateway or a named profile.
    """

    model: str = Field(
        ...,
        description=(
            "Model identifier in provider/model-name form; provider and model names are not "
            "catalogue-validated."
        ),
    )
    floor: ComplexityTier = Field(
        ComplexityTier.SIMPLE,
        description="Lowest complexity tier served (inclusive); omitted means SIMPLE.",
    )
    ceiling: ComplexityTier = Field(
        ComplexityTier.REASONING,
        description="Highest complexity tier served (inclusive); omitted means REASONING.",
    )
    endpoint: str | None = Field(
        None,
        description=(
            "Optional HTTP(S) gateway URL or endpoint-profile reference; omitted selects the "
            "configured role/harness default. Authentication and TLS policy are resolved outside "
            "this entry."
        ),
    )

    @field_validator("model")
    @classmethod
    def _provider_model_identifier(cls, value):
        if not isinstance(value, str) or not _MODEL_IDENTIFIER.fullmatch(value):
            raise ValueError(
                "model must use non-empty provider/model-name form (for example "
                "'openai/gpt-5'); provider catalogues are not validated"
            )
        return value

    @field_validator("floor", "ceiling", mode="before")
    @classmethod
    def _canonical_complexity_tier(cls, value):
        if isinstance(value, ComplexityTier):
            return value
        try:
            return ComplexityTier.parse(value)
        except ValueError as exc:
            raise ValueError(
                f"complexity bound must be one of {'|'.join(tier_names())}, got {value!r}"
            ) from exc

    @field_validator("endpoint")
    @classmethod
    def _gateway_or_profile(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(
                "endpoint must be a non-empty HTTP(S) URL or endpoint-profile reference"
            )
        if "://" not in value:
            if not _ENDPOINT_PROFILE.fullmatch(value):
                raise ValueError(
                    "endpoint must be an HTTP(S) URL or endpoint-profile reference containing "
                    "only letters, digits, '.', '_' and '-' (optionally prefixed by 'profile:')"
                )
            return value

        try:
            _HTTP_ENDPOINT.validate_python(value)
        except ValidationError as exc:
            raise ValueError(
                "endpoint URL must be absolute and use a supported http or https scheme"
            ) from exc
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(
                "endpoint URL must not embed authentication; resolve credentials outside the "
                "routing tier entry"
            )
        return value

    @model_validator(mode="after")
    def _ordered_bounds(self):
        if self.floor > self.ceiling:
            raise ValueError(f"floor {self.floor.name} must not exceed ceiling {self.ceiling.name}")
        return self

    @field_serializer("floor", "ceiling")
    def _serialize_complexity_tier(self, value: ComplexityTier) -> str:
        return value.name


class RoutingConfig(_Section):
    """Complexity-to-model routing intent; selection behavior belongs to routing readers."""

    policy: Literal["loose", "strict"] = Field(
        "loose",
        description=(
            "Resolution policy when no configured interval matches: loose (default, permit the "
            "reader's fallback) or strict (require an explicit match)."
        ),
    )
    tiers: list[RoutingTierConfig] = Field(
        default_factory=list,
        description="Ordered model routes with inclusive complexity floor/ceiling bounds.",
    )


#: An attest key's name: a lowercase slug, stable because receipts and ledger records cite it.
_ATTEST_KEY_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class AttestKeyResourcesConfig(_Section):
    """Optional per-key validation reservation (bh-9oc32): REPLACES the code default for the key.

    Absent means the default in ``beadhive.validation_reservations``; there is no field-wise
    merge, so an override states the whole reservation."""

    cpu_min: float = Field(..., ge=0, description="CPUs that must be free before the key starts.")
    cpu_max: float = Field(..., ge=0, description="Most CPUs the broker may grant (speedup knee).")
    memory_mb: int = Field(
        ..., ge=0, description="Memory in MiB at cpu_max, including tmpfs scratch."
    )
    pools: dict[str, int] = Field(
        default_factory=dict,
        description="Named pool amounts, e.g. {'dolt-servers': 2}.",
    )
    expected_s: float | None = Field(
        None, gt=0, description="Expected wall seconds; absent uses the measured default."
    )

    @model_validator(mode="after")
    def _ordered(self) -> AttestKeyResourcesConfig:
        if self.cpu_max < self.cpu_min:
            raise ValueError("attest key resources need cpu_min <= cpu_max")
        if any(amount < 0 for amount in self.pools.values()):
            raise ValueError("attest key resources pool amounts must be non-negative")
        return self


class AttestKeyConfig(_Section):
    """One attest key (Attested Green ADR, Amendment 1): an OPAQUE command bh runs verbatim and
    never parses, an execution state, an evidence policy, and per-backend selectors only the
    named impact backend reads."""

    name: str = Field(
        ...,
        description=(
            "Stable key name, a lowercase slug (letters, digits, '.', '_', '-'). Receipts and "
            "verdict records cite it."
        ),
    )
    cmd: str = Field(
        ...,
        min_length=1,
        description="The command, run verbatim like validate_cmd. bh never parses it.",
    )
    policy: Literal["required", "optional"] = Field(
        "required",
        description=(
            "required (default) blocks when the key is absent or red | optional passes when "
            "absent but still blocks on a real failure."
        ),
    )
    enabled: bool = Field(
        True,
        description=(
            "Whether this key executes. Disabled keys are deliberately absent: they neither "
            "run, block, satisfy the aggregate, nor produce carryable proof."
        ),
    )
    disabled_reason: str | None = Field(
        None,
        description="Required non-blank operator reason whenever enabled is false.",
    )
    disabled_until: datetime.datetime | None = Field(
        None,
        description=(
            "Optional timezone-aware expiry. Once reached, a disabled key automatically runs "
            "again (fail-closed) even while enabled remains false."
        ),
    )
    resources: AttestKeyResourcesConfig | None = Field(
        None,
        description=(
            "Optional validation reservation for this key (cpu_min, cpu_max, memory_mb, pools). "
            "Absent uses the built-in per-key default. Never part of the command or its hash."
        ),
    )
    selectors: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Opaque {backend: selector} map (e.g. a build-graph tag) read only by that impact "
            "backend. A key with no selector for the active backend is invalidated by any "
            "change — give none to a key that reads git metadata."
        ),
    )

    @field_validator("name")
    @classmethod
    def _slug_name(cls, v):
        if not _ATTEST_KEY_NAME.fullmatch(v):
            raise ValueError(f"attest key name must be a lowercase slug (a-z0-9._-): {v!r}")
        return v

    @field_validator("cmd")
    @classmethod
    def _non_blank_cmd(cls, v):
        if not v.strip():
            raise ValueError("attest key cmd must not be blank")
        return v

    @field_validator("disabled_until")
    @classmethod
    def _aware_disabled_until(cls, v):
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            raise ValueError("attest key disabled_until must include a timezone")
        return v

    @model_validator(mode="after")
    def _disabled_has_reason(self) -> AttestKeyConfig:
        if not self.enabled and not (self.disabled_reason or "").strip():
            raise ValueError("disabled attest key requires a non-blank disabled_reason")
        return self

    def is_disabled(self, at: datetime.datetime | None = None) -> bool:
        """Whether the execution disable is active at ``at`` (UTC now by default).

        Expiry deliberately fails closed: at or after ``disabled_until`` the key is enabled
        again, even if an operator forgot to clean up the stale disable metadata.
        """
        if self.enabled:
            return False
        if self.disabled_until is None:
            return True
        now = at or datetime.datetime.now(datetime.UTC)
        return now < self.disabled_until


class AttestImpactConfig(_Section):
    """Which impact resolver backend decides which keys a change invalidates."""

    backend: str = Field(
        "native-full",
        description=(
            "Impact resolver backend. native-full (default) invalidates every key on every "
            "change — today's all-or-nothing gate, byte for byte. Any other name must be a "
            "backend bound on this host; an unavailable one degrades to native-full."
        ),
    )
    timeout_seconds: float = Field(
        300.0,
        gt=0,
        description=(
            "Wall-clock budget for one backend answer; a slower answer is discarded and the "
            "resolution falls back to native-full."
        ),
    )
    on_unresolved: Literal["fallback", "strict"] = Field(
        "fallback",
        description=(
            "Policy when the impact resolver cannot answer. fallback (the production default) "
            "runs every key; strict is a development diagnostic that exits before any key runs."
        ),
    )

    @field_validator("backend")
    @classmethod
    def _slug_backend(cls, v):
        if not _ATTEST_KEY_NAME.fullmatch(v):
            raise ValueError(f"impact backend must be a lowercase slug (a-z0-9._-): {v!r}")
        return v


class AttestTrivialConfig(_Section):
    """A POLICY short-circuit: changes whose every path is trivially non-behavioral run a
    reduced key set instead of the full one.

    This is deliberately NOT an impact-resolver concern. A receipt answers "what did this
    change touch", proved from ownership and selectors; this answers "what do we choose to
    test", which carries no proof at all. Keeping them apart is what stops a policy skip from
    being recorded as carried evidence: the keys this drops are treated exactly like a
    disabled key -- they neither run, block, satisfy the aggregate, nor produce carryable
    proof, so `attested_green` still refuses to call the revision fully attested."""

    enabled: bool = Field(
        False,
        description=(
            "Whether the trivial-change short-circuit applies. Off by default: a hive must "
            "choose to trade coverage for latency."
        ),
    )
    paths: list[str] = Field(
        default_factory=list,
        description=(
            "fnmatch glob patterns, matched against every changed path. The short-circuit "
            "applies ONLY when EVERY changed path matches one of them, so a single unmatched "
            "path restores the normal route. NOTE: these are fnmatch patterns, not pathlib "
            "globs -- '*' CROSSES directory separators and '**' has no special meaning, so "
            "'docs/*.md' matches any depth and 'docs/*' also matches 'docs/schemas/x.json'. "
            "Prefer the narrowest extension-anchored pattern you can write; the semantic "
            "triage exists to veto a pattern that turns out broader than intended."
        ),
    )
    keys: list[str] = Field(
        default_factory=list,
        description=(
            "Key names that still run for a trivial change. Every other key is skipped by "
            "policy. An empty list disables the short-circuit rather than skipping everything."
        ),
    )


class AttestVersionBumpConfig(_Section):
    """CARRY the parent's behaviour-key verdicts through a version-only release bump.

    Unlike ``trivial`` (a policy skip that leaves the revision not fully attested), this is
    evidence: when HEAD differs from its single, attested-green parent ONLY by a version bump
    (two judges must agree: these path globs AND a content judge that parses the diff), the
    named keys are carried from the parent tree and recorded as carried, with the parent tree
    and the judge record in the receipt. Every other key runs normally."""

    enabled: bool = Field(
        False,
        description=(
            "Whether a version-only bump carries the parent's verdicts. Off by default: a hive "
            "must opt in."
        ),
    )
    paths: list[str] = Field(
        default_factory=lambda: ["CHANGELOG.md", "pyproject.toml", "uv.lock", "server.json"],
        description=(
            "fnmatch globs; EVERY changed path must match one (path judge). The content judge "
            "additionally requires the diff to be a version bump only."
        ),
    )
    keys: list[str] = Field(
        default_factory=list,
        description=(
            "Key names whose parent verdict is carried (the behaviour keys, e.g. integration, "
            "stateful, demos, architecture-contracts, root-composition). Keys not named run "
            "normally. An empty list disables the policy."
        ),
    )


class AttestSemanticConfig(_Section):
    """Optional fail-closed policy selector for attest keys.

    The command is an adviser, not an evidence backend: skipped keys produce no carryable
    proof.  It receives ``--base`` and ``--head`` and must emit the ``jevwrap/select/1`` JSON
    envelope.  Any absence, error, timeout, or malformed/partial answer selects every key.
    """

    enabled: bool = False
    command: str = Field("jevwrap select", min_length=1)
    timeout_seconds: float = Field(30.0, gt=0)


class AttestConfig(_Section):
    """The attest key catalog and impact resolver (Attested Green ADR, Amendment 1). Absent (the
    default) is today's behavior: no keys, native-full."""

    keys: list[AttestKeyConfig] = Field(
        default_factory=list,
        description="Named attest keys, each an opaque command with a policy and selectors.",
    )
    impact: AttestImpactConfig = Field(
        default_factory=AttestImpactConfig,
        description="Impact resolver backend selection.",
    )
    trivial: AttestTrivialConfig = Field(
        default_factory=AttestTrivialConfig,
        description="Policy short-circuit for trivially non-behavioral changes.",
    )
    version_bump: AttestVersionBumpConfig = Field(
        default_factory=AttestVersionBumpConfig,
        description="Carry the parent's verdicts through a content-checked version-only bump.",
    )
    semantic: AttestSemanticConfig = Field(
        default_factory=AttestSemanticConfig,
        description="Advisory semantic key selection; never produces carryable proof.",
    )

    drift_check: str | None = Field(
        None,
        description=(
            "Opt-in runtime catalog drift check (attest-keys ADR, 3.6): an OPAQUE command bh runs "
            "at the evaluated revision's checkout with the live catalog (name, cmd, enabled) as "
            "JSON on stdin. Exit 0 means no drift; anything else is drift. Unset (the default) "
            "runs nothing."
        ),
    )
    drift_check_timeout_seconds: float = Field(
        60.0,
        gt=0,
        description="Seconds the drift-check command may run before it counts as drift.",
    )

    @field_validator("drift_check")
    @classmethod
    def _drift_check_not_blank(cls, v):
        if v is not None and not v.strip():
            raise ValueError("attest drift_check must not be blank")
        return v

    @field_validator("keys")
    @classmethod
    def _unique_names(cls, v):
        names = [key.name for key in v]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate attest key names: {duplicates}")
        return v


class GateCarryConfig(_Section):
    """High-water carry source discovery (attest-keys ADR, section 3.5).

    A carry's source is always a current green run of the key (its *root*). With high-water
    discovery the evaluator may also try the root that a valid carried record on the receipt
    base points at, and the newest same-command green roots within TTL, each re-proven by one
    receipt spanning root to head. The root's age and TTL are kept; nothing is refreshed."""

    high_water: bool | None = Field(
        None,
        description=(
            "Discover carry roots beyond the receipt base. Unset follows work.gate.model: on "
            "under `keys`, off otherwise (single-hop sourcing, today's behaviour). false "
            "restores single-hop sourcing explicitly."
        ),
    )
    max_roots: int = Field(
        3,
        ge=0,
        description=(
            "Newest same-command green runs within TTL tried as carry roots per key, after the "
            "base's own root. 0 disables that ledger scan."
        ),
    )


class ValidationPriorityConfig(_Section):
    """Best-effort host scheduling controls for validation subprocesses."""

    enabled: bool = Field(
        True,
        description=(
            "Apply reduced CPU and I/O scheduling priority when host facilities are available. "
            "BH_VALIDATION_PRIORITY temporarily overrides this setting."
        ),
    )
    nice: int = Field(
        10,
        ge=0,
        le=19,
        description="Process nice level. Only non-negative values are accepted.",
    )
    ionice_class: Literal[2, 3] = Field(
        2,
        description=(
            "Linux I/O scheduling class: 2 is best-effort; 3 is idle. "
            "Realtime class 1 is intentionally rejected."
        ),
    )
    ionice_priority: int = Field(
        7,
        ge=0,
        le=7,
        description=(
            "Best-effort I/O priority from 0 (highest) to 7 (lowest); ignored for idle class 3."
        ),
    )


_MEMORY_SIZE = re.compile(r"^(?:\d+(?:\.\d+)?%|\d+[KMGT]?|infinity|none)?$", re.IGNORECASE)


def _memory_size(value: object, *, name: str, allow_zero: bool) -> str:
    """Validate one systemd-style memory size: bytes, K/M/G/T, a % of RAM, or none."""
    text = str(value).strip()
    if isinstance(value, bool) or not _MEMORY_SIZE.match(text):
        raise ValueError(
            f"{name} must be bytes, a K/M/G/T size, a percentage of RAM, or none: {value!r}"
        )
    if text.endswith("%") and not 0 < float(text[:-1]) <= 100:
        raise ValueError(f"{name} percentage must be greater than 0 and at most 100")
    if not allow_zero and text and text[0].isdigit() and float(text.rstrip("%KMGTkmgt")) == 0:
        raise ValueError(f"{name} must be greater than zero; use none to disable it")
    return text


class ValidationMemoryConfig(_Section):
    """Memory bounds and memory-aware admission for validation subprocesses (bh-jg7fy)."""

    enabled: bool = Field(
        True,
        description=(
            "Bound validation memory in the user systemd scope and wait for the admission floor. "
            "BH_VALIDATION_MEMORY temporarily overrides this setting."
        ),
    )
    memory_high: str = Field(
        "50%",
        description=(
            "Scope MemoryHigh: reclaim/throttle threshold as bytes, a K/M/G/T size, a percentage "
            "of physical RAM, or none."
        ),
    )
    memory_max: str = Field(
        "60%",
        description=(
            "Scope MemoryMax: the kernel OOM-kills inside the scope above this bound. Bytes, a "
            "K/M/G/T size, a percentage of physical RAM, or none."
        ),
    )
    memory_swap_max: str = Field(
        "0",
        description=(
            "Scope MemorySwapMax so an over-limit run is killed instead of swapping; none "
            "leaves swap unbounded."
        ),
    )
    admission_floor: str = Field(
        "10%",
        description=(
            "Wait before starting until MemAvailable is at least this size (bytes, K/M/G/T, or a "
            "percentage of RAM); 0 or none disables. BH_VALIDATION_MEMORY_FLOOR overrides it."
        ),
    )
    admission_timeout_seconds: float = Field(
        1800.0,
        ge=0,
        description=(
            "Give up (not started, exit 75) after waiting this long for the floor; 0 waits "
            "indefinitely."
        ),
    )
    admission_poll_seconds: float = Field(
        5.0,
        gt=0,
        description="Seconds between MemAvailable samples while waiting for the floor.",
    )

    @field_validator("memory_high", "memory_max", mode="before")
    @classmethod
    def _limit(cls, value, info):
        return _memory_size(value, name=info.field_name, allow_zero=False)

    @field_validator("memory_swap_max", "admission_floor", mode="before")
    @classmethod
    def _bound(cls, value, info):
        return _memory_size(value, name=info.field_name, allow_zero=True)


class ValidationBrokerBudgetConfig(_Section):
    """`work.validation_broker.budget` — the most the local engine may grant at once (bh-qrixp)."""

    cpu: float = Field(
        0.0,
        ge=0,
        description="CPUs the local engine may grant at once; 0 means every CPU this host reports.",
    )
    memory: str = Field(
        "100%",
        description=(
            "Memory the local engine may grant at once: bytes, a K/M/G/T size or a percentage of "
            "RAM. Admission also never exceeds measured MemAvailable less the floor."
        ),
    )
    pools: dict[str, int] = Field(
        default_factory=lambda: {"dolt-servers": 28},
        description=(
            "Named counted pools no sensor can see, e.g. dolt-servers: concurrent Dolt servers "
            "the host's validation keys may hold. A reservation naming an absent pool is refused."
        ),
    )

    @field_validator("memory", mode="before")
    @classmethod
    def _memory(cls, value, info):
        return _memory_size(value, name=info.field_name, allow_zero=False)

    @field_validator("pools")
    @classmethod
    def _non_negative_pools(cls, v):
        for name, amount in v.items():
            if amount < 0:
                raise ValueError(f"work.validation_broker.budget.pools.{name} must be >= 0")
        return v


class ValidationBrokerFloorConfig(_Section):
    """`work.validation_broker.floor` — what the local engine leaves free (the AMBER floor)."""

    cpu: float = Field(0.0, ge=0, description="CPUs held back from the measured CPU count.")
    memory: str = Field(
        "4G",
        description=(
            "Memory held back from measured MemAvailable: bytes, a K/M/G/T size or a percentage "
            "of RAM. The floor is the larger of this and memory_percent."
        ),
    )
    memory_percent: float = Field(
        8.5,
        ge=0,
        le=100,
        description="Memory held back as a percentage of RAM; the larger of the two floors wins.",
    )

    @field_validator("memory", mode="before")
    @classmethod
    def _memory(cls, value, info):
        return _memory_size(value, name=info.field_name, allow_zero=True)


class ValidationBrokerPackerConfig(_Section):
    """`work.validation_broker.packer` — the group-aware packer's knobs (validation-broker ADR)."""

    laggard_first: bool = Field(
        True, description="Within a gate, start the longest expected key first."
    )
    laggard_flex: bool = Field(
        True,
        description="Offer each gate's laggard spare CPU up to cpu_max before the headroom rule.",
    )
    easy: bool = Field(
        True,
        description="Generalised EASY backfill: a backfill never delays a reserved blocked key.",
    )
    headroom_cpu: float = Field(
        8.0, ge=0, description="CPUs held back from flexible grants for keys other than a laggard."
    )
    group_major: bool = Field(
        False,
        description=(
            "Reorder waiting gates by queued work minus aging x time waited; off admits gates "
            "in arrival order within a priority class."
        ),
    )
    aging: float = Field(
        4.0, ge=0, description="Work-seconds forgiven per second waited; only with group_major."
    )


class ValidationBrokerRootConfig(_Section):
    """One `work.validation_broker.roots` entry — a bounded execution root (bh-mf1o4)."""

    path: str = Field(
        ...,
        description=(
            "Absolute directory (~ expands) the local engine may place key runs on. Each run's "
            "clean checkout and hermetic fence TMPDIR scratch both live under it."
        ),
    )
    kind: Literal["disk", "tmpfs"] = Field(
        ...,
        description="disk, or tmpfs: a RAM-backed root whose usage also counts against memory.",
    )
    max_bytes: str = Field(
        ...,
        description=(
            "The most the root's runs may reserve at once: bytes or a K/M/G/T size. Admission "
            "also never exceeds the filesystem's measured free space."
        ),
    )

    @field_validator("path")
    @classmethod
    def _absolute(cls, v):
        if not os.path.isabs(os.path.expanduser(v)):
            raise ValueError(f"work.validation_broker.roots path must be absolute: {v!r}")
        return v

    @field_validator("max_bytes", mode="before")
    @classmethod
    def _size(cls, value, info):
        text = _memory_size(value, name=info.field_name, allow_zero=False)
        if not text or text.endswith("%") or text.lower() in ("none", "infinity"):
            raise ValueError("max_bytes must be bytes or a K/M/G/T size greater than zero")
        return text


class ValidationBrokerConfig(_Section):
    """`work.validation_broker` — which engine admits validation jobs (bh-qrixp, opt-in)."""

    engine: Literal["slots", "local"] = Field(
        "slots",
        description=(
            "slots (default) keeps today's host file-lock slots exactly; local is the in-process "
            "reservation broker. BH_VALIDATION_BROKER_ENGINE temporarily overrides it. A local "
            "engine that is misconfigured, unavailable or failing falls back to slots, loudly."
        ),
    )
    budget: ValidationBrokerBudgetConfig = Field(
        default_factory=ValidationBrokerBudgetConfig,
        description="The local engine's configured budget (cpu, memory, named pools).",
    )
    floor: ValidationBrokerFloorConfig = Field(
        default_factory=ValidationBrokerFloorConfig,
        description="What the local engine leaves free of measured availability.",
    )
    packer: ValidationBrokerPackerConfig = Field(
        default_factory=ValidationBrokerPackerConfig,
        description="Ordering, backfill and flexible-grant knobs of the local engine.",
    )
    repoll_seconds: float = Field(
        1.0,
        gt=0,
        description="Seconds between availability re-reads while a local-engine client waits.",
    )
    roots: list[ValidationBrokerRootConfig] = Field(
        default_factory=list,
        description=(
            "Execution roots (bh-mf1o4, local engine only), in preference order. Each key run "
            "is placed on exactly one root holding its checkout and fence scratch. Empty "
            "(default): runs use today's locations."
        ),
    )
    setup: list[str] = Field(
        default_factory=lambda: ["uv sync --all-packages --inexact"],
        description=(
            "Local engine only (bh-qhrl6): commands run once in a gate's shared checkout, after "
            "the verify init rules and before any key starts, so no key's own sync changes the "
            "shared environment under another key. A failing command sends the gate's keys to "
            "the sequential slots path. Default: sync every uv workspace package."
        ),
    )

    @field_validator("setup")
    @classmethod
    def _setup_commands(cls, v):
        if any(not command.strip() for command in v):
            raise ValueError("work.validation_broker.setup commands must be non-empty")
        return v

    @model_validator(mode="after")
    def _distinct_roots(self):
        paths = [os.path.normpath(os.path.expanduser(root.path)) for root in self.roots]
        for i, first in enumerate(paths):
            for second in paths[i + 1 :]:
                if os.path.commonpath([first, second]) in (first, second):
                    raise ValueError(
                        "work.validation_broker.roots must be distinct and not nested: "
                        f"{first} and {second}"
                    )
        return self


class GatePushConfig(_Section):
    """`work.gate.push` — the pre-push hook's opt-in per-key acceptance (attest-keys ADR 3.8)."""

    model_config = ConfigDict(extra="forbid")

    accept_attested_green: bool = Field(
        False,
        description=(
            "Opt in: the push-main hook also accepts the exact pushed tree when every key required "
            "by its configured profile is current or carried at that tree (selective_validation."
            "attested_green). Off (the default) keeps today's aggregate-command lookup and full "
            "gate byte for byte; an absent or red key, drift, or any error falls through to them."
        ),
    )


#: The closed lifecycle phase vocabulary a `work.gate.profiles` entry may name (attest-keys
#: ADR 3.2). `default` is not a phase: it is the profile every unlisted phase uses.
GATE_PHASES = (
    "check",
    "submit",
    "review",
    "merge",
    "merge-main",
    "molecule",
    "postland",
    "union",
    "release",
    "push-main",
)
#: The profile value meaning "every active key".
GATE_PROFILE_ALL = "all"
GATE_PROFILE_DEFAULT = "default"


def _gate_profile_names(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


class GateConfig(_Section):
    """`work.gate` — gate-model settings (attest-keys single-gate ADR, 3.2, 3.5 and 3.8)."""

    model_config = ConfigDict(extra="forbid")

    model: Literal["legacy", "keys"] = Field(
        "legacy",
        description=(
            "Gate model. legacy (the default) runs today's per-phase command strings and every "
            "catalog key at every selective boundary; keys runs each phase's profile through "
            "the selective evaluator, with the runtime catalog drift check and a fallback to "
            "the phase's legacy command on drift. Setting legacy is the rollback."
        ),
    )
    profiles: dict[str, str | list[str]] = Field(
        default_factory=dict,
        description=(
            "Per-phase key profile, consulted only under model: keys. Keys are phases (check, "
            "submit, review, merge, merge-main, molecule, postland, union, release, push-main) "
            "or `default`; values are `all`, a subset name, or an explicit list of key names. "
            "Every phase accepts project-defined profiles and inherits `default` (itself `all` "
            "when unset). Projects determine their own strictness and boundary policy. "
            "A `merge-main` profile wins for a merge onto "
            "the integration branch, else `merge`."
        ),
    )
    subsets: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Named key subsets a profile may reference, e.g. fast: [docs, unit].",
    )
    carry: GateCarryConfig = Field(
        default_factory=GateCarryConfig,
        description="High-water carry source discovery.",
    )
    push: GatePushConfig = Field(default_factory=GatePushConfig)

    @field_validator("profiles")
    @classmethod
    def _known_phases(cls, v):
        unknown = sorted(set(v) - set(GATE_PHASES) - {GATE_PROFILE_DEFAULT})
        if unknown:
            raise ValueError(
                f"unknown gate phase(s) {unknown}: profiles name one of "
                f"{[*GATE_PHASES, GATE_PROFILE_DEFAULT]}"
            )
        for phase, value in v.items():
            names = _gate_profile_names(value)
            if not names:
                raise ValueError(f"work.gate.profiles.{phase} must not be empty")
            for name in names:
                if not _ATTEST_KEY_NAME.fullmatch(name):
                    raise ValueError(
                        f"work.gate.profiles.{phase}: {name!r} is not a lowercase slug"
                    )
        return v

    @field_validator("subsets")
    @classmethod
    def _slug_subsets(cls, v):
        for subset, names in v.items():
            if subset == GATE_PROFILE_ALL or not _ATTEST_KEY_NAME.fullmatch(subset):
                raise ValueError(f"invalid gate subset name {subset!r}")
            if not names:
                raise ValueError(f"work.gate.subsets.{subset} must not be empty")
            for name in names:
                if not _ATTEST_KEY_NAME.fullmatch(name):
                    raise ValueError(f"work.gate.subsets.{subset}: {name!r} is not a key name")
        return v


def gate_reference_errors(
    profiles: Mapping[str, str | list[str]],
    subsets: Mapping[str, list[str]],
    key_names: Sequence[str],
) -> list[str]:
    """Unknown subset or key names in an EFFECTIVE (layered) gate configuration.

    A string profile is `all` or a subset name; a list profile names keys directly. Key names
    are checked only when a catalog is in effect: with no keys the profiles are never consulted
    (a no-catalog hive keeps its `validate_cmd`)."""
    errors: list[str] = []
    known = set(key_names)
    for phase, value in sorted(profiles.items()):
        if isinstance(value, str):
            if value != GATE_PROFILE_ALL and value not in subsets:
                errors.append(f"work.gate.profiles.{phase}: unknown subset {value!r}")
        elif known:
            missing = sorted(set(value) - known)
            if missing:
                errors.append(f"work.gate.profiles.{phase}: unknown key name(s) {missing}")
    if known:
        for subset, names in sorted(subsets.items()):
            missing = sorted(set(names) - known)
            if missing:
                errors.append(f"work.gate.subsets.{subset}: unknown key name(s) {missing}")
    return errors


class WorkConfig(_Section):
    """Integration-plane driver (`bh work`) settings — drives a bead assigned -> merged."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    validate_cmd: str = Field(
        "just check", description="Default validation command for any boundary without an override."
    )
    validation_bypass: bool = Field(
        False,
        description=(
            "Emergency per-hive switch that bypasses validation command execution without "
            "changing configured commands or creating green attestations."
        ),
    )
    validation_slots: int = Field(
        1,
        ge=0,
        description=(
            "Host-wide concurrent validation capacity. One is the safe default; zero disables "
            "admission. BH_VALIDATION_SLOTS temporarily overrides this host-local value."
        ),
    )
    validation_priority: ValidationPriorityConfig = Field(
        default_factory=ValidationPriorityConfig,
        description=(
            "Host scheduling policy for validation children: nice/ionice and a weighted user "
            "systemd scope when available."
        ),
    )
    validation_memory: ValidationMemoryConfig = Field(
        default_factory=ValidationMemoryConfig,
        description=(
            "Host memory policy for validation children: MemoryHigh/MemoryMax in the user "
            "systemd scope and a MemAvailable admission floor."
        ),
    )
    validation_broker: ValidationBrokerConfig = Field(
        default_factory=ValidationBrokerConfig,
        description=(
            "Validation broker engine selection and the local engine's budget, floor and packer "
            "(validation-broker ADR). Opt-in: the default slots engine is today's admission."
        ),
    )
    validation_protocol: Literal["none", "beadhive-validation-result/v1"] = Field(
        "none",
        description=(
            "Explicit opt-in typed runner result protocol. v1 exports "
            "BH_VALIDATION_RESULT_PATH; absent/invalid output remains ordinary process semantics."
        ),
    )
    validation_artifact_root: str = Field(
        "",
        description=(
            "Optional absolute root for durable per-run validation artifacts. "
            "BH_VALIDATION_ARTIFACT_ROOT takes precedence for CI upload staging."
        ),
    )
    validate_subset: str = Field(
        "",
        description=(
            "OPTIONAL template for re-running only NAMED tests, with one required {tests} "
            "placeholder — e.g. `./scripts/hermetic.sh uv run pytest -n auto --pyargs {tests}`. "
            "Absent (the default) is fully supported: no converge loop, every phase runs whole. "
            "DEVELOPER-LOOP ONLY — never consulted on a run that writes an attestation, because "
            "re-running only the failures until they pass is how a flaky suite gets laundered "
            "into green (attested-green ADR, settled decision 1). See converge.py."
        ),
    )
    always_run: str = Field(
        "",
        description=(
            "OPTIONAL command run before any recorded validation verdict is honored — the small "
            "set a TREE hash cannot vouch for, because it reads git METADATA rather than file "
            "content (`git describe`, commit counts, tag-derived versions). E.g. "
            "`./scripts/hermetic.sh uv run pytest -m always_run`. Spawned opaquely in the hive's "
            "own clone; bh never learns what it selects. Absent (the default) is fully "
            "supported: a hit is honored whole, exactly as before. A non-zero exit REFUSES the "
            "hit and seals the ledger for the rest of the process, so that failure can never "
            "become an attestation (attested-green ADR, git-metadata asterisk; bh-ehmd8). It is "
            "paid on every HIT, so keep it seconds, not minutes."
        ),
    )
    validation: Literal["relaxed", "conservative", "loose"] = Field(
        "relaxed",
        description=(
            "Which merge boundaries re-test the tip: relaxed (submit + assembled-mol pre-land) | "
            "conservative (also after every per-bead merge AND post-land) | loose (skip pre-land)."
        ),
    )
    validate_overrides: dict[str, str] = Field(
        default_factory=dict,
        alias="validate",
        description=(
            "Per-boundary validate_cmd overrides, keyed by phase (submit|merge|molecule|"
            "postland|union), falling back to validate_cmd. A `<phase>-main` key wins when "
            "the op targets the integration branch."
        ),
    )
    demo_cmd: str = Field(
        "", description="How `bh work review --demo` exercises the feature (default none)."
    )
    review_gate: str = Field(
        "human", description="bd gate type opened at submit: human | timer | gh:run | gh:pr."
    )
    routing: RoutingConfig = Field(
        default_factory=RoutingConfig,
        description="Complexity-tier model routing intent (selection is reader-owned).",
    )
    runtime: Literal["claude", "local", "temporal"] = Field(
        "local",
        description=(
            "Which scheduler wakes a role binary for a ready bead: claude (today's Task-tool "
            "sub-agent fanout — documented, not a bh-owned implementation; harness-bound, "
            "cannot be the default) | local (poll loop + subprocess supervision, "
            "harness-agnostic default) | temporal (Temporal workers, fleet-scale). Mirrors the "
            "`beads.engine` seam in engine.py: a config key selects a thin implementation, not "
            "a plugin framework. See docs/WORK.md#runtime-tiers and "
            "docs/design/work-runtime-tiers-adr.md."
        ),
    )
    landing: Literal["local", "pr"] = Field(
        "local",
        description=(
            "How merge/finish land on the shared integration branch: local (--no-ff merge) | "
            "pr (push + `gh pr create`; gh:pr gate + `bh work land` complete the close)."
        ),
    )
    push_remote: str = Field(
        "origin", description="Remote branch pushes target (submit gh:* publish + landing: pr)."
    )
    integration_branch: str = Field(
        "main", description="Base the bead branch merges back to / is measured against."
    )
    max_commits: int = Field(
        10, description="submit rejects a branch with more commits than this over the base."
    )
    enforce_signing: bool = Field(
        False,
        description=(
            "When true, merge refuses a branch unless EVERY commit in the merge range carries a "
            "signature git verifies as TRUSTED (`git log --format=%G?` = G), names the offending "
            "commits, and does not merge. No grandfathering: commits made before the flag was "
            "turned on are refused like any other — re-sign them (rebase) or leave the flag off. "
            "Default false because turning it on for an unprepared fleet would block every "
            "merge. Verification needs `gpg.ssh.allowedsignersfile` pointing at a real file — bh "
            "keeps one in the HQ store and `bh host identity` enrolls this host + wires it up. "
            "WITHOUT a usable one git reports even a perfectly-signed commit as N (key unset) "
            "or U (file missing) and never G — measured, git 2.54 — so the gate refuses "
            "everything until that file is real."
        ),
    )
    precious_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PRECIOUS_GLOBS),
        description="Local-only paths protected regardless of size (per-hive replaces global).",
    )
    junk_globs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_JUNK_GLOBS),
        description=(
            "Disposable paths excluded before stat/walk (per-hive replaces global); bd's live "
            "and rotated backup stores remain excluded regardless of overrides."
        ),
    )
    precious_min_bytes: int = Field(
        DEFAULT_PRECIOUS_MIN_BYTES,
        ge=0,
        description="Review threshold in bytes for ignored/untracked paths outside the taxonomy.",
    )
    batch_max_size: int = Field(
        5, description="Max issues a planner-declared batch:<group> may hold as one unit."
    )
    ledger_ttl: str = Field(
        "P1D",
        description=(
            "How long a recorded validation verdict stays reusable, as an ISO-8601 duration "
            "(PT30M | PT4H | P1D). The verdict ledger keys on (tree hash, validate-cmd hash), so "
            "an aging entry is a claim about bytes that are still identical but were last proved "
            "green a while ago. Default P1D — exactly the 24h shipped since bh-dfx0. The "
            "realistic reuse window is minutes-to-hours: tune this DOWN, not up."
        ),
    )

    @field_validator("validate_subset")
    @classmethod
    def _subset_placeholder(cls, v):
        """A template bh cannot fill is not a template. Loud here (and at `bh config set`); the
        READ path (`converge.template`) degrades to absent instead, so tier 2 fails open to the
        full run rather than failing a validation on a typo."""
        if v and SUBSET_PLACEHOLDER not in v:
            raise ValueError(
                f"work.validate_subset must contain the {SUBSET_PLACEHOLDER} placeholder "
                f"(where the failing test names go): {v!r}"
            )
        return v

    @field_validator("validation_artifact_root")
    @classmethod
    def _artifact_root_is_absolute(cls, v):
        if v and not Path(v).expanduser().is_absolute():
            raise ValueError("validation_artifact_root must be an absolute path")
        return v

    @field_validator("ledger_ttl")
    @classmethod
    def _iso8601_duration(cls, v):
        """Catch a typo here, where it is one loud error at validation time. `config.ledger_ttl`
        deliberately degrades to the default instead, so a hand-edited bad value can never fail
        an unrelated `bh` command.

        A NEGATIVE duration (`-P1D`) is a well-formed ISO-8601 duration that pydantic's own
        `timedelta` adapter happily parses (bh-ku9n9.19, item 3) — but `config.duration_seconds`
        would then hand the ledger a negative TTL, which makes `_is_fresh` false for every entry.
        Fail-safe (nothing is ever wrongly reused), but silent: the operator gets zero reuse and
        no signal why. Reject it here, loudly, instead."""
        try:
            parsed = _TIMEDELTA.validate_python(v)
        except ValidationError as exc:
            raise ValueError(f"not an ISO-8601 duration (want PT30M / PT4H / P1D): {v!r}") from exc
        if parsed.total_seconds() < 0:
            raise ValueError(f"ledger_ttl must not be negative (want PT30M / PT4H / P1D): {v!r}")
        return v

    attest: AttestConfig = Field(
        default_factory=AttestConfig,
        description=(
            "Attest key catalog + impact resolver (Attested Green ADR, Amendment 1). Absent is "
            "today's whole-tree gate: no keys, native-full."
        ),
    )
    gate: GateConfig = Field(
        default_factory=GateConfig,
        description=(
            "Gate-model settings (attest-keys ADR): high-water carry discovery and the opt-in "
            "push-hook acceptance."
        ),
    )
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    beads: BeadsConfig = Field(default_factory=BeadsConfig)
    identity: IdentityConfig | None = Field(
        None,
        description="Agent identity profile; omit (or mode: supervised) to inherit git config.",
    )
    conflict: ConflictConfig = Field(default_factory=ConflictConfig)


# ---- hq (Factory HQ remote, bh-e0y8.1) ----------------------------------------


class HqFnoxRef(_Section):
    """Host-local fnox lookup; the value itself is never configuration."""

    config_path: str = ""
    profile: str = ""
    key: str = ""

    @model_validator(mode="after")
    def valid_ref(self):
        if self.config_path and not Path(self.config_path).is_absolute():
            raise ValueError("fnox config path must be absolute")
        if self.profile and not re.fullmatch(r"[A-Za-z0-9_-]+", self.profile):
            raise ValueError("invalid fnox profile")
        if self.key and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.key):
            raise ValueError("invalid fnox key")
        return self


class HqSqlConnection(_Section):
    """One scoped SQL principal with an explicit host-local transport policy."""

    host: str = ""
    port: int = Field(3308, ge=1, le=65535, strict=True)
    database: str = ""
    user: str = ""
    tls_mode: Literal["required", "disabled"] = "required"
    server_name: str = ""
    ca_file: str = ""
    credential: HqFnoxRef = Field(default_factory=HqFnoxRef)
    connect_timeout: int = Field(5, ge=1, le=30, strict=True)
    read_timeout: int = Field(5, ge=1, le=30, strict=True)
    write_timeout: int = Field(5, ge=1, le=30, strict=True)
    operation_timeout: int = Field(15, ge=1, le=60, strict=True)

    @model_validator(mode="after")
    def valid_binding(self):
        if self.host and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", self.host):
            raise ValueError("invalid SQL endpoint hostname")
        if self.database and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.database):
            raise ValueError("invalid SQL database identifier")
        if self.user and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", self.user):
            raise ValueError("invalid SQL principal name")
        if self.ca_file and not Path(self.ca_file).is_absolute():
            raise ValueError("SQL CA path must be absolute")
        if self.server_name and self.server_name != self.host:
            raise ValueError("SQL server name must match endpoint hostname")
        return self


class HqSqlConfig(_Section):
    """Dedicated config service and optional, separately credentialed runtime."""

    enabled: StrictBool = False
    reader: HqSqlConnection = Field(default_factory=HqSqlConnection)
    publisher: HqSqlConnection | None = None
    runtime: HqSqlConnection | None = None
    observer: HqSqlConnection | None = None
    authority_writer: HqSqlConnection | None = None
    runtime_backend_identity: str = ""
    runtime_generation: str = ""
    runtime_initial_revision: str = ""
    runtime_floor_path: str = ""
    runtime_operator_public_key: str = ""
    cache_ttl: int = Field(30, ge=1, le=300, strict=True)
    floor_path: str = ""
    backend_identity: str = ""
    generation: str = ""
    minimum_sequence: int = Field(1, ge=1, strict=True)
    initial_revision: str = ""
    liveness: Literal["receiver", "signed"] = Field(
        "receiver",
        description=(
            "Frame liveness source for this HOST. `receiver` (default) trusts the "
            "SqlTrustedReceiver's public observation and renews the hive lease through it. "
            "`signed` verifies this frame's newest signed heartbeat from its own inbox at "
            "read time, treats hive-lease expiry as an advisory failover hint, and makes no "
            "receiver round trip on the dev path. Adopt, release and failover still need a "
            "receiver. BH_HQ_SQL_LIVENESS, when set, overrides this value."
        ),
    )

    @model_validator(mode="after")
    def valid_bootstrap(self):
        if self.runtime is not None and (
            self.observer is not None or self.authority_writer is not None
        ):
            raise ValueError("frame runtime binding cannot carry observer/operator credentials")
        if self.floor_path and not Path(self.floor_path).is_absolute():
            raise ValueError("SQL floor path must be absolute")
        if self.enabled and not all(
            (
                self.reader.host,
                self.reader.database,
                self.reader.user,
                self.reader.tls_mode == "disabled"
                or (self.reader.ca_file and self.reader.server_name),
                self.reader.credential.config_path,
                self.reader.credential.profile,
                self.reader.credential.key,
                self.floor_path,
                self.backend_identity,
                self.generation,
                self.initial_revision,
            )
        ):
            raise ValueError("enabled SQL bootstrap requires explicit trust and reader binding")
        if self.initial_revision and not re.fullmatch(r"[0-9a-v]{32}", self.initial_revision):
            raise ValueError("invalid initial Dolt revision")
        if self.runtime is not None and not all(
            (
                self.runtime.host,
                self.runtime.database,
                self.runtime.user,
                self.runtime.tls_mode == "disabled"
                or (self.runtime.ca_file and self.runtime.server_name),
                self.runtime.credential.config_path,
                self.runtime.credential.profile,
                self.runtime.credential.key,
                self.runtime_backend_identity,
                self.runtime_generation,
                self.runtime_initial_revision,
                self.runtime_floor_path,
                self.runtime_operator_public_key,
            )
        ):
            raise ValueError("runtime binding requires separate explicit authority trust")
        if self.runtime_initial_revision and not re.fullmatch(
            r"[0-9a-v]{32}", self.runtime_initial_revision
        ):
            raise ValueError("invalid initial runtime Dolt revision")
        if self.runtime_floor_path and not Path(self.runtime_floor_path).is_absolute():
            raise ValueError("runtime floor path must be absolute")
        if self.runtime_operator_public_key and not self.runtime_operator_public_key.startswith(
            "ssh-ed25519 "
        ):
            raise ValueError("runtime operator key must be an Ed25519 public key")
        return self


class HqReleaseCatalogConfig(_Section):
    """Signed release catalog automation (``hq.release_catalog``, bh-vfrem.4). HOST-local: it
    names the host whose daemon runs the importer. The catalog credential itself is
    ``hq.sql.release_catalog`` in the operator settings file, never in host.yaml."""

    auto_import: StrictBool = Field(
        False,
        description=(
            "Run a host-daemon timer that imports each new PyPI release into the signed release "
            "catalog (`bh hq release-catalog import`, same verification, INSERT-only "
            "credential). Off by default. Needs dolt-server HQ, the catalog writer settings file "
            "and `beadhive[release-verify]`; it never imports an older or pre-release version, "
            "and does nothing until the first entry has been imported by hand."
        ),
    )
    interval_seconds: float = Field(
        3600.0,
        gt=0,
        le=86_400,
        description="Seconds between PyPI listings while auto_import is on.",
    )
    operator_settings: str = Field(
        "",
        description=(
            "Absolute path of the operator settings file carrying hq.sql.release_catalog. When "
            "empty, BH_HQ_OPERATOR_SETTINGS in the daemon's environment is used."
        ),
    )
    trusted_root: str = Field(
        "",
        description=(
            "Absolute path of a pinned Sigstore trusted_root.json for offline verification. "
            "When empty the TUF root is refreshed online."
        ),
    )

    @model_validator(mode="after")
    def _absolute_paths(self):
        for name in ("operator_settings", "trusted_root"):
            value = getattr(self, name)
            if value and not Path(value).is_absolute():
                raise ValueError(f"release_catalog {name} must be an absolute path")
        return self


class HqConfig(_Section):
    """Factory HQ remote (``hq``) — where the factory's central HQ store publishes to."""

    mode: Literal["git", "dolt-server"] = "git"
    beadyard_id: str | None = Field(
        None,
        description=(
            "HOST-local pin copied from canonical HQ beadyard.json at explicit frame bootstrap; "
            "never generated by config reads or stored as a second SQL column."
        ),
    )

    @field_validator("beadyard_id")
    @classmethod
    def _canonical_beadyard_id(cls, value):
        if value is not None:
            from .domain.beadyard_identity import parse_id

            return parse_id(value)
        return value

    authority_anchor: str = ""
    admission_policy: Literal["manual"] = "manual"
    authority_mode: Literal["signed", "trusted", "inherit"] = Field(
        "inherit",
        description=(
            "Frame-local HQ authority mode (HOST only, never fleet). `signed` verifies "
            "authority, heartbeat and config signatures; `trusted` is an opt-in waiver; "
            "`inherit` (default) follows the fleet default, which resolves to `signed` until "
            "one is set. BH_HQ_AUTHORITY_MODE, when set, overrides this value."
        ),
    )
    default_authority_mode: Literal["signed", "trusted"] = Field(
        "signed",
        description=(
            "FLEET default HQ authority mode for frames whose hq.authority_mode is `inherit` "
            "(the default). Set it with `bh hq authority mode-signed|mode-trusted`, never by "
            "hand: raising it to `trusted` takes effect on a signed frame only once the "
            "operator re-signs authority against the new config head (SQL) or signs the "
            "config commit (Git); lowering it to `signed` always takes effect. A trusted "
            "fleet also admits a registering frame without an operator grant (open "
            "admission). Upgrade every frame to 0.24.0 before setting it: 0.23.x rejects the "
            "key."
        ),
    )
    sql: HqSqlConfig = Field(default_factory=HqSqlConfig)
    release_catalog: HqReleaseCatalogConfig = Field(default_factory=HqReleaseCatalogConfig)

    remote: str = Field(
        "",
        description=(
            "HQ repo remote: `<owner>/beadhive-hq`, an absolute local path, "
            "or local `file://` URL. "
            "Empty (default) derives `<owner>` "
            "from the logged-in `gh` identity at read time (`config.hq_remote`) — host "
            "identity, so the answer does not vary by which hive you invoke from; an "
            "explicit value here always overrides the derivation. `bh hq init`/`clone` "
            "confirm the result interactively unless `--auto` is passed."
        ),
    )


# ---- host (multi-host primary / host lease, bh-ytbb.6) ------------------------


class HostLeaseConfig(_Section):
    """**Host-lease** timing (``host.lease``) — the TTL/renewal contract behind
    ``refs/bh/lease/<prefix>`` in HQ (``docs/design/multi-host-model-adr.md``, Amendment 1
    §3; implemented in :mod:`beadhive.host_lease`).

    "Host lease" is host ↔ hive, deliberately distinct from ``bd``'s *worker* lease
    (worker ↔ issue: ``lease_expires_at``, ``bd heartbeat`` / ``bd reclaim``) — Amendment 1
    §5. Nothing here configures bd's.

    Both keys are **fleet-scoped** (``config_partition.FLEET_PREFIXES``) on purpose: expiry is
    a shared judgement. If two hosts disagreed about when a lease lapses, one would consider a
    hive free while the other still considered itself primary — the exact split-brain the
    fence exists to prevent. Per-host variation is expressed through the host's ``role``
    (``hosts/<host_id>.yaml``), which SCALES this baseline
    (:func:`beadhive.host_lease.ttl_for_role`) — never by per-host overrides of these keys.
    """

    evict_after_s: float = Field(900.0, gt=0, allow_inf_nan=False)

    renew_interval: float = Field(
        300.0,
        description=(
            "Seconds between host-lease renewals while a dispatcher has active workers (no "
            "daemon, no cron). ADR Amendment 1 §3 default: 300 (5 min). Must stay comfortably "
            "below `host.lease.ttl`."
        ),
    )
    ttl: float = Field(
        1800.0,
        description=(
            "Seconds a host lease stays valid without renewal; past it another host may take "
            "the hive. ADR Amendment 1 §3 default: 1800 (30 min) — the `transient` "
            "baseline. An `executor` host scales it up for long stable tenure; "
            "a `viewer` never adopts at all."
        ),
    )


class HostDispatchConfig(_Section):
    """Unattended-dispatch supervision (``host.dispatch``, bh-e7r9q.4/.5) — the backend
    selection for ``bh host dispatch run --hive <hive>`` and the dumb picker it drives.

    Per-HOST, not fleet-scoped like ``host.lease``: which supervisor exists (systemd vs
    launchd vs a container's own restart policy) is a fact about THIS machine, not a shared
    fleet judgement."""

    enabled: bool = Field(True, description="Allow frame intake; legacy hosts retain lease policy.")

    backend: str = Field(
        "systemd",
        description=(
            "Dispatch supervision: 'systemd' installs and persists per-hive systemd --user "
            "units (the default). 'process' runs bh host dispatch run in the foreground "
            "under an external supervisor, which owns restart and status. 'container' and "
            "'launchd' select that same process runtime; they do not install services. "
            "Stop the foreground process with SIGTERM for bounded draining."
        ),
    )
    max_epics_in_flight: int = Field(
        3,
        description=(
            "How many `bh work loop <epic>` child processes the hive-level picker "
            "(`bh host dispatch run`) runs at once. Deliberately dumb: kicked-off epics in "
            "`bd ready` order, bounded by this cap — NO cross-hive arbitration, NO budget "
            "reasoning (both belong to the director loop at the second-hive trigger, "
            "operator decision 2026-08-10). This is NOT the per-epic seat concurrency cap "
            "(`work.dispatch.max_concurrency`), which bounds seats WITHIN one epic's loop."
        ),
    )
    poll_interval: float = Field(
        10.0,
        description=(
            "Seconds the hive-level picker sleeps between passes: lease check, reap finished "
            "`bh work loop` children, pick the next kicked-off ready epic if there is room."
        ),
    )
    stale_after_seconds: float = Field(
        900.0,
        description=(
            "`bh doctor`'s dispatch section (bh-e7r9q.6) flags a RUNNING loop as stalled when "
            "no pass has been recorded in this many seconds (default 900 = 15 min — well over "
            "the default `poll_interval`, so this is a genuine stall signal, not poll jitter)."
        ),
    )


class HostForwardServeConfig(_Section):
    """This frame's hive server takes forwarders (``host.forward.serve``, bh-g7dlo; ADR §3
    condition 16). Opt-in per frame: off, ``bh doctor`` reports nothing about forwarding here.
    The forwarder-session kill before a divert reset runs whatever ``enabled`` says (it only
    ever kills non-operator logins), unless ``quiesce_before_reset`` is false."""

    enabled: StrictBool = Field(
        False,
        description=(
            "This frame serves forwarders: `bh doctor` reports the globals watchdog list, the "
            "read-only DOLT_ROOT_PATH and forwarder grant conformance for its server-mode hives."
        ),
    )
    operators: list[str] = Field(
        default_factory=lambda: ["root", "watchdog"],
        description=(
            "Logins on the hive server that are NOT forwarders (bd's own, the watchdog's). Every "
            "other account must hold the table-scoped forwarder shape; their sessions are killed "
            "before a divert reset. Dolt's built-in accounts are always operators."
        ),
    )
    require_tls: StrictBool = Field(
        True, description="Forwarder accounts are created, and must stay, REQUIRE SSL."
    )
    quiesce_before_reset: StrictBool = Field(
        True,
        description=(
            "Kill every forwarder session before a demoted primary's DOLT_RESET --hard, so an "
            "in-flight forwarded write is refused rather than acknowledged and dropped (M13 E5). "
            "BH_FORWARD_QUIESCE=off also disables it."
        ),
    )
    watched: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Overrides or additions to the watched globals (default: dolt_force_transaction_"
            "commit=0, dolt_transaction_commit=0, read_only=0, max_connections=100)."
        ),
    )
    unwatched: list[str] = Field(
        default_factory=list, description="Watched globals to drop (a deliberate policy choice)."
    )
    root_path: str = Field(
        "",
        description=(
            "The hive server's DOLT_ROOT_PATH, checked read-only by `bh doctor` (empty: reported "
            "as not checked)."
        ),
    )

    @model_validator(mode="after")
    def valid_serve(self):
        if self.root_path and not Path(self.root_path).is_absolute():
            raise ValueError("forward serve root_path must be absolute")
        for name in (*self.watched, *self.unwatched):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
                raise ValueError(f"invalid watched global name {name!r}")
        for login in self.operators:
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}", login):
                raise ValueError(f"invalid operator login {login!r}")
        return self


class HostForwardConfig(_Section):
    """The forward write path, option A (``host.forward``, bh-g7dlo; ADR §3, condition 16).

    Opt-in per frame. With ``enabled``, `bh hive forward point <hive>` points this frame's bd for
    a hive at the current primary's hive server (``endpoints[<primary frame>]``) with this frame's
    own host-pinned TLS login, re-points it when placement moves, and refuses (fails closed) when
    the placed primary is not the writer."""

    enabled: StrictBool = Field(
        False,
        description="This frame forwards bd writes (claim, create, close) to the hive's primary.",
    )
    endpoints: dict[str, HqSqlConnection] = Field(
        default_factory=dict,
        description=(
            "Primary frame id -> its hive server binding (host, port, database, this frame's "
            "user, tls_mode, server_name, ca_file, fnox credential)."
        ),
    )
    serve: HostForwardServeConfig = Field(default_factory=HostForwardServeConfig)

    @model_validator(mode="after")
    def valid_endpoints(self):
        for frame, endpoint in self.endpoints.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", frame):
                raise ValueError(f"invalid forward endpoint frame id {frame!r}")
            if endpoint.tls_mode == "required" and not (endpoint.ca_file and endpoint.server_name):
                raise ValueError(f"forward endpoint {frame!r} requires ca_file and server_name")
        return self


class FrameBridgeIdentityConfig(_Section):
    """Enrollment-owned identity of the private Factory bridge."""

    host_id: str | None = None
    instance_id: str | None = None
    factory_id: str | None = None
    primary_hive_id: str | None = None


class HostConfig(_Section):
    """Multi-host model policy (``host``) — how this factory arbitrates who may write a hive.

    Distinct from ``hosts/<host_id>.yaml`` (the per-host manifest in HQ,
    :mod:`beadhive.hosts`): that file describes ONE machine; this section is the fleet-wide
    policy every machine applies."""

    frame_id: str = Field(
        "",
        description="Enrollment-owned Fleet identity; config storage does not enroll a host.",
    )
    frame_bridge: FrameBridgeIdentityConfig = Field(default_factory=FrameBridgeIdentityConfig)
    lease: HostLeaseConfig = Field(default_factory=HostLeaseConfig)
    dispatch: HostDispatchConfig = Field(default_factory=HostDispatchConfig)
    daemon: HostDaemonConfig = Field(
        default_factory=HostDaemonConfig,
        description="Unified host daemon listener, security, transport, and resource limits.",
    )
    forward: HostForwardConfig = Field(
        default_factory=HostForwardConfig,
        description="Forward write path to a hive's primary (opt-in per frame; bh-g7dlo).",
    )


# ---- release (release-order planning, bh-k2j8) --------------------------------


class ReleaseConfig(_Section):
    """Release-order planning policy (``release``) — advisory: consulted by the
    dispatcher's start-verdict and the merger's merge-order, never obeyed blindly.
    Unset (all default) falls back to today's FCFS behavior."""

    strategy: str = Field(
        "stable-versioning",
        description=(
            "Named release strategy the scorer registry resolves (release_order.py); "
            "only stable-versioning ships today."
        ),
    )
    enforce_hold: bool = Field(
        False,
        description=(
            "When true, a release:breaking bead gets a release-hold: gate filed at "
            "planning time — a hard block, not just advisory ordering."
        ),
    )
    fix_churn_budget: int = Field(
        3,
        description=(
            "Max release:fix beads flushed ahead of features in the current patch window "
            "before further fixes yield to additive work."
        ),
    )
    conflict_estimator: str = Field(
        "file-overlap",
        description=(
            "Named ConflictEstimator the start-verdict path consults; file-overlap is the "
            "bundled floor implementation."
        ),
    )
    channel_stale_days: int = Field(
        14,
        description=(
            "Days the oldest unpromoted release may sit before `bh doctor` reports `stable` as "
            "stale; 0 disables. Default sits just above the widest gap between two consecutive "
            "beadhive releases (9.68 d over v0.1.0..v0.8.4) so it cannot fire on a quiet period. "
            "Reporting only — doctor never gates."
        ),
    )
    channel_stale_releases: int = Field(
        0,
        description=(
            "Releases `stable` may trail `latest` by before `bh doctor` reports it as stale. "
            "Default 0 (off): at beadhive's cadence three releases can land in 0.1 days, so a "
            "count threshold fires on every ordinary patch burst. Set >0 for a slow-cadence hive."
        ),
    )


# ---- otel / observaloop -------------------------------------------------------


class GenaiConfig(_Section):
    """EXPERIMENTAL agentic GenAI span attributes (``otel.genai``)."""

    model: str = Field("", description="gen_ai.request.model for dispatcher->developer spans.")
    system: str = Field(
        "",
        description=(
            "gen_ai.system (the harness) for dispatch spans. Empty (default) defers to "
            "`harness_name()` at runtime, so a non-default harness attributes correctly."
        ),
    )


class OtelConfig(_Section):
    """OpenTelemetry — enable + point at a collector. Disabled by default."""

    enabled: bool = Field(False, description="Whether to initialize the OTel SDK.")
    endpoint: str = Field(
        "", description="OTLP collector endpoint (OTEL_EXPORTER_OTLP_ENDPOINT env wins)."
    )
    protocol: Literal["grpc", "http/protobuf"] = Field(
        "grpc", description="OTLP transport selecting the exporter class for every signal."
    )
    hive: str = Field("", description="Hive name stamped onto the Resource (`bh.hive`).")
    headers: dict[str, str] = Field(
        default_factory=dict, description="Headers threaded into every OTLP exporter constructor."
    )
    metrics_temporality: Literal["delta", "cumulative"] = Field(
        "delta", description="Preferred OTLP metric temporality."
    )
    export_timeout_seconds: float = Field(
        0.5,
        gt=0,
        le=30,
        description="Finite timeout for one OTLP exporter request.",
    )
    flush_timeout_seconds: float = Field(
        2.0,
        ge=0,
        le=30,
        description="Finite total process-exit telemetry flush budget.",
    )
    role: str = Field(
        "",
        description="`bh.role` stamped onto the Resource (the seat this process runs as).",
    )
    genai: GenaiConfig = Field(default_factory=GenaiConfig)


class ObservaloopConfig(_Section):
    """Observaloop integration — telemetry routing + per-hive profile management. Requires
    otel.enabled: true; no-op otherwise."""

    enabled: bool = Field(False, description="Enable observaloop routing (requires otel.enabled).")
    command: str = Field("observaloop", description="Override the observaloop CLI command.")
    per_worktree_container: bool = Field(
        False, description="Reserved for Mode-2 (one container per worktree); not yet active."
    )
    profile: str = Field(
        "",
        description="Observaloop profile stamped onto the Resource (BH_OBSERVALOOP_PROFILE wins).",
    )


# ---- dolt remote descriptor (bh-b1scz, docs/design/hq-dolt-remote-descriptor-adr.md) ----

#: The stable contract id other molecules cite (bh-lniwf nests the same model per store).
DOLT_REMOTE_DESCRIPTOR_CONTRACT = "dolt-remote-descriptor/v1"
DOLT_REMOTE_DEFAULT_NAME = "origin"
#: The only query keys an ``s3://`` URL may carry: Dolt's provider-addressing parameters.
DOLT_REMOTE_S3_QUERY_KEYS = frozenset({"endpoint", "region", "path-style"})
#: Schemes whose URL may carry a (password-less) login name: Git SSH routes by user.
DOLT_REMOTE_USER_SCHEMES = frozenset({"git+ssh", "ssh"})
#: Families that address one database by a storage location (bucket path or directory).
DOLT_REMOTE_LOCATION_SCHEMES = frozenset({"s3", "aws", "gs", "file", "az", "oss"})
_DOLT_REMOTE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,62}")
_DOLT_REMOTE_LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_DOLT_REMOTE_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://(.*)", re.DOTALL)
_DOLT_REMOTE_SECRET_KEY = re.compile(
    r"(key|secret|token|passw|credential|signature|sig|auth|session)", re.IGNORECASE
)


@dataclass(frozen=True)
class DoltRemoteUrlParts:
    """A Dolt remote URL split without ``urllib``'s IPv6 guess (``aws://[table:bucket]/db``)."""

    scheme: str
    userinfo: str
    host: str
    path: str
    query: tuple[tuple[str, str], ...]
    fragment: str


def _dolt_remote_error(code: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(code, message)


def split_dolt_remote_url(url: str) -> DoltRemoteUrlParts:
    """Split *url* into parts, raising a value-free ``PydanticCustomError`` when malformed."""

    if not isinstance(url, str) or not url.strip():
        raise _dolt_remote_error("dolt_remote_url_required", "Dolt remote URL is required")
    if url != url.strip() or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise _dolt_remote_error(
            "dolt_remote_url_malformed", "Dolt remote URL contains whitespace or control bytes"
        )
    match = _DOLT_REMOTE_SCHEME.fullmatch(url)
    if match is None:
        raise _dolt_remote_error(
            "dolt_remote_url_malformed", "Dolt remote URL needs an explicit scheme://"
        )
    scheme, rest = match.group(1).lower(), match.group(2)
    rest, hash_mark, fragment = rest.partition("#")
    if hash_mark:
        raise _dolt_remote_error(
            "dolt_remote_url_malformed", "Dolt remote URL may not carry a #fragment"
        )
    rest, _, query_text = rest.partition("?")
    authority, slash, path = rest.partition("/")
    path = slash + path
    userinfo, at, host = authority.rpartition("@")
    if not at:
        userinfo, host = "", authority
    query: list[tuple[str, str]] = []
    if query_text:
        for item in query_text.split("&"):
            key, _, value = item.partition("=")
            if not key:
                raise _dolt_remote_error(
                    "dolt_remote_url_malformed", "Dolt remote URL has an empty query key"
                )
            query.append((key, value))
    return DoltRemoteUrlParts(scheme, userinfo, host, path, tuple(query), fragment)


def validate_dolt_remote_url(url: str) -> DoltRemoteUrlParts:
    """Reject secrets and malformed shapes; accept unknown schemes (classified at runtime)."""

    parts = split_dolt_remote_url(url)
    if parts.userinfo and (":" in parts.userinfo or parts.scheme not in DOLT_REMOTE_USER_SCHEMES):
        raise _dolt_remote_error(
            "dolt_remote_embedded_credential",
            "Dolt remote URL may not embed credentials; configure them on the host",
        )
    for key, _value in parts.query:
        if _DOLT_REMOTE_SECRET_KEY.search(key) and key.lower() not in DOLT_REMOTE_S3_QUERY_KEYS:
            raise _dolt_remote_error(
                "dolt_remote_embedded_credential",
                "Dolt remote URL may not embed credentials; configure them on the host",
            )
    if parts.query and parts.scheme != "s3":
        raise _dolt_remote_error(
            "dolt_remote_query_unsupported", "only s3:// Dolt remote URLs take query parameters"
        )
    unknown = [key for key, _ in parts.query if key.lower() not in DOLT_REMOTE_S3_QUERY_KEYS]
    if unknown:
        raise _dolt_remote_error(
            "dolt_remote_query_unsupported",
            "s3:// query keys are limited to endpoint, region and path-style",
        )
    if parts.scheme in ("file", "git+file"):
        # `git+file:///abs/repo.git` is a local Git repository carrying refs/dolt/data — the
        # form bd itself registers for a path origin — so, like file://, it has no host.
        if parts.host not in ("", "localhost") or not parts.path.startswith("/"):
            raise _dolt_remote_error(
                "dolt_remote_url_malformed", f"{parts.scheme}:// Dolt remote needs an absolute path"
            )
    elif parts.scheme in DOLT_REMOTE_LOCATION_SCHEMES:
        if not parts.host or parts.path.strip("/") == "":
            raise _dolt_remote_error(
                "dolt_remote_url_malformed",
                "object-store Dolt remote needs scheme://bucket/database-path",
            )
    elif not parts.host:
        raise _dolt_remote_error("dolt_remote_url_malformed", "Dolt remote URL needs a host")
    return parts


def dolt_remote_location(url: str) -> str | None:
    """Normalised storage location of an object-store/file URL, else ``None``.

    Two managed hives with the same location would share one Dolt history (ADR, "One location
    per database"); a different S3 ``endpoint`` is a different location, so the query stays.
    """

    try:
        parts = split_dolt_remote_url(url)
    except PydanticCustomError:
        return None
    if parts.scheme not in DOLT_REMOTE_LOCATION_SCHEMES:
        return None
    path = parts.path.rstrip("/")
    if parts.scheme == "file":
        return f"file://{os.path.normpath(path or '/')}"
    query = "&".join(f"{k.lower()}={v}" for k, v in sorted(parts.query))
    location = f"{parts.scheme}://{parts.host.lower()}{path}"
    return f"{location}?{query}" if query else location


class DoltRemoteDescriptor(_Section):
    """``dolt-remote-descriptor/v1``: the non-secret desired Dolt remote of one hive.

    Fleet-owned (``managed_repos[].dolt_remote``). Absent means today's behaviour.
    Credentials never live here — ``credentials`` only names a host-local profile.
    """

    url: str = Field(description="Dolt remote URL (s3://bucket/path, file:///abs, git+ssh://…).")
    name: str = Field(
        DOLT_REMOTE_DEFAULT_NAME, description="Canonical Dolt remote name to register it under."
    )
    credentials: str = Field(
        "",
        description="Host credential profile label (dolt.remote_credentials.<label>); "
        "empty = ambient AWS SDK chain.",
    )

    @field_validator("url", mode="before")
    @classmethod
    def _valid_url(cls, value):
        if not isinstance(value, str):
            raise _dolt_remote_error("dolt_remote_url_required", "Dolt remote URL is required")
        validate_dolt_remote_url(value)
        return value

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value):
        if not _DOLT_REMOTE_NAME.fullmatch(value):
            raise _dolt_remote_error("dolt_remote_name_invalid", "invalid Dolt remote name")
        return value

    @field_validator("credentials")
    @classmethod
    def _valid_label(cls, value):
        if value and not _DOLT_REMOTE_LABEL.fullmatch(value):
            raise _dolt_remote_error(
                "dolt_remote_credentials_label_invalid", "invalid credential profile label"
            )
        return value


class DoltRemoteCredentialConfig(_Section):
    """HOST-local credential source for one ``credentials`` label. Never holds a secret value:
    ``fnox`` entries are lookups resolved through the host's fnox broker at call time."""

    source: Literal["ambient", "instance-role", "aws-profile", "fnox"] = Field(
        "ambient",
        description="ambient = AWS SDK chain; instance-role = trust instance metadata; "
        "aws-profile = set AWS_PROFILE; fnox = read keys through the fnox broker.",
    )
    aws_profile: str = Field("", description="AWS profile name for source=aws-profile.")
    access_key_id: HqFnoxRef = Field(default_factory=HqFnoxRef)
    secret_access_key: HqFnoxRef = Field(default_factory=HqFnoxRef)
    session_token: HqFnoxRef = Field(default_factory=HqFnoxRef)
    s3_checksums: Literal["", "when_required"] = Field(
        "",
        description="Opt-in relaxed AWS SDK checksum handling for this profile (bh-81gpj). "
        "when_required sets AWS_RESPONSE_CHECKSUM_VALIDATION and "
        "AWS_REQUEST_CHECKSUM_CALCULATION to when_required in every dolt/bd env using the "
        "profile and in the rendered server credentials file. Needed for Garage multipart "
        "objects. Empty (default) = SDK default.",
    )

    @model_validator(mode="after")
    def _complete_source(self):
        if self.aws_profile and not re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", self.aws_profile):
            raise ValueError("invalid AWS profile name")
        if self.source == "aws-profile" and not self.aws_profile:
            raise ValueError("source aws-profile needs aws_profile")
        if self.source == "fnox":
            for ref in (self.access_key_id, self.secret_access_key):
                if not (ref.config_path and ref.profile and ref.key):
                    raise ValueError("source fnox needs access_key_id and secret_access_key refs")
        return self


# ---- dolt / dimensions / passthrough / log / archive / metadata --------------


class DoltConfig(_Section):
    """Optional Dolt SQL server backend (bh's own docker-compose-managed one — `dolt.py` /
    docs/DOLT.md — NOT bd's shared `dolt sql-server`, which is a different subsystem).

    Deliberately still ONE field after `bh-areg.3` (store-engine liveness reporting): bd
    already owns its own server's mode/endpoint declaration (`shared-server: true` /
    `BEADS_DOLT_SHARED_SERVER=1`, `BEADS_DOLT_SERVER_HOST`/`_PORT`, and the persisted
    `dolt_mode` in `.beads/metadata.json`) — a bh-side mirror here would be a second place to
    declare the same fact, with no mechanism keeping the two in sync. See
    docs/design/dolt-store-engine-liveness-adr.md for the detection-over-configuration
    reasoning; `dolt_health.py` reads bd's own signals directly instead of adding keys here.
    """

    backend: Literal["colima", "docker", "podman", "none"] = Field(
        "docker",
        description="Container runtime pre-step (none = no probe, external Dolt server).",
    )
    remote_credentials: dict[str, DoltRemoteCredentialConfig] = Field(
        default_factory=dict,
        description="HOST-local credential profiles for HQ Dolt remote descriptors, keyed by "
        "the descriptor's `credentials` label (bh-b1scz). Lookups only, never secret values.",
    )

    server_credentials: str = Field(
        "",
        description="Label of a dolt.remote_credentials profile whose resolved AWS environment "
        "bh injects into the shared dolt sql-server when bh starts it (bh-pphik). In server "
        "mode the sql-server, not the bd client, performs the s3:// transfer and only sees "
        "its own start-up environment; one credential per host. Empty (default) = bh adds "
        "nothing: the server inherits whatever its parent environment carries.",
    )

    @field_validator("server_credentials")
    @classmethod
    def _valid_server_label(cls, value):
        if value and not _DOLT_REMOTE_LABEL.fullmatch(value):
            raise _dolt_remote_error(
                "dolt_remote_credentials_label_invalid", "invalid credential profile label"
            )
        return value

    @field_validator("remote_credentials")
    @classmethod
    def _valid_labels(cls, value):
        for label in value:
            if not _DOLT_REMOTE_LABEL.fullmatch(label):
                raise _dolt_remote_error(
                    "dolt_remote_credentials_label_invalid", "invalid credential profile label"
                )
        return value


class DimensionConfig(_Section):
    """One non-identity label dimension (``dimensions.<name>``)."""

    description: str = Field("", description="Human-readable description of this dimension.")
    values: list[str] | None = Field(
        None,
        description=(
            "Closed set of valid values; omit for an open set (any value accepted); "
            "[] locks the dimension (closed but reserved)."
        ),
    )


class ExcludeConfig(_Section):
    """git-workspace repos bh ignores."""

    orgs: list[str] = Field(
        default_factory=list, description="Org names label sync/hive init skip."
    )
    repos: list[str] = Field(
        default_factory=list,
        description='Repos ("<group>/<org>/<repo>") label sync/hive init skip.',
    )


class OrgEntry(_Section):
    """One ``orgs.<name>`` entry — org-code policy."""

    code: str | None = Field(None, description="Short org code for the required-prefix policy.")
    policy: Literal["required", "personal"] | None = Field(
        None,
        description="required: org-native repos must use <code>-<repo>; personal: a suggestion.",
    )


class PassthroughConfig(_Section):
    """Passthrough gating for the raw `bh bd` / `bh git` escape hatches."""

    bd_enabled: bool = Field(False, description="Whether the `bh bd` passthrough runs.")
    git_enabled: bool = Field(True, description="Whether the `bh git` passthrough runs.")


class LogConfig(_Section):
    """Diagnostics logging (structlog dual-mode pipeline)."""

    format: Literal["auto", "rich", "json"] = Field(
        "auto", description="auto detects TTY (rich) vs non-interactive (json)."
    )
    level: Literal["debug", "info", "warning", "error", "critical"] = Field(
        "info", description="Minimum level for diagnostics."
    )


class ArchiveConfig(_Section):
    """Soft-archive graveyard for retired hives."""

    dir: str | None = Field(None, description="Root directory for soft-archived clones.")
    window_days: int = Field(
        30, description="Days an archived clone is kept before eligible for pruning."
    )


class BackupConfig(_Section):
    """Retention policy for the four backup roots (``bh backup``, bh-cmqp.2 / bh-5009a — see
    ``docs/design/backup-retention-boundary-adr.md`` for the boundary between them). Host-scoped:
    how much of THIS host's disk each root may keep is a local tuning knob, not fleet policy."""

    hq_keep: int = Field(
        3,
        description=(
            "Dated sets kept under $BH_HOME/backups/hq/ (newest first); pruned automatically "
            "right after `bh hq init` takes and verifies a new one. Never clamped below 1 — a "
            "fresh backup is always left restorable."
        ),
    )
    hive_cap_mb: int = Field(
        500,
        description=(
            "Size threshold (MB) for <hive>/.beads/backup/ (bd's own Dolt-native backup) "
            "past which `bh backup reclaim --root hive` rotates it. Below the cap, reclaim "
            "is a no-op."
        ),
    )
    hive_rotate_keep: int = Field(
        3,
        description=(
            "Rotated .beads/backup.<timestamp>/ generations kept after a `--root hive` "
            "reclaim (newest first)."
        ),
    )
    migrate_keep: int = Field(
        3,
        description=(
            "Pre-migration backup sets kept PER HIVE under $BH_HOME/backups/migrate/ (newest "
            "first); pruned automatically right after `bh hive migrate-storage` verifies a new "
            "one."
        ),
    )
    total_warn_mb: int = Field(
        2048,
        description=(
            "Total across every backup root past which `bh backup usage` warns. 0 disables "
            "the warning."
        ),
    )


class AlertsConfig(_Section):
    """Host-local disk-pressure policy for the agent-steering alert surface."""

    worktree_cap_mb: int = Field(
        5120,
        ge=0,
        description=(
            "Per-hive managed-worktree footprint cap in MB. Above this, `bh alerts show` "
            "asks an operator to dispatch cleanup. 0 disables this alert."
        ),
    )
    disk_free_floor_mb: int = Field(
        10240,
        ge=0,
        description=(
            "Host-root filesystem free-space floor in MB. Below this, `bh alerts show` asks "
            "an operator to reclaim capacity from the filesystem mounted at `/`. 0 disables "
            "this alert."
        ),
    )
    worktree_filesystem_free_floor_mb: int = Field(
        10240,
        ge=0,
        description=(
            "Free-space floor in MB for the filesystem containing the configured worktree "
            "root. Below this, `bh alerts show` asks an operator to prune worktrees or move "
            "the root to a filesystem with more capacity. When omitted from an existing "
            "config, inherits `disk_free_floor_mb`. 0 disables this alert."
        ),
    )


class MetadataConfig(_Section):
    """Workspace-metadata cache."""

    ttl: float = Field(
        300,
        description="Coarse TTL backstop in seconds. 0=always-fresh, negative=never-expire.",
    )
    background_reload: bool = Field(
        True, description="Whether per-repo invalidation kicks a threaded background refresh."
    )


# ---- claude / git_workspace / orca --------------------------------------------


class ClaudeConfig(_Section):
    """Claude Code plugin distribution — how `bh hive init --claude` installs seat agents +
    role skills."""

    source: Literal["plugin", "copy"] = Field(
        "plugin",
        description="plugin: install via the marketplace. copy: legacy copy-into-repo.",
    )
    scope: Literal["user", "project"] = Field(
        "user", description="Install scope for the bh plugin."
    )
    marketplace: str = Field(".", description="Marketplace path/identifier for the bh plugin.")
    plugin: str = Field("bh", description="Name of the Claude Code plugin vending seat agents.")


class GitWorkspaceConfig(_Section):
    """git-workspace repo-group config and workspace-root ownership policy.

    There is no ``enabled`` flag: git-workspace is a required dependency. ``mode`` and
    ``root`` instead decide whether bh owns the clone root or consumes an operator-owned
    workspace. Root resolution itself remains centralized in :mod:`beadhive.identity`.
    """

    mode: Literal["internal", "external"] | None = Field(
        None,
        description=(
            "internal: bh owns <bh home>/ws. external: use $GIT_WORKSPACE, an explicit root, "
            "or the legacy ~/workspace. Unset preserves a populated legacy workspace and "
            "otherwise defaults to internal."
        ),
    )
    root: str | None = Field(
        None,
        description=(
            "Explicit external workspace root. Rejected with mode: internal; relocate an "
            "internal workspace by moving the complete bh home with $BH_HOME."
        ),
    )

    path: str | None = Field(
        None,
        description=(
            "Explicit workspace*.toml path — the escape hatch, and it always wins. Unset, bh "
            "takes the first of $GIT_WORKSPACE/workspace*.toml (externally managed) then "
            "<hq_dir>/workspace*.toml (internally managed — inherited by cloning HQ)."
        ),
    )
    hive_match: Literal["flexible", "prefix", "triplet"] = Field(
        "flexible", description="How `bh -r <id> ...` resolves a hive."
    )

    @model_validator(mode="after")
    def _root_is_external_only(self) -> GitWorkspaceConfig:
        if self.mode == "internal" and self.root:
            raise ValueError(
                "git_workspace.root cannot be set with git_workspace.mode: internal; "
                "remove root or use mode: external. To relocate the internal workspace, "
                "set $BH_HOME."
            )
        return self


class OrcaWorktreesConfig(_Section):
    """``orca.worktrees`` in its expanded (mapping) form."""

    enabled: bool = Field(False, description="Whether orca worktree delegation is flagged on.")
    fallback: bool = Field(
        False, description="Whether orca falls back gracefully when its runtime is down."
    )


class OrcaConfig(_Section):
    """orca — repo registry integration (first plugin)."""

    enabled: bool = Field(False, description="Whether orca (repo registry integration) is on.")
    worktrees: bool | OrcaWorktreesConfig = Field(
        False, description="Bare bool, or {enabled, fallback} for the expanded form."
    )
    data_path: str | None = Field(
        None, description="Path to orca's on-disk state (orca-data.json)."
    )


class RepowiseConfig(_Section):
    """repowise — optional local codebase index integration."""

    enabled: bool = Field(False, description="Enable the repowise index integration.")


class HitchConfig(_Section):
    """Optional integration with agent-hitch (docs/design/managed-harness-config-adr.md,
    Amendment 2) — resolves a seat's Hitch Pack profile into a Config Directory and launches a
    harness against it via `bh plugin hitch up <target> <profile>`.

    No AND-gate on another plugin (unlike orca, which requires git-workspace because it
    registers git-workspace's own clones): hitch shares no data or state with git-workspace /
    orca / observaloop, so it is independently enable-able.
    """

    enabled: bool = Field(False, description="Enable the hitch launch integration.")
    command: str = Field("hitch", description="Override the hitch CLI command/path.")
    repo: str | None = Field(
        None,
        description=(
            "Path to the agent-hitch checkout providing profiles/local.yaml + "
            "catalogs/local.yaml + packs/. Required to actually launch."
        ),
    )
    root: str | None = Field(
        None,
        description="Persistent Config Directory root (ephemeral=false only); default "
        "~/.beadhive/hitch.",
    )


class HerdrConfig(_Section):
    """Optional Herdr agent-pane defaults.

    Herdr, rather than Beadhive, owns the installed agent-kind vocabulary.  This
    schema consequently validates only that an operator supplied a non-empty
    string; the Herdr launch boundary checks it against the live supported list.
    """

    kind: str | None = Field(
        None,
        description=(
            "Preferred Herdr agent kind (for example claude or codex). The installed Herdr "
            "CLI validates supported values when a pane is launched."
        ),
    )

    @field_validator("kind")
    @classmethod
    def _kind_is_nonempty(cls, value):
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("herdr.kind must be a non-empty, trimmed agent kind")
        return value


# ---- plugins.tracker-github (tracker-adapter-plugin-adr.md section 3) -------------
# Host-owned authority for the `tracker-github` plugin. Every switch is off and every boolean is
# STRICT, so a hand-edited `"false"` is invalid rather than truthy. There is deliberately no
# `repo` key: owner and repository come from the bound hive's identity triplet (ADR B2), and no
# key can hold a credential, a command or an approval. Validation messages never repeat a value.

TRACKER_GITHUB_CREDENTIAL_SOURCES: tuple[str, ...] = ("env", "gh")


class _QuietSection(_Section):
    """A section whose ValidationError never repeats an input value (validated standalone by
    fragment composition and the composition root, where a caller may format ``str(exc)``)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class TrackerGithubFlag(_QuietSection):
    """One strict, default-off operation switch."""

    enabled: StrictBool = Field(False, description="Enable this operation (default off).")


class TrackerGithubCredential(_QuietSection):
    """Where the ambient credential is looked up; names and precedence only, never a value."""

    sources: list[Literal["env", "gh"]] = Field(
        default_factory=lambda: list(TRACKER_GITHUB_CREDENTIAL_SOURCES),
        description="Credential source names in precedence order (`env`: GH_TOKEN/GITHUB_TOKEN; "
        "`gh`: the gh CLI). Empty means anonymous reads only.",
    )

    @field_validator("sources")
    @classmethod
    def _unique_sources(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("credential sources must be unique members of: env, gh")
        return value


class TrackerGithubRead(_QuietSection):
    """Read operations (`issues.read`, `comments.read`) and their scope."""

    issues: TrackerGithubFlag = Field(
        default_factory=TrackerGithubFlag, description="`issues.read` (default off)."
    )
    comments: TrackerGithubFlag = Field(
        default_factory=TrackerGithubFlag, description="`comments.read` (default off)."
    )
    include_closed: StrictBool = Field(
        True, description="Scope within a read: include closed issues. Not a feature switch."
    )
    page_size: int | None = Field(
        None, strict=True, ge=1, le=100, description="Listing page size (1-100); unset = API."
    )


class TrackerGithubRetry(_QuietSection):
    """The adapter's retry budget."""

    max_retries: int = Field(3, strict=True, ge=0, description="Retries per request.")
    max_single_wait_seconds: float = Field(
        60, strict=True, gt=0, allow_inf_nan=False, description="Longest single wait."
    )
    max_total_wait_seconds: float = Field(
        120,
        strict=True,
        gt=0,
        allow_inf_nan=False,
        description="Total wait budget; at least max_single_wait_seconds.",
    )

    @model_validator(mode="after")
    def _single_within_total(self):
        if self.max_single_wait_seconds > self.max_total_wait_seconds:
            raise ValueError(
                "retry.max_single_wait_seconds must not exceed retry.max_total_wait_seconds"
            )
        return self


class TrackerGithubLabels(_QuietSection):
    """`labels.change`: necessary, never sufficient (each write needs a single-use grant)."""

    enabled: StrictBool = Field(False, description="Enable `labels.change` (default off).")
    owned_prefixes: list[str] = Field(
        default_factory=list,
        description="Label prefixes Beadhive owns; empty means no label write is possible.",
    )

    @field_validator("owned_prefixes")
    @classmethod
    def _nonempty_prefixes(cls, value):
        if any(not item or item != item.strip() for item in value):
            raise ValueError("write.labels.owned_prefixes entries must be non-empty and trimmed")
        return value

    @model_validator(mode="after")
    def _enabled_needs_an_owned_prefix(self):
        # Fail closed at load: an enabled write with nothing it may touch can only be a mistake.
        if self.enabled and not self.owned_prefixes:
            raise ValueError(
                "write.labels.enabled needs a non-empty write.labels.owned_prefixes "
                "(an empty list means no label write is possible)"
            )
        return self


class TrackerGithubWrite(_QuietSection):
    """Write operations. Enabling one is necessary, never sufficient."""

    comment: TrackerGithubFlag = Field(
        default_factory=TrackerGithubFlag, description="`comment.create` (default off)."
    )
    labels: TrackerGithubLabels = Field(default_factory=TrackerGithubLabels)


class TrackerGithubConfig(_QuietSection):
    """``plugins.tracker-github`` — the GitHub tracker adapter plugin (host-owned).

    Off unless configured: no owner, no port and no network while ``enabled`` is false. A store
    binding (``managed_repos[].stores.public.adapter``) never enables anything here.
    """

    enabled: StrictBool = Field(False, description="The plugin's own gate (default off).")
    base_url: str | None = Field(
        None,
        description="GitHub Enterprise Server API prefix (https, no userinfo, query or "
        "fragment); unset = the public API.",
    )
    credential: TrackerGithubCredential = Field(default_factory=TrackerGithubCredential)
    read: TrackerGithubRead = Field(default_factory=TrackerGithubRead)
    http_cache: TrackerGithubFlag = Field(
        default_factory=TrackerGithubFlag,
        description="Conditional requests for the id-reconcile pass (default off).",
    )
    retry: TrackerGithubRetry = Field(default_factory=TrackerGithubRetry)
    write: TrackerGithubWrite = Field(default_factory=TrackerGithubWrite)

    @field_validator("base_url")
    @classmethod
    def _https_api_prefix(cls, value):
        if value is None:
            return value
        # Never repeat the value: a URL can carry userinfo, i.e. a credential.
        try:
            parts = urlsplit(value)
            valid_port = parts.port is None or parts.port > 0
        except ValueError:
            parts, valid_port = None, False
        if (
            parts is None
            or not valid_port
            or parts.scheme != "https"
            or not parts.hostname
            or "@" in parts.netloc
            or "?" in value
            or "#" in value
        ):
            raise ValueError("base_url must be an https URL without userinfo, query or fragment")
        return value


# ---- intake.external (external-intake-module-adr.md section 15) -------------------


class IntakeExternalConfig(_QuietSection):
    """``intake.external`` — external issue intake (host-owned; everything off).

    Off unless configured: with ``enabled`` false `bh work import` refuses and the intake port is
    the null one. Nothing here binds a tracker, holds a credential, a grant or an approval, and no
    key can enable or pre-approve a tracker write: a write is only ever the operator's, per record.
    """

    enabled: StrictBool = Field(
        False, description="Enable external issue intake: `bh work import` (default off)."
    )
    default_type: Literal["bug", "feature", "chore"] = Field(
        "bug", description="The issue type of an imported report."
    )


class IntakeConfig(_QuietSection):
    """``intake`` — the intake module's host-owned settings."""

    external: IntakeExternalConfig = Field(default_factory=IntakeExternalConfig)


# ---- plugins / plugin_kernel (plugin-kernel-v1-adr.md section 6) -----------------

_PLUGIN_ID_RE = re.compile(r"[a-z][a-z0-9-]*")
_CAPABILITY_KEY_RE = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+@[1-9][0-9]*")


class PluginsConfig(_QuietSection):
    """``plugins.<plugin_id>`` — the canonical plugin-owned namespaces.

    Built-in fragments are validated against their published models; an unknown plugin id is
    retained as opaque, inactive data (the published schema's policy), never activated by being
    present. Each field's persisted key is its plugin id (the alias); absent = today's behaviour.
    """

    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    herdr: HerdrConfig | None = Field(None, description="Canonical namespace of `herdr`.")
    hitch: HitchConfig | None = Field(None, description="Canonical namespace of `hitch`.")
    observaloop: ObservaloopConfig | None = Field(
        None, description="Canonical namespace of `observaloop`."
    )
    orca: OrcaConfig | None = Field(None, description="Canonical namespace of `orca`.")
    repowise: RepowiseConfig | None = Field(None, description="Canonical namespace of `repowise`.")
    tracker_github: TrackerGithubConfig | None = Field(
        None,
        alias="tracker-github",
        description="The GitHub tracker adapter plugin (host-owned; every switch off).",
    )


class PluginKernelSection(_QuietSection):
    """``plugin_kernel`` — core-owned discovery policy, enablement and owner selection.

    The shape :func:`beadhive.kernel.plugins.parse_kernel_config` accepts, checked here without
    importing the kernel. Messages never repeat a key or value.
    """

    enabled: dict[str, StrictBool] = Field(
        default_factory=dict,
        description="Per-plugin enablement override (plugin id -> boolean); `false` always wins.",
    )
    capability_owners: dict[str, str] = Field(
        default_factory=dict,
        description="Selected owner per capability (`capability.id@MAJOR` -> plugin id).",
    )
    allow_external_entry_points: StrictBool = Field(
        False, description="Discover third-party entry-point manifests (default off)."
    )

    @field_validator("enabled")
    @classmethod
    def _plugin_id_keys(cls, value):
        if any(not _PLUGIN_ID_RE.fullmatch(key) for key in value):
            raise ValueError("plugin_kernel.enabled keys must be plugin ids")
        return value

    @field_validator("capability_owners")
    @classmethod
    def _owner_rows(cls, value):
        for key, owner in value.items():
            if not _CAPABILITY_KEY_RE.fullmatch(key) or not _PLUGIN_ID_RE.fullmatch(owner):
                raise ValueError(
                    "plugin_kernel.capability_owners must map capability.id@MAJOR to a plugin id"
                )
        return value


# ---- managed_repos -------------------------------------------------------------


class FrameHivePolicy(_Section):
    """Canonical fleet-owned frame admission policy for one managed hive."""

    config_revision: str = Field(min_length=1, strict=True)
    requires: dict[str, str | int | list[str]] = Field(default_factory=dict)
    evict_after_s: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("requires", mode="before")
    @classmethod
    def valid_raw_requirements(cls, value):
        if not isinstance(value, dict):
            raise ValueError("frame capability requirements must be a mapping")
        for key, item in value.items():
            if key == "max_sessions":
                valid = type(item) is int and item > 0
            elif key in {"isolation", "trust_zone", "arch", "harness"}:
                valid = type(item) is str and bool(item)
            elif key == "harnesses":
                valid = type(item) is list and all(type(part) is str and part for part in item)
            else:
                valid = False
            if not valid:
                raise ValueError("invalid frame capability requirement")
        return value

    @field_validator("evict_after_s", mode="before")
    @classmethod
    def valid_raw_eviction(cls, value):
        if type(value) not in (int, float):
            raise ValueError("frame eviction duration must be a number")
        return value

    @field_validator("config_revision")
    @classmethod
    def valid_revision(cls, value):
        if value != value.strip():
            raise ValueError("frame policy revision must be trimmed")
        return value


# ---- managed_repos[*].stores (bh-lniwf.2) ---------------------------------------
# Optional per-repository bead stores: docs/design/two-store-scope-adr.md. Absent = today's one
# implicit store at the checkout's .beads, byte-for-byte. A store NAMES a remote descriptor
# (bh-b1scz's `dolt-remote-descriptor/v1`, declared per hive as `managed_repos[*].dolt_remote`)
# and never embeds a URL, credential or transport detail; `git-origin` is reserved for "ride the
# code origin's refs/dolt/data". (Not `origin`: that is the descriptor's own default NAME.)

STORE_SCOPES: tuple[str, ...] = ("internal", "public")
GIT_ORIGIN_REMOTE = "git-origin"
_STORE_PREFIX_RE = re.compile(r"[a-z][a-z0-9-]*")
_REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SYNC_FOR_AUTHORITY: dict[str, frozenset[str]] = {
    "local": frozenset({"push-pull", "none"}),
    "remote": frozenset({"pull-only", "none"}),
}

#: The known-adapter table (tracker-adapter-plugin-adr.md section 4, B6): adapter plugin id ->
#: the ``stores.<scope>.source`` it serves. A CLOSED static table, exactly as ``source`` is a
#: closed ``Literal``: config load may not consult plugin discovery, so a new adapter id is a
#: schema release. Drift-tested against the built-in manifests once the adapter package ships.
KNOWN_TRACKER_ADAPTERS: Mapping[str, str] = types.MappingProxyType(
    {"tracker-github": "github-issues"}
)
_ADAPTER_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ADAPTER_SHAPE_MESSAGE = (
    "adapter must be a plugin id (lowercase letters, digits and hyphens, at most 64 characters)"
)

#: Store models never repeat an input value in a ValidationError: ``store_target`` embeds
#: ``str(ValidationError)`` in ``StoreConfigInvalid``, and a pasted token must not be printed.
#: The setting must be on the model that is VALIDATED (``ManagedRepoStores``), so both carry it.
_STORE_MODEL_CONFIG = ConfigDict(extra="forbid", hide_input_in_errors=True)


class StoreDescriptor(_Section):
    """One bead store of a managed repository (``managed_repos[*].stores.<scope>``)."""

    model_config = _STORE_MODEL_CONFIG

    prefix: str | None = Field(
        None,
        description="Bead-id prefix of this store. Unique across every managed store. "
        "`internal` defaults to (and must equal) the hive `prefix`; required for `public`.",
    )
    path: str | None = Field(
        None,
        description="Detached store root holding `.beads/` (absolute or `~`). `internal`: "
        "absent = the checkout's own `.beads`; `public`: required, outside the checkout.",
    )
    remote: str | None = Field(
        None,
        description="Name of the store's HQ Dolt remote descriptor, never a URL or credential: "
        "`internal` names the hive's `dolt_remote` (bh-b1scz); `public` names its own nested "
        "`dolt_remote`. `git-origin` is reserved for the code origin's refs/dolt/data.",
    )
    dolt_remote: DoltRemoteDescriptor | None = Field(
        None,
        description="The store's own `dolt-remote-descriptor/v1` (bh-rz379). Public stores "
        "only: the internal store's descriptor is the hive's `dolt_remote`. `remote` names it "
        "and defaults to its `name`.",
    )
    authority: Literal["local", "remote"] | None = Field(
        None,
        description="Who is canonical: `local` (this store; internal) or `remote` (the "
        "external system, e.g. GitHub; public).",
    )
    sync: Literal["push-pull", "pull-only", "none"] | None = Field(
        None,
        description="Sync direction. `local` authority allows push-pull|none; `remote` "
        "authority allows pull-only|none.",
    )
    source: Literal["github-issues"] | None = Field(
        None, description="Projection source of a `public` store; forbidden on `internal`."
    )
    dispatch: bool | None = Field(
        None,
        description="Whether the store may issue operational work. Only `internal` may; a "
        "public store set to true is rejected.",
    )
    adapter: str | None = Field(
        None,
        description="Plugin id of the tracker adapter serving this store (public only; requires "
        "`source`). Fleet-owned intent: it never enables a plugin, a read or a write. Never a "
        "URL, repository or credential. Absent = unbound (today's behaviour).",
    )

    @field_validator("adapter")
    @classmethod
    def _adapter_is_a_plugin_id(cls, value):
        # R2. The value is deliberately NOT echoed: anything not shaped like a plugin id might
        # be a pasted secret.
        if value is not None and not _ADAPTER_ID_RE.fullmatch(value):
            raise ValueError(_ADAPTER_SHAPE_MESSAGE)
        return value

    @field_validator("prefix")
    @classmethod
    def _valid_prefix(cls, value):
        if value is not None and not _STORE_PREFIX_RE.fullmatch(value):
            raise ValueError(
                f"store prefix {value!r} must start with a lowercase letter and contain only "
                "lowercase letters, digits and hyphens"
            )
        return value

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value):
        if value is not None and not (value.startswith("/") or value.startswith("~")):
            raise ValueError(f"store path {value!r} must be absolute or start with ~")
        return value

    @field_validator("remote")
    @classmethod
    def _valid_remote(cls, value):
        if value is None:
            return value
        if "://" in value or "@" in value or ":" in value:
            raise ValueError(
                f"store remote {value!r} must name an HQ remote descriptor, not embed a URL, "
                "host or credential — declare the URL on the descriptor (bh-b1scz)"
            )
        if not _REMOTE_NAME_RE.fullmatch(value):
            raise ValueError(f"store remote {value!r} is not a valid descriptor name")
        return value

    @model_validator(mode="after")
    def _remote_names_the_nested_descriptor(self):
        """A nested descriptor is what ``remote`` names: default the name from it, and refuse a
        store that names one remote while nesting another, or nests the reserved code origin."""
        nested = self.dolt_remote
        if nested is None:
            return self
        if nested.name == GIT_ORIGIN_REMOTE:
            raise ValueError(
                f"store dolt_remote.name {GIT_ORIGIN_REMOTE!r} is reserved for the code origin's "
                "refs/dolt/data — name the descriptor something else"
            )
        if self.remote is None:
            self.remote = nested.name
        elif self.remote != nested.name:
            raise ValueError(
                f"store remote {self.remote!r} does not name its nested dolt_remote {nested.name!r}"
            )
        return self

    def effective_authority(self, scope: str) -> str:
        return self.authority or ("local" if scope == "internal" else "remote")

    def effective_sync(self, scope: str) -> str:
        if self.sync:
            return self.sync
        return "push-pull" if self.effective_authority(scope) == "local" else "pull-only"


class ManagedRepoStores(_Section):
    """``managed_repos[*].stores`` — the explicit internal/public store pair (bh-lniwf.2)."""

    model_config = _STORE_MODEL_CONFIG

    internal: StoreDescriptor | None = Field(
        None, description="The private, operational store (required when `stores` is set)."
    )
    public: StoreDescriptor | None = Field(
        None, description="The optional public projection store (explicit access only)."
    )

    @model_validator(mode="after")
    def _coherent(self):
        problems = store_pair_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self


def store_pair_problems(stores: ManagedRepoStores) -> list[str]:
    """Every per-repository store rule (two-store ADR sections 2-4), as messages."""
    problems: list[str] = []
    internal, public = stores.internal, stores.public
    if internal is None:
        return [
            "stores.internal is required when `stores` is declared — the operational store "
            "must be named, never inferred"
        ]
    for scope, store in (("internal", internal), ("public", public)):
        if store is None:
            continue
        authority = store.effective_authority(scope)
        sync = store.effective_sync(scope)
        if sync not in _SYNC_FOR_AUTHORITY[authority]:
            allowed = "|".join(sorted(_SYNC_FOR_AUTHORITY[authority]))
            problems.append(
                f"stores.{scope}: sync={sync!r} is invalid with authority={authority!r} "
                f"(allowed: {allowed})"
            )
    if internal.effective_authority("internal") != "local":
        problems.append("stores.internal: authority must be 'local' (bh writes it)")
    if internal.path is not None:
        problems.append(
            "stores.internal: path is not supported — the internal store stays at the "
            "checkout's .beads (two-store ADR section 11)"
        )
    if internal.source is not None:
        problems.append("stores.internal: source is only valid on a public store")
    if internal.dispatch is False:
        problems.append("stores.internal: dispatch cannot be false — it is the operational store")
    if internal.dolt_remote is not None:
        problems.append(
            "stores.internal: dolt_remote is not supported — the internal store's descriptor is "
            "the hive's own `dolt_remote` (bh-b1scz); name it with stores.internal.remote"
        )
    if public is not None:
        if not public.prefix:
            problems.append("stores.public: prefix is required")
        if not public.path:
            problems.append("stores.public: path is required (a detached store root)")
        if public.effective_authority("public") != "remote":
            problems.append(
                "stores.public: authority must be 'remote' (GitHub is authoritative for the "
                "projection; sync is pull-only)"
            )
        if public.dispatch:
            problems.append(
                "stores.public: dispatch cannot be true — a public store is never operational"
            )
        if internal.prefix and public.prefix == internal.prefix:
            problems.append("stores: internal and public must use different prefixes")
        if internal.remote is not None and internal.remote == public.remote:
            problems.append("stores: internal and public must name different remotes")
        if public.remote not in (None, GIT_ORIGIN_REMOTE) and public.dolt_remote is None:
            problems.append(
                f"stores.public: remote {public.remote!r} has no descriptor — a public store "
                "declares its own `dolt_remote` (or names 'git-origin'); it never borrows the "
                "hive's internal descriptor"
            )
        if internal.remote == GIT_ORIGIN_REMOTE:
            problems.append(
                "stores.internal: remote cannot be 'git-origin' when a public store exists — that "
                "would publish private beads on the code origin's refs/dolt/data"
            )
    for scope, store in (("internal", internal), ("public", public)):
        if store is not None:
            problems += adapter_problems(scope, store.adapter, store.source)
    return problems


def adapter_problems(
    scope: str,
    adapter: str | None,
    source: str | None,
    known: Mapping[str, str] = KNOWN_TRACKER_ADAPTERS,
) -> list[str]:
    """The document-only binding rules R1, R3, R4 and R5 for one store's ``adapter``
    (tracker-adapter-plugin-adr.md section 4); empty when it is unset. R2, the shape, is the
    field validator. Pure: no discovery, environment, network or plugin import. Only an id that
    already passed the R2 shape is ever repeated."""
    if adapter is None:
        return []
    if scope != "public":
        return [f"stores.{scope}: adapter is only valid on a public store"]
    if adapter not in known:
        return [
            f"stores.{scope}: unknown tracker adapter {adapter!r} — known adapters: "
            f"{', '.join(sorted(known))}"
        ]
    if source is None:
        return [
            f"stores.{scope}: adapter {adapter!r} needs `source: {known[adapter]}` — a bound "
            "store must name the tracker it mirrors"
        ]
    if source != known[adapter]:
        return [
            f"stores.{scope}: adapter {adapter!r} serves source {known[adapter]!r}, not {source!r}"
        ]
    return []


class ManagedRepoEntry(_Section):
    """One entry in ``managed_repos`` — a hive `bh hive init` maintains, with optional
    per-hive overrides of the sections above."""

    # Validated standalone (hq_hive_policy), so it is a validation ROOT for the stores block:
    # without this its str(ValidationError) repeats a stores value (see _STORE_MODEL_CONFIG).
    model_config = _STORE_MODEL_CONFIG

    provider: str = Field("", description="Repo-group path (auth/fetch mechanism label).")
    org: str = Field("", description="Org/account the repo belongs to.")
    repo: str = Field("", description="Repo name.")
    prefix: str = Field("", description="Short stable bead-id prefix for this hive.")
    frame_policy: FrameHivePolicy | None = Field(
        None, description="Explicit fleet-owned frame requirements for this hive."
    )
    kind: Literal["org-native", "personal", "prototype", "fork", "external", "hq"] | None = Field(
        None,
        description="Hive kind; forks also carry `upstream`. `hq` marks the Factory HQ "
        "singleton store (registry.HQ_KIND) — not a hive, and excluded from hive-level "
        "operations; it is written by `bh hq init`, not by `bh hive init`.",
    )
    furnish: Literal["full", "none"] | None = Field(
        None,
        description="Declared footprint: `full` (tracked scaffolding + scaffold commit) or "
        "`none` (zero-footprint: local-only .beads, nothing committed). Absent infers from "
        "`kind` — forks/external were never furnished, everything else was (registry.furnish_of).",
    )
    upstream: str | None = Field(None, description='Upstream "owner/name" for a fork kind.')
    contribution: Literal["pull"] | None = Field(
        None,
        description="Marks a hive as a Contribution-plane target. Today only `pull` "
        "(upstream is a read rail; nothing yet consumes it as a push/PR target).",
    )

    @field_validator("kind", "furnish", "contribution", mode="before")
    @classmethod
    def _empty_string_is_unset(cls, v):
        """`kind: ""` means "unset" to every reader — they all do
        `str(entry.get("kind", ""))` and compare against known values, so an empty string
        already behaves as absent at runtime. Normalize it here rather than admitting `""` as
        a Literal member, so the schema agrees with the runtime without widening the type."""
        return None if v == "" else v

    worktree_init: list[WorktreeInitRule] = Field(
        default_factory=list, description="Extra init rules appended after the global ones."
    )
    toolchain: str | list[str] | None = Field(
        None, description="Per-hive declared toolchain name(s) (overrides worktrees.toolchain)."
    )
    toolchains: dict[str, dict[str, str]] | None = Field(
        None, description="Per-hive toolchain registry overrides."
    )
    harness: Literal["claude", "opencode"] | None = Field(
        None, description="Per-hive harness override (overrides top-level `harness`)."
    )
    work: WorkConfig | None = Field(None, description="Per-hive `work` section override.")
    release: ReleaseConfig | None = Field(None, description="Per-hive `release` section override.")
    claude: ClaudeConfig | None = Field(None, description="Per-hive `claude` section override.")
    observaloop: ObservaloopConfig | None = Field(
        None, description="Per-hive `observaloop` section override."
    )
    orca: OrcaConfig | None = Field(None, description="Per-hive `orca` section override.")
    repowise: RepowiseConfig | None = Field(
        None, description="Per-hive `repowise` section override."
    )
    hitch: HitchConfig | None = Field(None, description="Per-hive `hitch` section override.")
    herdr: HerdrConfig | None = Field(None, description="Per-hive `herdr` section override.")
    dolt_remote: DoltRemoteDescriptor | None = Field(
        None,
        description="HQ-declared desired Dolt remote (dolt-remote-descriptor/v1); absent keeps "
        "today's Git-origin behaviour.",
    )
    stores: ManagedRepoStores | None = Field(
        None,
        description="Optional explicit internal/public bead stores (bh-lniwf.2). Absent = the "
        "one implicit store at the checkout's .beads (today's behavior).",
    )

    @model_validator(mode="after")
    def _internal_prefix_is_hive_prefix(self):
        internal = self.stores.internal if self.stores is not None else None
        if internal is not None and internal.prefix and self.prefix:
            if internal.prefix != self.prefix:
                raise ValueError(
                    f"stores.internal.prefix {internal.prefix!r} must equal the hive prefix "
                    f"{self.prefix!r} — the internal store keeps the hive's ids"
                )
        return self

    @model_validator(mode="after")
    def _internal_remote_is_hive_descriptor(self):
        """The internal store's descriptor IS the hive's ``dolt_remote`` (bh-rz379): when both
        are declared, ``stores.internal.remote`` must name it, and a public store may not."""
        internal = self.stores.internal if self.stores is not None else None
        if internal is None or self.dolt_remote is None:
            return self
        hive_name = self.dolt_remote.name
        if internal.remote is not None and internal.remote != hive_name:
            raise ValueError(
                f"stores.internal.remote {internal.remote!r} must name the hive dolt_remote "
                f"{hive_name!r} — the hive descriptor is the internal store's remote"
            )
        public = self.stores.public
        if public is not None and public.remote == hive_name:
            raise ValueError(
                f"stores.public.remote {hive_name!r} names the hive's internal descriptor — "
                "a public store declares its own dolt_remote"
            )
        return self


# ---- top level ------------------------------------------------------------------


def _declared_dolt_remotes(entry: ManagedRepoEntry) -> list[DoltRemoteDescriptor]:
    """Every descriptor one hive declares: the hive's own (its internal store) and each store's
    nested one (bh-rz379). One location per database holds across all of them."""
    found = [entry.dolt_remote] if entry.dolt_remote is not None else []
    if entry.stores is not None:
        for scope in STORE_SCOPES:
            store = getattr(entry.stores, scope)
            if store is not None and store.dolt_remote is not None:
                found.append(store.dolt_remote)
    return found


def fleet_store_problems(entries: Sequence[ManagedRepoEntry]) -> list[str]:
    """Cross-entry store collisions, as messages (two-store ADR section 4)."""
    problems: list[str] = []
    hive_prefixes: dict[str, str] = {}
    for entry in entries:
        if entry.prefix:
            hive_prefixes.setdefault(entry.prefix, f"{entry.org}/{entry.repo}")
    claimed: dict[str, str] = {}
    paths: dict[str, str] = {}
    remotes: dict[str, str] = {}
    for entry in entries:
        if entry.stores is None:
            continue
        hive = f"{entry.org}/{entry.repo}"
        for scope in STORE_SCOPES:
            store = getattr(entry.stores, scope)
            if store is None:
                continue
            label = f"{hive} stores.{scope}"
            if scope == "public" and store.prefix:
                owner = hive_prefixes.get(store.prefix) or claimed.get(store.prefix)
                if owner:
                    problems.append(f"{label}: prefix {store.prefix!r} is already used by {owner}")
                claimed[store.prefix] = label
            if store.path:
                key = os.path.normpath(os.path.expanduser(store.path))
                if key in paths:
                    problems.append(f"{label}: path {store.path!r} is already used by {paths[key]}")
                paths[key] = label
            if store.remote and store.remote != GIT_ORIGIN_REMOTE:
                if store.remote in remotes:
                    problems.append(
                        f"{label}: remote {store.remote!r} is already used by "
                        f"{remotes[store.remote]} — each store needs its own remote path"
                    )
                remotes[store.remote] = label
    return problems


class LegacyBeadsConfig(BaseModel):
    """Legacy top-level Beads section with one live selector and opaque extensions."""

    model_config = ConfigDict(extra="allow")

    engine: Literal["bd"] | None = Field(
        None,
        description="Installed Beads engine; absent, null, or empty selects bd.",
    )

    @field_validator("engine", mode="before")
    @classmethod
    def _empty_engine_is_default(cls, value):
        return None if value == "" else value


class BeadhiveConfig(BaseSettings):
    """The bh config schema: ~/.beadhive/config.yaml, validated + discoverable.

    A validation + schema layer only — the ~40 getters in :mod:`beadhive.config` keep reading
    the ruamel ``CommentedMap`` directly and own the actual read/write path. This model is for
    checking a loaded config's shape (bh-5cgm.2) and dumping it for discovery (bh-5cgm.4).
    """

    model_config = SettingsConfigDict(
        env_prefix="BH_",
        env_nested_delimiter="__",
        extra="forbid",
        nested_model_default_partial_update=True,
        # The validation root of every daemon/bootstrap call site that formats the error
        # (`str(exc)`), so no config value — a pasted token anywhere — is repeated. Structured
        # renderers (`validate_config`, resolution) read `msg`/`loc` and are unaffected.
        hide_input_in_errors=True,
    )

    schema_version: int = Field(
        SCHEMA_VERSION, description="Config schema version this file was written for."
    )
    beads: LegacyBeadsConfig | None = Field(
        None, description="Legacy Beads settings with a validated engine selector."
    )
    delimiter: str = Field(":", description="Label delimiter.")
    providers: list[str] = Field(
        default_factory=lambda: ["github", "gitlab", "gitea"],
        description="Recognized provider labels (git-workspace auth/fetch mechanisms).",
    )
    orgs: dict[str, OrgEntry] = Field(
        default_factory=dict, description="org (full name) -> {code, policy}."
    )
    exclude: ExcludeConfig = Field(default_factory=ExcludeConfig)
    dimensions: dict[str, DimensionConfig] = Field(
        default_factory=dict, description="Non-identity label dimensions (orthogonal axes)."
    )
    dolt: DoltConfig = Field(default_factory=DoltConfig)
    git_workspace: GitWorkspaceConfig = Field(default_factory=GitWorkspaceConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    passthrough: PassthroughConfig = Field(default_factory=PassthroughConfig)
    otel: OtelConfig = Field(default_factory=OtelConfig)
    observaloop: ObservaloopConfig = Field(default_factory=ObservaloopConfig)
    worktrees: WorktreesConfig = Field(default_factory=WorktreesConfig)
    harness: Literal["claude", "opencode"] = Field(
        "claude",
        description="Agent harness `bh role <seat>` execs: claude | opencode. BH_HARNESS wins.",
    )
    work: WorkConfig = Field(default_factory=WorkConfig)
    hq: HqConfig = Field(default_factory=HqConfig)
    host: HostConfig = Field(default_factory=HostConfig)
    release: ReleaseConfig = Field(default_factory=ReleaseConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    orca: OrcaConfig = Field(default_factory=OrcaConfig)
    repowise: RepowiseConfig = Field(default_factory=RepowiseConfig)
    hitch: HitchConfig = Field(default_factory=HitchConfig)
    herdr: HerdrConfig = Field(default_factory=HerdrConfig)
    intake: IntakeConfig | None = Field(
        None,
        description="External issue intake (host-owned, every switch off). Absent = intake off.",
    )
    plugins: PluginsConfig | None = Field(
        None,
        description="Canonical plugin-owned namespaces `plugins.<plugin_id>`. Built-in fragments "
        "are validated; an unknown id is kept as opaque inactive data. Absent = today.",
    )
    plugin_kernel: PluginKernelSection | None = Field(
        None,
        description="Core-owned plugin discovery policy: enablement overrides, capability owner "
        "selection, external entry points. Absent = the kernel defaults.",
    )
    managed_repos: list[ManagedRepoEntry] = Field(
        default_factory=list, description="Managed hives — maintained by `bh hive init`."
    )

    @field_validator("managed_repos")
    @classmethod
    def _distinct_dolt_remote_locations(cls, value):
        """Each object-store database needs its own path (ADR "One location per database")."""
        seen: set[str] = set()
        for repo in value:
            for descriptor in _declared_dolt_remotes(repo):
                location = dolt_remote_location(descriptor.url)
                if location is None:
                    continue
                if location in seen:
                    raise _dolt_remote_error(
                        "dolt_remote_path_shared",
                        "two managed stores declare the same Dolt remote location",
                    )
                seen.add(location)
        return value

    @model_validator(mode="after")
    def _store_references(self):
        """Fleet-wide store uniqueness (bh-lniwf.2): every store-declared prefix is unique
        across EVERY managed store (hive prefixes included), and every detached path and every
        non-`git-origin` remote name is unique. Legacy entries without `stores` are untouched, so
        a pre-existing hive-prefix collision is still only reported, never newly rejected."""
        errors = fleet_store_problems(self.managed_repos)
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @model_validator(mode="after")
    def _gate_references(self):
        """Unknown subset or key names in `work.gate`, checked on the EFFECTIVE view: the
        global section alone, then each hive layered over it the way the readers layer it
        (profiles and subsets per name, the attest key list replaced whole)."""
        glob = self.work
        errors = gate_reference_errors(
            glob.gate.profiles, glob.gate.subsets, [key.name for key in glob.attest.keys]
        )
        for repo in self.managed_repos:
            hive = repo.work
            if hive is None:
                continue
            keys = hive.attest.keys if "keys" in hive.attest.model_fields_set else glob.attest.keys
            hive_errors = gate_reference_errors(
                {**glob.gate.profiles, **hive.gate.profiles},
                {**glob.gate.subsets, **hive.gate.subsets},
                [key.name for key in keys],
            )
            label = repo.prefix or repo.repo or "?"
            errors += [f"hive {label}: {error}" for error in hive_errors if error not in errors]
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        """BH_* env vars win over the constructor value (the already-loaded ruamel config
        data — `BeadhiveConfig(**loaded_dict)`), matching the standing env > file > default
        precedence used everywhere else in this codebase (``config._env``, ``work_value``,
        …). Swaps the library's default init-first ordering; dotenv/secrets stay lowest."""
        # ``BH_WORKTREES`` is a transport-only scalar override consumed by
        # ``config.worktrees_root()``.  It must not also be interpreted by this settings model
        # as the structured ``worktrees`` section merely because it shares the ``BH_`` prefix:
        # pydantic then attempts to JSON-decode a filesystem path.  Filter only that runtime
        # alias from this source; ``config._Env`` continues to read it normally.
        # EnvSettingsSource holds a live view of the process environment.  Never mutate it:
        # config getters are deliberately re-evaluated after tests/commands alter os.environ.
        # A shallow source copy lets this schema omit its transport-only collision without
        # removing the legacy WS_WORKTREES override that the rest of bh still consumes.
        env_settings = copy(env_settings)
        env_vars = getattr(env_settings, "env_vars", None)
        if env_vars is not None:
            env_settings.env_vars = {
                name: value for name, value in env_vars.items() if name.lower() != "bh_worktrees"
            }
        return env_settings, init_settings, dotenv_settings, file_secret_settings


# ---- schema introspection (bh-5cgm.4: `bh config schema` + did-you-mean) ------
# Walks BeadhiveConfig's own fields (recursing into nested sub-models) to build the SAME
# dotted-key space `bh config get/set/unset` address — one source of truth for "what keys
# exist" (`bh config schema`) and "which known key is this typo closest to" (did-you-mean).
# No hand-maintained key list to drift from the model.


@dataclass(frozen=True)
class SchemaField:
    """One row of the schema dump: a dotted key + how BeadhiveConfig declares it."""

    path: str
    type: str
    default: str
    description: str


def _type_str(annotation: Any) -> str:
    """Render a field annotation as a short human string: `str | None`, `list[str]`,
    `'grpc' | 'http/protobuf'`, `WorktreesConfig`, …"""
    origin = typing.get_origin(annotation)
    if origin is types.UnionType or origin is typing.Union:
        return " | ".join(_type_str(a) for a in typing.get_args(annotation))
    if origin is typing.Literal:
        return " | ".join(repr(a) for a in typing.get_args(annotation))
    if origin in (list, dict):
        args = ", ".join(_type_str(a) for a in typing.get_args(annotation))
        return f"{origin.__name__}[{args}]" if args else origin.__name__
    if annotation is type(None):
        return "null"
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def _to_plain(value: Any) -> Any:
    """Collapse BaseModel instances (incl. nested in lists/dicts) to plain JSON-able data,
    so a default like ``worktrees.init``'s list of ``WorktreeInitRule`` renders as JSON
    instead of a python repr."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, ComplexityTier):
        return value.name
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    return value


def _default_str(field_info) -> str:
    """Render a field's default (or ``default_factory()``) compactly; ``(required)`` when
    the field has neither (e.g. ``WorktreeInitRule.run``)."""
    if field_info.default_factory is not None:
        value = field_info.default_factory()
    elif field_info.default is PydanticUndefined:
        return "(required)"
    else:
        value = field_info.default
    text = json.dumps(_to_plain(value), default=str)
    return text if len(text) <= 60 else text[:57] + "..."


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """The BaseModel subclass a field recurses into, or None for a scalar/collection field.
    Only a bare model or ``Model | None`` counts — ``list[Model]``/``dict[str, Model]`` are
    described as a single collection row, not expanded (their members are dynamically keyed,
    not fixed config keys)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = typing.get_origin(annotation)
    if origin is types.UnionType or origin is typing.Union:
        models = [
            a
            for a in typing.get_args(annotation)
            if isinstance(a, type) and issubclass(a, BaseModel)
        ]
        if len(models) == 1:
            return models[0]
    return None


def _member_model(annotation: Any) -> type[BaseModel] | None:
    """The BaseModel a ``list[Model]``/``dict[str, Model]`` field holds, or None.

    Distinct from :func:`_nested_model` on purpose: those members are dynamically keyed, so
    they are described (`managed_repos[].furnish`) but never treated as settable keys."""
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


# ---- Literal-range lookup (bh-aidze) ------------------------------------------
# `dolt.backend: shared-server` was accepted, persisted, and echoed back by `bh config show`
# as if in effect — `Literal["colima", "docker", "podman", "none"]` in DoltConfig, but neither
# `set_value`'s write-path `_validate` nor a bare `load()` checked a leaf's value against its
# OWN field's Literal range (as opposed to the section/key SHAPE `_validate` already checks).
# These two helpers give both call sites (`config._validate`, `config.literal_violations`) the
# same single source of truth `iter_schema_fields` already is for key SHAPE, now for value
# RANGE too — one schema walk, not two hand-written ones that could drift apart.


def _field_info(dotted: str, model: type[BaseModel] = BeadhiveConfig):
    """The leaf ``FieldInfo`` for *dotted*, walking nested sub-models one segment at a time —
    or ``None`` when any segment isn't a real field (unknown key, or a key belonging to a
    dynamically-keyed collection like ``managed_repos[].kind``, which this deliberately does
    not walk into)."""
    parts = dotted.split(".")
    node_model = model
    info = None
    for i, part in enumerate(parts):
        fields = _fields_by_key(node_model) if getattr(node_model, "model_fields", None) else None
        info = fields.get(part) if fields else None
        if info is None:
            return None
        if i == len(parts) - 1:
            return info
        nested = _nested_model(info.annotation)
        if nested is None:
            return None
        node_model = nested
    return info


def literal_choices(dotted: str) -> tuple[Any, ...] | None:
    """The allowed values for *dotted* if it (or its ``X | None`` wrapper) is a
    ``Literal[...]`` field, else ``None`` — either *dotted* isn't a known schema field, or it
    is one but isn't Literal-typed (nothing to range-check)."""
    info = _field_info(dotted)
    if info is None:
        return None
    annotation = info.annotation
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return typing.get_args(annotation)
    if origin is types.UnionType or origin is typing.Union:
        for arg in typing.get_args(annotation):
            if typing.get_origin(arg) is typing.Literal:
                return typing.get_args(arg)
    return None


def field_default(dotted: str) -> Any:
    """The schema default for *dotted* — the EFFECTIVE value a reader falls back to when the
    persisted one fails validation (bh-aidze's ``bh config show`` half). ``None`` when *dotted*
    isn't a known schema field, or is one with neither a default nor a ``default_factory``
    (e.g. a required nested-rule field like ``WorktreeInitRule.run``)."""
    info = _field_info(dotted)
    if info is None:
        return None
    if info.default_factory is not None:
        return info.default_factory()
    if info.default is PydanticUndefined:
        return None
    return info.default


def _persisted_key(model: type[BaseModel], name: str, info: Any) -> str:
    """The key a field is written under: its alias when the alias is the ONLY accepted
    spelling (``plugins.tracker-github``), else the field name. A model that also populates by
    name (``work.validate_overrides``) keeps reporting the field name, as it always has."""
    if isinstance(info.alias, str) and not model.model_config.get("populate_by_name"):
        return info.alias
    return name


def _fields_by_key(model: type[BaseModel]) -> dict[str, Any]:
    return {_persisted_key(model, name, info): info for name, info in model.model_fields.items()}


def iter_schema_fields(
    model: type[BaseModel] = BeadhiveConfig, prefix: str = ""
) -> list[SchemaField]:
    """Flatten *model*'s fields into dotted :class:`SchemaField` rows, recursing into nested
    sub-models (``otel.genai.model``, …) — the same key space `bh config get/set` operate on."""
    out: list[SchemaField] = []
    for name, info in _fields_by_key(model).items():
        path = f"{prefix}{name}"
        out.append(
            SchemaField(
                path, _type_str(info.annotation), _default_str(info), info.description or ""
            )
        )
        nested = _nested_model(info.annotation)
        if nested is not None:
            out.extend(iter_schema_fields(nested, prefix=f"{path}."))
            continue
        member = _member_model(info.annotation)
        if member is not None:
            # Members of a collection are dynamically keyed (`managed_repos.6.furnish`), so
            # they are NOT settable dotted keys and must stay out of `known_keys`. Marking
            # them `managed_repos[].furnish` keeps them discoverable in `bh config schema`
            # while remaining trivially filterable (bh-05w7).
            out.extend(iter_schema_fields(member, prefix=f"{path}[]."))
    return out


def known_keys(model: type[BaseModel] = BeadhiveConfig) -> list[str]:
    """Every dotted key BeadhiveConfig declares (section rows + leaves) — did-you-mean's
    universe of "known" keys. Collection-member rows (`…[].field`) are excluded: they describe
    a shape, not a key anyone can `bh config set`."""
    return [f.path for f in iter_schema_fields(model) if "[]" not in f.path]


def known_sections(model: type[BaseModel] = BeadhiveConfig) -> frozenset[str]:
    """Accepted top-level persisted sections derived from canonical fields and aliases."""

    names = set(CONFIG_SECTION_COMPATIBILITY_ALIASES)
    for name, info in model.model_fields.items():
        names.add(name)
        for alias in (info.alias, info.validation_alias, info.serialization_alias):
            if isinstance(alias, str):
                names.add(alias)
                continue
            for choice in getattr(alias, "choices", ()):
                if isinstance(choice, str):
                    names.add(choice)
    return frozenset(names)


def suggest_key(dotted: str, keys: list[str] | None = None, cutoff: float = 0.8) -> str | None:
    """Closest known dotted key to *dotted* (e.g. a typo'd `config get`/`set` argument), or
    None when nothing is close enough. A high cutoff means a hopelessly-wrong key — or a
    genuinely-just-unset one that happens to share a section prefix — gets no false-positive
    suggestion."""
    matches = difflib.get_close_matches(
        dotted, keys if keys is not None else known_keys(), n=1, cutoff=cutoff
    )
    if matches and matches[0] != dotted:
        return matches[0]
    return None


__all__ = (
    "AlertsConfig",
    "ArchiveConfig",
    "AttestConfig",
    "AttestImpactConfig",
    "AttestSemanticConfig",
    "AttestTrivialConfig",
    "AttestVersionBumpConfig",
    "AttestKeyConfig",
    "BackupConfig",
    "BeadhiveConfig",
    "BeadsConfig",
    "ClaudeConfig",
    "ConflictConfig",
    "CONFIG_SECTION_COMPATIBILITY_ALIASES",
    "DEFAULT_JUNK_GLOBS",
    "DEFAULT_PRECIOUS_GLOBS",
    "DEFAULT_PRECIOUS_MIN_BYTES",
    "DevIdentity",
    "DimensionConfig",
    "DispatchConfig",
    "DoltConfig",
    "ExcludeConfig",
    "GenaiConfig",
    "GitWorkspaceConfig",
    "HerdrConfig",
    "HitchConfig",
    "HostConfig",
    "HostDaemonConfig",
    "HostDispatchConfig",
    "HostForwardConfig",
    "HostForwardServeConfig",
    "HostLeaseConfig",
    "HqConfig",
    "IdentityConfig",
    "IntakeConfig",
    "IntakeExternalConfig",
    "KNOWN_TRACKER_ADAPTERS",
    "LogConfig",
    "ManagedRepoEntry",
    "ManagedRepoStores",
    "MetadataConfig",
    "ObservaloopConfig",
    "OrcaConfig",
    "OrcaWorktreesConfig",
    "OrgEntry",
    "OtelConfig",
    "PassthroughConfig",
    "PluginKernelSection",
    "PluginsConfig",
    "ReleaseConfig",
    "RepowiseConfig",
    "RoutingConfig",
    "RoutingTierConfig",
    "SCHEMA_VERSION",
    "SUBSET_PLACEHOLDER",
    "SchemaField",
    "StoreDescriptor",
    "TrackerGithubConfig",
    "ValidationBrokerConfig",
    "WorkConfig",
    "WorktreeInitRule",
    "WorktreesConfig",
    "adapter_problems",
    "field_default",
    "iter_schema_fields",
    "known_keys",
    "known_sections",
    "literal_choices",
    "suggest_key",
)
