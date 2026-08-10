"""BeadhiveConfig — the pydantic-settings schema for ~/.beadhive/config.yaml.

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

import difflib
import json
import types
import typing
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticUndefined
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# First official schema version. Bump only for a breaking change (rename/removal/type change
# that needs a transform) — see the module docstring.
SCHEMA_VERSION = 1


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
            "no sandbox grant needed. False: persistent worktrees under `path`."
        ),
    )
    path: str | None = Field(
        None, description="Persistent worktree root (ephemeral=false only); $BH_WORKTREES wins."
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
    max_seats_in_flight: int = Field(
        0,
        description=(
            "Max seats (spawned role-binary processes) an unattended loop runs at once. 0 or "
            "negative = unlimited (matches how other unset caps read). Held ONLY in the loop's "
            "own in-process memory — never persisted, resets on restart by design (bh-e7r9q.3; "
            "docs/design/loop-ownership-and-execution-memory-adr.md Decision 2). NOT the token-"
            "budget governor — that is `work.dispatch.budget`, owned by bh-3yoh.1."
        ),
    )
    max_run_wall_time_seconds: int = Field(
        0,
        description=(
            "Per-run wall-time cap in seconds; a seat still running past this is cancelled "
            "through the CANCEL ladder (docs/design/work-runtime-tiers-adr.md Amendment 2 §5). "
            "0 or negative = unlimited. Held ONLY in the loop's own in-process memory — never "
            "persisted, resets on restart by design (bh-e7r9q.3)."
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


class WorkConfig(_Section):
    """Integration-plane driver (`bh work`) settings — drives a bead assigned -> merged."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    validate_cmd: str = Field(
        "just check", description="Default validation command for any boundary without an override."
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
    batch_max_size: int = Field(
        5, description="Max issues a planner-declared batch:<group> may hold as one unit."
    )
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    identity: IdentityConfig | None = Field(
        None,
        description="Agent identity profile; omit (or mode: supervised) to inherit git config.",
    )
    conflict: ConflictConfig = Field(default_factory=ConflictConfig)


# ---- hq (Factory HQ remote, bh-e0y8.1) ----------------------------------------


class HqConfig(_Section):
    """Factory HQ remote (``hq``) — where the factory's central HQ store publishes to."""

    remote: str = Field(
        "",
        description=(
            "HQ repo remote, `<owner>/beadhive-hq` form. Empty (default) derives `<owner>` "
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


class HostConfig(_Section):
    """Multi-host model policy (``host``) — how this factory arbitrates who may write a hive.

    Distinct from ``hosts/<host_id>.yaml`` (the per-host manifest in HQ,
    :mod:`beadhive.hosts`): that file describes ONE machine; this section is the fleet-wide
    policy every machine applies."""

    lease: HostLeaseConfig = Field(default_factory=HostLeaseConfig)


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
    """git-workspace repo-group config. No `enabled` flag: git-workspace is a required dep
    (bh-hsus.4 — `deps.py`, `required=ALWAYS`), not an optional integration, so bh always reads
    whatever `workspace*.toml` it finds."""

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


# ---- managed_repos -------------------------------------------------------------


class ManagedRepoEntry(_Section):
    """One entry in ``managed_repos`` — a hive `bh hive init` maintains, with optional
    per-hive overrides of the sections above."""

    provider: str = Field("", description="Repo-group path (auth/fetch mechanism label).")
    org: str = Field("", description="Org/account the repo belongs to.")
    repo: str = Field("", description="Repo name.")
    prefix: str = Field("", description="Short stable bead-id prefix for this hive.")
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
    hitch: HitchConfig | None = Field(None, description="Per-hive `hitch` section override.")


# ---- top level ------------------------------------------------------------------


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
    )

    schema_version: int = Field(
        SCHEMA_VERSION, description="Config schema version this file was written for."
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
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    orca: OrcaConfig = Field(default_factory=OrcaConfig)
    hitch: HitchConfig = Field(default_factory=HitchConfig)
    managed_repos: list[ManagedRepoEntry] = Field(
        default_factory=list, description="Managed hives — maintained by `bh hive init`."
    )

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
        fields = getattr(node_model, "model_fields", None)
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


def iter_schema_fields(
    model: type[BaseModel] = BeadhiveConfig, prefix: str = ""
) -> list[SchemaField]:
    """Flatten *model*'s fields into dotted :class:`SchemaField` rows, recursing into nested
    sub-models (``otel.genai.model``, …) — the same key space `bh config get/set` operate on."""
    out: list[SchemaField] = []
    for name, info in model.model_fields.items():
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
