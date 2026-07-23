"""ws-layer write-guard for bd verbs forwarded through the hub and the `ws bd` passthrough.

bd has no notion of *where* it is safe to write, so ws gates two footguns bd will not protect
against itself:

  1. `ws hub bd create` (any mutating verb) mints a bead in the hub's READ cache — stranded as a
     permanent orphan. bd repo sync is ADDITIVE (empirically verified,):
     it imports source-hive beads alongside native ones, so a hub-native bead is *never* auto-wiped
     — it persists indefinitely with no source-hive home and no AGF workflow. The hub is a read-only
     cross-hive aggregate; only read verbs make sense there. We **allowlist** reads (simpler and
     safer than chasing a denylist of writes).

     Exception — hq-native (control-plane) writes: when the Factory HQ store IS the aggregate,
     writes that target an existing hq-prefixed bead (e.g. ``bd update hq-123``) are canonical
     control-plane operations and are explicitly allowed. A product-hive bead written directly into
     the aggregate (e.g. ``bd update bc-123`` via ``ws hub bd``) is still refused — that footgun
     stands regardless of additive-sync. The allowlist is extended, not flipped to a denylist.

  2. bare `bd github sync` / `bd github push` would push local beads to a PUBLIC tracker — bd has
     no sync-eligibility filter, so a broad sync leaks everything. Publishing upstream is the
     `contributor` seat's job, and even then only via the gated single-item path
     (`bd github push --issues <one-id>`), never a bare sync.

The guard is a thin gate over beads-native primitives: it decides *whether* a bd invocation is
allowed, and never reimplements bd behavior.
"""

from __future__ import annotations

import typer

from . import config
from .registry import HQ_PREFIX

# Read verbs safe to run against the hub cache (and any read-only aggregate).
READ_VERBS = frozenset({"list", "ready", "show", "stats", "search"})

# HQ-native control-plane bead IDs carry the reserved HQ_PREFIX (e.g. "hq-123").
# Writes that target an existing hq-prefixed bead are canonical control-plane operations
# and are explicitly allowed even against the aggregate (which IS the HQ store when HQ is live).
_HQ_ID_PREFIX = HQ_PREFIX + "-"

# github subcommands that publish local beads outward (the footgun); `pull`/`import` are safe.
_PUBLISH_SUBVERBS = frozenset({"push", "sync"})

# Seat convention (mirrors work.py `_guard_seat`): only a contributor seat may publish upstream.
_CONTRIB_PREFIX = "contrib/"

# Assurance plane (roles/RBAC matrix §2.3, bead .33): a `security:*` gate — secret-scan / SBOM /
# policy-as-code — is opened alongside the review gate and blocks the merge in PARALLEL with review
# (the generic open-gate check already refuses a merge while ANY gate naming the bead is open, so a
# change lands only when BOTH the review AND the security gate clear). Only a **warden** seat may
# RESOLVE a security gate — it owns the security + policy verdict; provenance stays with the
# contributor seat.
_WARDEN_PREFIX = "warden/"

# A security gate is identified by a `security:` marker in its bd-gate reason (parallel to how the
# review gate is matched on `reason: review`), so it is distinguishable from review/kickoff gates.
SECURITY_GATE_MARKER = "security:"


def is_warden(actor: str) -> bool:
    """Whether `actor` names a warden seat (warden/<name>) — the only seat allowed to resolve a
    security:* gate (mirrors the seat prefixes in work.py)."""
    return actor.startswith(_WARDEN_PREFIX)


def is_security_gate(gate) -> bool:
    """True when a bd gate dict is an Assurance `security:*` gate — matched on the `security:`
    marker in its reason/description (parallel to the review gate's `reason: review`). Tolerant of
    the two bd shapes: a top-level `reason` field and the `reason: …` tail in `description`."""
    if not isinstance(gate, dict):
        return False
    reason = str(gate.get("reason") or "").lower()
    desc = str(gate.get("description") or "").lower()
    return SECURITY_GATE_MARKER in reason or f"reason: {SECURITY_GATE_MARKER}" in desc


def guard_security_gate_resolution(gate, actor: str) -> None:
    """Assurance RBAC: only a warden (warden/<name>) may RESOLVE a `security:*` gate — so the
    security + policy verdict can't be self-cleared by the author/reviewer, and the merge stays
    blocked until the warden signs off. A no-op for non-security gates (review/kickoff/…) and for a
    warden actor; raises `typer.Exit(1)` when a non-warden targets a security gate."""
    if not is_security_gate(gate) or is_warden(actor):
        return
    gate_id = str(gate.get("id") or "?")
    typer.echo(
        f"✗ security gate {gate_id} is warden-only to resolve — {actor!r} is not a warden "
        "(warden/<name>).\n"
        "  The security:* gate is the Assurance verdict (secret-scan / SBOM / policy-as-code); it "
        "blocks the merge in parallel with review until a warden clears it.",
        err=True,
    )
    raise typer.Exit(1)


# Control-plane HQ-registry write partitioning (roles/RBAC matrix §2.1, bead .36). The Head Office
# registry (~/.ws/config.yaml) is partitioned by control seat: supervisor (super/) -> policy;
# director (dir/) -> fleet/managed_repos membership; custodian (cust/) -> hive config; controller
# (ctrl/) -> READ ONLY. The supervisor is org-root and, per the §2.1 collapse path, may write every
# partition (a single-hive factory runs just the supervisor, absorbing the other scopes).
_SUPERVISOR_PREFIX = "super/"
_DIRECTOR_PREFIX = "dir/"
_CUSTODIAN_PREFIX = "cust/"
_CONTROLLER_PREFIX = "ctrl/"

HQ_POLICY = "policy"
HQ_FLEET = "fleet"
HQ_HIVE_CONFIG = "hive-config"

# partition -> the control seat prefix that owns it (supervisor is handled separately as org-root).
_HQ_PARTITION_OWNER = {
    HQ_POLICY: _SUPERVISOR_PREFIX,
    HQ_FLEET: _DIRECTOR_PREFIX,
    HQ_HIVE_CONFIG: _CUSTODIAN_PREFIX,
}

# top-level config section -> HQ partition. Fleet membership and fleet-wide governance/policy are
# called out; everything else (per-hive work/otel/dolt/… knobs) is hive config (custodian's scope).
_HQ_SECTION_PARTITION = {
    "managed_repos": HQ_FLEET,
    "orgs": HQ_POLICY,
    "providers": HQ_POLICY,
    "dimensions": HQ_POLICY,
    "exclude": HQ_POLICY,
    "passthrough": HQ_POLICY,
}


def is_controller(actor: str) -> bool:
    """Whether `actor` names a controller seat (ctrl/<name>) — the read-only Control-plane seat
    that observes factory telemetry and never mutates the HQ registry."""
    return actor.startswith(_CONTROLLER_PREFIX)


def _control_prefix(actor: str) -> str:
    """The control-seat prefix `actor` carries (super//dir//cust//ctrl/), or '' for a non-control
    identity (a developer/dispatcher/human — not bound by the control-plane partitioning)."""
    for pfx in (_SUPERVISOR_PREFIX, _DIRECTOR_PREFIX, _CUSTODIAN_PREFIX, _CONTROLLER_PREFIX):
        if actor.startswith(pfx):
            return pfx
    return ""


def hq_partition_of_section(section: str) -> str:
    """The HQ-registry partition a top-level config `section` belongs to; unknown/per-hive
    sections default to hive config (the custodian's scope)."""
    return _HQ_SECTION_PARTITION.get(section, HQ_HIVE_CONFIG)


def guard_controller_readonly(actor: str) -> None:
    """Hard rule (§2.1): the controller (ctrl/) is READ-ONLY over the HQ registry — it observes
    factory telemetry and never mutates the registry, so any HQ-registry write by a controller is
    denied. No-op for every other identity. Raises `typer.Exit(1)` on a controller write."""
    if not is_controller(actor):
        return
    typer.echo(
        f"✗ HQ-registry write denied for {actor!r} — the controller seat (ctrl/) is READ-ONLY "
        "(factory telemetry only, no registry mutation) per the control-plane partitioning (§2.1).",
        err=True,
    )
    raise typer.Exit(1)


def guard_hq_registry_write(partition: str, actor: str) -> None:
    """Control-plane RBAC (§2.1): a write to an HQ-registry `partition` is allowed only for the
    owning control seat (policy->supervisor, fleet->director, hive-config->custodian); the
    supervisor may write any partition (org-root / collapse path). The controller (ctrl/) is denied
    (hard, read-only). A mismatched control seat is WARNED (soft — the non-controller control seats
    are advisory) but allowed. A non-control identity (human/developer/dispatcher) is exempt."""
    guard_controller_readonly(actor)  # hard: the controller never writes
    prefix = _control_prefix(actor)
    if not prefix or prefix == _SUPERVISOR_PREFIX:
        return  # non-control identity (exempt) or supervisor (org-root, writes every partition)
    owner = _HQ_PARTITION_OWNER.get(partition, "")
    if prefix == owner:
        return
    from . import log  # lazy: keep guard free of the log import at load

    log.get_logger(__name__).warning(
        "hq_registry_partition_violation",
        actor=actor,
        partition=partition,
        owner=owner or "?",
        reason="control-plane HQ-registry write outside the seat's partition (§2.1)",
    )


def _positionals(args) -> list[str]:
    """The positional (non-flag) tokens of a bd arg vector, order-stable."""
    return [a for a in args if not a.startswith("-")]


def is_contributor(actor: str) -> bool:
    """Whether `actor` names a contributor seat (contrib/<name>) — the only seat allowed to
    publish to an external tracker (mirrors the seat prefixes in work.py). Public so the
    `contributor` module can gate its outbound-editor path on the SAME seat predicate."""
    return actor.startswith(_CONTRIB_PREFIX)


# Back-compat internal alias — earlier call sites used the underscored spelling.
_is_contributor = is_contributor


def _is_hq_native_write(args) -> bool:
    """True when the positional args (after the verb) contain an hq-prefixed bead id.

    An hq-prefixed id (e.g. ``hq-123``) signals a canonical control-plane write that belongs
    natively in the Factory HQ store — the one class of mutating write that is explicitly
    allowed through the hub guard even when the aggregate IS the HQ store. Product-hive ids
    (e.g. ``bc-123``) are not hq-native and remain refused.
    """
    positionals = _positionals(args)
    # positionals[0] is the verb; anything after may be a bead id
    return any(p.startswith(_HQ_ID_PREFIX) for p in positionals[1:])


def guard_hub(args) -> None:
    """Gate a bd invocation forwarded to the hub/HQ aggregate: allow read verbs (and a bare
    help/no-verb invocation) plus hq-native control-plane writes; refuse everything else with
    a pointer to the correct write paths.

    Allowlist (in priority order):
      1. No verb / ``--help`` invocations — let bd render its own help.
      2. Read verbs (list, ready, show, stats, search).
      3. HQ-native writes — positionals contain an hq-prefixed bead id (e.g. ``hq-123``).

    Everything else (product-hive bead ids, bare ``create``, etc.) raises ``typer.Exit(1)``."""
    positionals = _positionals(args)
    verb = positionals[0] if positionals else ""
    if not verb or verb in READ_VERBS:
        return
    if _is_hq_native_write(args):
        return  # hq-native control-plane write — allowed into the HQ store (the aggregate)
    typer.echo(
        f"✗ `{config.BINARY_ALIAS} hub bd {verb}` — the hub is a READ-ONLY cross-hive cache; "
        "a write here strands a bead (permanent orphan — sync is ADDITIVE, so it never "
        "self-heals).\n"
        f"  File a report with `{config.BINARY_ALIAS} report`, escalate a tool problem with "
        f"`{config.BINARY_ALIAS} escalate`, or create in the owning hive: "
        f"`{config.BINARY_ALIAS} --hive <hive> bd create`.",
        err=True,
    )
    raise typer.Exit(1)


def _github_issue_ids(args) -> list[str]:
    """Every id passed to `--issues` (repeated flag and/or comma-separated), order-stable."""
    ids: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--issues" and i + 1 < len(args):
            ids.extend(v for v in args[i + 1].split(",") if v)
            i += 2
            continue
        if a.startswith("--issues="):
            ids.extend(v for v in a[len("--issues=") :].split(",") if v)
        i += 1
    return ids


def publish_refusal(args, actor: str) -> str | None:
    """The pure decision behind :func:`guard_bd`: the refusal MESSAGE for an outward-publishing bd
    invocation (`github push`/`github sync`), or ``None`` when it is allowed. Returns ``None`` for
    every non-publish verb (nothing to gate).

    ONE decision, two callers (DRY): :func:`guard_bd` echoes + raises on a message, and the
    `contributor` seat's gated publish path reuses it so the write-guard is single-owned and the two
    can never disagree about who may publish or how (contributor seat + single-item only)."""
    positionals = _positionals(args)
    if len(positionals) < 2 or positionals[0] != "github":
        return None
    sub = positionals[1]
    if sub not in _PUBLISH_SUBVERBS:
        return None

    if not _is_contributor(actor):
        return (
            f"`bd github {sub}` is denied for seat {actor!r} — publishing to an external tracker "
            "is the contributor seat's job (contrib/<name>), behind a human publication gate.\n"
            f"  Stage the signal with `{config.BINARY_ALIAS} report` or escalate a tool problem "
            f"with `{config.BINARY_ALIAS} escalate`; the contributor files it upstream."
        )

    ids = _github_issue_ids(args)
    if sub != "push" or len(ids) != 1:
        return (
            "bare `bd github sync`/`push` is refused — bd has no sync-eligibility filter, so it "
            "would push local beads to a PUBLIC tracker.\n"
            "  The only safe publish is one bead at a time: `bd github push --issues <one-id>`."
        )
    return None


def guard_bd(args, actor: str) -> None:
    """Gate a raw bd invocation forwarded through `ws bd`. Only `github push`/`github sync` are
    guarded here (`create`/`import` are handled + allowed upstream; reads are harmless) — every
    other verb passes through untouched.

    A publish verb is denied for every seat except a contributor, and even a contributor may only
    take the gated single-item path (`bd github push --issues <one-id>`) — never a bare sync, and
    never more than one bead. Raises `typer.Exit(1)` on refusal (the decision is
    :func:`publish_refusal`)."""
    refusal = publish_refusal(args, actor)
    if refusal is not None:
        typer.echo(f"✗ {refusal}", err=True)
        raise typer.Exit(1)
