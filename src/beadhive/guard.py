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

# `bd dolt <sub>` verbs that publish state already there or merely read remote config — never
# CREATE a bead, so the stranded-bead reasoning `guard_hub` exists for does not apply to them
# (bh-ohx2). `push`/`status` publish/inspect; `remote list` reads remote config. Deliberately
# narrow: `dolt remote add`/`remove` (repoints where a later push lands) and `dolt pull`/`sync`
# (would import THROUGH the aggregate, the same stranding risk under a different verb) stay
# refused — nothing HQ legitimately needs through this passthrough asks for them today; `bh hq
# init` already owns remote wiring.
_DOLT_PUBLISH_SAFE_SUBVERBS = frozenset({"push", "status"})

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


# Release plane (bh-k2j8): a `release-hold:` gate holds a `release:breaking` bead out of the current
# release window — filed at planning time when `release.enforce_hold` is on (plan.py), classified
# `release-hold` by `work_logic._gate_kind`, and (like every open gate) blocking the merge. Only a
# **releaser** seat may RESOLVE it, so a breaking change can't slip into a patch/minor window
# without the release owner's sign-off. Advisory ordering (release_order.py) is the soft
# counterpart; this gate is the hard one.
_RELEASER_PREFIX = "releaser/"

# A release-hold gate is identified by the `release-hold:` marker in its bd-gate reason (parallel to
# the security gate's `security:` marker), so it is distinguishable from review/kickoff/security.
RELEASE_HOLD_GATE_MARKER = "release-hold:"


def is_releaser(actor: str) -> bool:
    """Whether `actor` names a releaser seat (releaser/<name>) — the only seat allowed to resolve a
    release-hold: gate (mirrors the seat prefixes in work.py)."""
    return actor.startswith(_RELEASER_PREFIX)


def is_release_hold_gate(gate) -> bool:
    """True when a bd gate dict is a `release-hold:` gate — matched on the `release-hold:` marker in
    its reason/description (parallel to `is_security_gate`). Tolerant of the two bd shapes: a
    top-level `reason` field and the `reason: …` tail in `description`."""
    if not isinstance(gate, dict):
        return False
    reason = str(gate.get("reason") or "").lower()
    desc = str(gate.get("description") or "").lower()
    return RELEASE_HOLD_GATE_MARKER in reason or f"reason: {RELEASE_HOLD_GATE_MARKER}" in desc


def guard_release_hold_gate_resolution(gate, actor: str) -> None:
    """Release RBAC: only a releaser (releaser/<name>) may RESOLVE a `release-hold:` gate — so a
    breaking change can't be self-released into the wrong version window. A no-op for a
    non-release-hold gate and for a releaser actor; raises `typer.Exit(1)` when a non-releaser
    targets one."""
    if not is_release_hold_gate(gate) or is_releaser(actor):
        return
    gate_id = str(gate.get("id") or "?")
    typer.echo(
        f"✗ release-hold gate {gate_id} is releaser-only to resolve — {actor!r} is not a releaser "
        "(releaser/<name>).\n"
        "  The release-hold: gate holds a release:breaking change out of the current release "
        "window; only the releaser seat clears it for merge.",
        err=True,
    )
    raise typer.Exit(1)


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


# ---- multi-host exclusive primary (bh-ytbb.9) -----------------------------------------------
# The write gate for the multi-host model (docs/design/multi-host-model-adr.md, Decision 2 +
# Amendment 1): only the host holding a hive's **host lease** may run the write verbs.
#
# "Gate writes, never reads" is the ADR's rule and it is absolute here: `ready`, `list`, `show`,
# `brief` and `sync` are never routed through this function at all. Looking is always safe.
#
# The gated set is `bh work assign|claim|submit|merge` AND — the non-obvious, most important one
# — `bh plan file`. Creating children under a shared parent is *literally* the beads#4796
# trigger: two hosts each running `bd create --parent <epic>` before syncing allocate the SAME
# child id, and the next pull hits an add/add PK collision plus a both-changed `child_counters`
# conflict. Neither auto-resolves; sync blocks indefinitely. Planning from a follower is the
# known-broken path, not an edge case.
#
# This refusal is deliberately made STRUCTURALLY DISTINCT from every other failure a write verb
# can produce — in particular from bd's own post-merge close failure ("cannot close: assignee is
# dev/X, actor is <human>", bh-r8el), which shares the merge verb but nothing else. See
# `PRIMARY_REFUSAL_MARKER` and tests/test_guard_primary.py.
PRIMARY_REFUSAL_MARKER = "not the primary for"

# Verbs that MUST NOT be gated, kept as data so the "reads are never gated" rule is inspectable
# rather than implicit in which call sites happen to call guard_primary (asserted by test).
UNGATED_READ_VERBS = frozenset({"ready", "list", "show", "brief", "sync", "issue", "review"})


# Where the refused decision's lease VALUE came from. Load-bearing, not decoration (bh-sks7f):
# the cached read is the whole point of Amendment 1 §4, but a refusal that prints a cached
# holder as bare fact told an operator to "run the write on the host named above" when that host
# had been wiped hours earlier. The refusal now always says which it read.
SOURCE_HQ = "a fresh read of HQ just now"
SOURCE_CACHED = "the CACHED host lease in this host's HQ clone"
SOURCE_CACHED_OFFLINE = (
    "the CACHED host lease in this host's HQ clone — HQ could not be reached to refresh it, so "
    "it may be out of date"
)


def _refresh_expired(prefix: str, lease):
    """``(lease, source)`` a refusal should be decided from: an EXPIRED cached lease is re-read
    from HQ first and the local cache healed, a live one is returned untouched.

    Why only the expired leg. A live cached lease naming another host IS the split-brain window
    and must fail closed locally, offline, with no round trip — that is Amendment 1 §4 and the
    hot path. An EXPIRED one is the opposite case: the ADR's own model says "reassignment is
    lazy: the next host that wants the hive sees an expired lease", and until bh-sks7f nothing
    implemented that leg — the cache's only writer is this host's own won CAS, so a lease that
    moved on stayed invisible here forever. Re-reading costs one round trip on a path that was
    about to refuse anyway.

    ``lease is None`` in the result means HQ holds no lease at all — never adopted, nothing to
    gate."""
    if not lease.is_expired():
        return lease, SOURCE_CACHED

    from . import gitref, host_lease  # lazy: keep guard import-light + cycle-free

    try:
        return host_lease.refresh_cached("origin", prefix, cwd=config.hq_dir()), SOURCE_HQ
    except gitref.RemoteUnreachable:
        return lease, SOURCE_CACHED_OFFLINE


def _primary_refusal(hive_label: str, lease, source: str = SOURCE_CACHED) -> str:
    """The refusal text for a gated verb on a hive this host does not hold.

    Names the current holder and its expiry, because "you are not primary" without them leaves
    an operator with no next action — and names `source`, because those values can come from a
    cache this host has no other way to refresh. `lease` is a `host_lease.HostLease` or None.

    The remedy is split on whether anything actually holds the lease, so it is always
    actionable: a lapsed/absent lease is taken over HERE, and only a LIVE foreign lease can
    honestly say "run it on that host"."""
    free = lease is None or lease.is_tombstone or lease.is_expired()
    if lease is None or lease.is_tombstone:
        held = "nobody currently holds it (the host lease is released or was never taken)"
    elif free:
        held = (
            f"its last holder was {lease.describe()} — that lease has EXPIRED, so nobody holds it"
        )
    else:
        held = f"held by {lease.describe()}"
    remedy = (
        f"  Adopt this hive on THIS host to become primary:\n"
        f"      {config.BINARY_ALIAS} host adopt {hive_label}"
        if free
        else "  Adopt this hive on THIS host once that lease lapses, or run the write on the "
        "host named above."
    )
    return (
        f"✗ this host is {PRIMARY_REFUSAL_MARKER} {hive_label} — {held}.\n"
        f"  Writes (assign/claim/submit/merge, and `{config.BINARY_ALIAS} plan file`) are "
        f"restricted to the primary host; reads (ready/list/show/brief/sync) work from "
        f"anywhere.\n"
        f"  Decided from {source}.\n"
        f"{remedy}"
    )


def _not_primary(prefix: str, this_host: str, lease, *, verb: str, reason: str) -> str:
    """The shared not-held branch of :func:`guard_primary` and :func:`bd_write_refusal`: refresh
    a stale cache, log the escalation, and return the refusal text — or ``""`` to ALLOW, when
    the refresh proves HQ holds no lease at all or that this host holds it after all."""
    lease, source = _refresh_expired(prefix, lease)
    if lease is None or lease.held_by(this_host):
        return ""  # the cache was stale in this host's favour; the refresh just healed it

    from . import log  # lazy: keep guard free of the log import at load

    log.get_logger(__name__).warning(
        "primary_guard_refused",
        hive_prefix=prefix,
        verb=verb or "?",
        holder=lease.host_id or "",
        expires_at=lease.expires_at,
        lease_source=source,
        reason=reason,
    )
    return _primary_refusal(prefix, lease, source)


def primary_state(hive: str = "", *, cfg=None, hive_dir=None, entry=None):
    """``(prefix, this_host_id, lease)`` for `hive`, or ``None`` when the multi-host model is
    simply not in force for this call.

    The ONE resolution of "which hive, which host, which generation", shared by
    :func:`guard_primary` (does this host hold it?) and :func:`live_epoch` (which generation is
    in force?) so the two can never disagree about what they are reading.

    ``None`` — meaning *nothing to gate, allow* — covers every case where the question is not
    answerable or not applicable, each of which was an early ``return`` in ``guard_primary``
    before this was extracted:

      * the hive doesn't resolve (not this guard's error to raise);
      * the resolved dir isn't a registered hive / has no prefix (nothing holds a lease);
      * this host has no HQ clone (no lease store ⇒ nothing was ever adopted);
      * no lease is cached for the prefix (never adopted: single-host default).

    Reads the **cached** lease only — the local ``refs/bh/lease/<prefix>`` in this host's HQ
    clone, never HQ over the network (Amendment 1 §4; see :func:`guard_primary`).

    Two optional escape hatches for callers that already know more than a bare `hive` id can
    express — both bh-ytbb.12, which needs this same resolution from callers that are NOT a
    `bh work` verb dispatch and so have no ambient cwd to resolve against:

      * `hive_dir` — an explicit hive directory, skipping `registry.hive_dir_for`'s cwd-based
        resolution. The pre-push git hook's use case: it runs from wherever git happens to
        invoke it (a hive's own checkout, or — for the embedded Dolt engine's default storage
        shape — a git-transport bare repo nested under `.beads/`, which is neither a
        registered hive nor under the caller's cwd), and the hive dir is baked into the hook
        script at install time instead.
      * `entry` — an already-resolved `managed_repos` entry, skipping BOTH `hive_dir_for` and
        `entry_for_dir`. `bh doctor`'s per-hive warnings loop already has the entry in hand;
        re-deriving it from a directory would be redundant work for a call site that iterates
        every registered hive."""
    from . import host, host_lease, registry  # lazy: keep guard import-light + cycle-free

    cfg = cfg if cfg is not None else config.load()
    if entry is None:
        try:
            resolved_dir = hive_dir if hive_dir is not None else registry.hive_dir_for(cfg, hive)
            entry = registry.entry_for_dir(cfg, resolved_dir) or {}
        except Exception:  # noqa: BLE001 — an unresolvable hive is not this guard's error to raise
            return None
    prefix = str(entry.get("prefix") or "")
    if not prefix:
        return None  # a hive nowhere / unregistered dir: nothing to hold a lease on

    hq_dir = config.hq_dir()
    if not (hq_dir / ".git").exists():
        return None  # no HQ clone on this host -> no lease store -> nothing has been adopted

    lease = host_lease.read_cached(prefix, cwd=hq_dir)
    if lease is None:
        return None  # never adopted: single-host default, unchanged behavior

    try:
        this_host = host.host_id()
    except FileNotFoundError:
        this_host = ""  # no minted identity: cannot be the holder of anything

    return prefix, this_host, lease


def guard_primary(hive: str = "", *, cfg=None, verb: str = "") -> None:
    """Refuse a WRITE verb when this host is not `hive`'s primary (ADR Decision 2).

    The seam mirrors :func:`guard_hq_registry_write` / :func:`guard_hub`: one decision, called
    from each write verb, raising ``typer.Exit(1)`` on refusal and returning silently otherwise.

    Reads the **cached** host lease — the local ``refs/bh/lease/<prefix>`` in this host's HQ
    clone — not HQ over the network. Per Amendment 1 §4 an established primary must keep
    working while HQ is unreachable, so paying an HQ round trip on every ``bh work claim``
    would both slow the hot path and convert an HQ outage into a total write outage.

    **Allowed when no lease exists at all.** A factory that has never adopted anything is
    single-host by default and keeps working exactly as before; exclusive primary switches on
    when a host adopts, not when this code ships. An absent lease is "unconfigured", not
    "someone else's".

    Refused when: the lease names another host (whether or not it has expired), when it is a
    tombstone, or when this host's own lease has lapsed. The last one is fail-closed on
    purpose — a lapsed lease is exactly the window in which another host may have taken over,
    and writing through it is the split-brain path.

    `verb` is cosmetic (it appears in the log line); the decision never depends on it."""
    state = primary_state(hive, cfg=cfg)
    if state is None:
        return  # multi-host model not in force here (see `primary_state`)
    prefix, this_host, lease = state
    if lease.held_by(this_host):
        from . import host_lease  # lazy: keep guard import-light + cycle-free (see primary_state)

        # Opportunistic renewal (bh-ytbb.11): every gated write verb funnels through this one
        # call site, so it doubles as the renewal loop's body — "runs only while workers are
        # active" falls out for free, since an idle host simply never calls a write verb and
        # so never reaches this line. Best-effort and silent: it can only ever EXTEND this
        # host's own already-valid cached lease, never change the ALLOW decision this call is
        # already making, so an HQ-unreachable renewal failure here must not (and does not)
        # turn into a refusal. Renews at the un-scaled `host.lease.ttl` baseline rather than
        # re-deriving this host's role-scaled tenure (host_lease.ttl_for_role) — that would
        # need a manifest read on every gated write verb, and the baseline is a safe, cheap
        # default for "push the expiry a bit further out".
        hq_dir = config.hq_dir()  # same resolution primary_state() used to reach this lease
        host_lease.renew_if_due(
            "origin",
            prefix,
            host_id=this_host,
            cwd=hq_dir,
            ttl=config.host_lease_ttl(cfg),
            renew_interval=config.host_lease_renew_interval(cfg),
        )
        return

    refusal = _not_primary(
        prefix,
        this_host,
        lease,
        verb=verb,
        reason="write verb attempted on a hive this host does not hold the host lease for",
    )
    if not refusal:
        return
    typer.echo(refusal, err=True)
    raise typer.Exit(1)


# ---- the claim fencing token (bh-ytbb.10) ---------------------------------------------------
# `guard_primary` above asks "may this host write *right now*?". This asks the orthogonal
# question a long-running worker needs answered: "is the claim I have been holding for the last
# six hours still backed by the generation in force?" — the ADR's fencing-token check
# (Amendment 1, Consequences: "`ClaimRecord` carries the `epoch` it was minted under, as a
# fencing token").
#
# The two compose, and neither subsumes the other:
#   * the lease lapses and nobody re-adopts  -> `guard_primary` refuses (expired / foreign);
#   * the lease is lost and THIS host re-adopts (or recovers a bh-ytbb.8 half-state) -> the new
#     generation is live and `guard_primary` is satisfied, but every claim minted under the old
#     one is superseded. Only this check sees that. `host_lease.adopt` makes it detectable on
#     purpose: "the epoch ALWAYS advances, including when this host re-adopts its own live
#     lease … a fresh epoch invalidates every token minted under the old one".
#
# Structurally distinct from the `guard_primary` refusal (its own marker + its own log event),
# for the same reason that one is distinct from bd's post-merge close failure: an operator has
# to be able to tell the three apart to know what to do next.
STALE_CLAIM_REFUSAL_MARKER = "claim's fencing token is stale"


def live_epoch(hive: str = "", *, cfg=None) -> int:
    """The ADOPT generation currently in force for `hive`, or ``0`` when nothing has been
    adopted (an un-fenced, single-host factory).

    **Sourced from the cached host lease**, not from ``refs/bh/epoch`` on the hive's remote,
    and that is a deliberate choice with two reasons:

      1. *Cheap and local.* This is read on the claim hot path, and "workers must not poll" is
         the framing constraint. ``host_lease.read_cached`` is a local ref read in this host's
         HQ clone with no network at all — the same read ``guard_primary`` already does on
         every write verb, so this adds no round trip and no new failure mode. Reading the
         fence means ``ls-remote`` + a blob fetch against the hive's remote per call.
      2. *Same number.* The two-phase adopt (:mod:`beadhive.host_adopt`) installs the fence and
         records the lease at the **same** generation by construction — phase 2 passes phase
         1's `epoch` explicitly, precisely so they cannot drift — and ``renew`` holds the epoch
         fixed. So the cached lease's epoch IS the fence's epoch for any completed adopt.

    The fence remains the *enforcement* truth (Amendment 1 §2): its CAS is what makes the write
    itself safe, atomically, and no local reading can substitute for that. This check is the
    early, legible refusal at the bead-write boundary — it turns a token that a later fenced
    push would reject anyway into an actionable message *before* the worker's submit does any
    work. When ``fenced_push`` is wired into the real ``bd dolt push`` path (it ships standalone
    today, bh-ytbb.7), this function is the single place to upgrade the source."""
    state = primary_state(hive, cfg=cfg)
    return state[2].epoch if state is not None else 0


def _stale_claim_refusal(record, live: int, hive_label: str) -> str:
    """The refusal text for a claim whose fencing token has been superseded.

    Load-bearing content, not padding: the whole point of gating bead WRITES rather than code
    pushes (Amendment 1 §2) is that a refused submit leaves the work salvageable — so the
    refusal has to *say so*, and say how. An operator who reads "refused" and assumes the
    branch is stuck will do something destructive to recover it."""
    minted_on = f" on host {record.host_id}" if record.host_id else ""
    return (
        f"✗ {record.bead}: the {STALE_CLAIM_REFUSAL_MARKER} — it was claimed under epoch "
        f"{record.epoch}{minted_on}, but {hive_label} is now at epoch {live}. The host lease "
        f"was lost and re-adopted while this work was in flight, so the claim no longer "
        f"authorizes a bead write (ADR Amendment 1: a fresh epoch invalidates every token "
        f"minted under the old one).\n"
        f"  YOUR WORK IS NOT LOST. Only the BEAD write is gated, never the code — the branch "
        f"is still pushable exactly as it stands:\n"
        f"      git -C {record.worktree} push -u origin HEAD\n"
        f"  To recover, either:\n"
        f"    • re-adopt {hive_label} on THIS host, then re-ack and re-submit — the re-ack "
        f"re-stamps the token under the new epoch:\n"
        f"          {config.BINARY_ALIAS} work claim {record.bead} --as {record.seat}\n"
        f"          {config.BINARY_ALIAS} work submit {record.bead}\n"
        f"    • or push the branch and let the current primary land the bead updates under its "
        f"own epoch."
    )


def guard_claim_epoch(record, hive: str = "", *, cfg=None, verb: str = "") -> None:
    """Refuse a write verb when `record`'s fencing token predates the generation in force.

    `record` is the :class:`beadhive.claim_authority.ClaimRecord` `claim`/`resume` minted; a
    missing record (``None``) is not this guard's business — submit's existing seat check
    already owns "is this bead claimed at all", and duplicating that judgement here would be a
    second, differently-worded refusal for one condition.

    No-ops (allows) when nothing has been adopted, when the record predates bh-ytbb.10, and
    whenever the recorded epoch is still current — see :meth:`ClaimRecord.is_stale` for why
    unfenced records fail open.

    **Escalates** on refusal the same way :func:`guard_primary` does — a structured
    ``log.warning`` event on stderr naming the hive, the two epochs and the minting host,
    alongside the operator-facing message — so a fleet that is quietly churning its primary
    shows up in the log stream and not only in one worker's terminal."""
    if record is None or not record.is_fenced():
        return
    state = primary_state(hive, cfg=cfg)
    live = state[2].epoch if state is not None else 0
    if not record.is_stale(live):
        return
    # Staleness implies `live > record.epoch >= 1`, so a live epoch exists and `state` with it.
    hive_label = state[0]

    from . import log  # lazy: keep guard free of the log import at load

    log.get_logger(__name__).warning(
        "claim_fence_refused",
        hive_prefix=hive_label,
        verb=verb or "?",
        bead=record.bead,
        seat=record.seat,
        claim_host=record.host_id or "",
        claim_epoch=record.epoch,
        live_epoch=live,
        reason=(
            "claim fencing token superseded — the host lease was re-adopted while this work "
            "was in flight, so the bead write is refused (the branch is still pushable)"
        ),
    )
    typer.echo(_stale_claim_refusal(record, live, hive_label), err=True)
    raise typer.Exit(1)


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


def _is_dolt_publish_safe(args) -> bool:
    """True for ``bd dolt push`` / ``bd dolt status`` / ``bd dolt remote list`` — the
    read-only-safe and publish-only members of the ``bd dolt`` subcommand group (bh-ohx2).
    None of these CREATE anything, so the stranded-bead reasoning ``guard_hub`` exists for
    (bead creation in an additive cache is a permanent, unhealable orphan) does not apply."""
    positionals = _positionals(args)
    if not positionals or positionals[0] != "dolt":
        return False
    sub = positionals[1] if len(positionals) > 1 else ""
    if sub in _DOLT_PUBLISH_SAFE_SUBVERBS:
        return True
    return sub == "remote" and len(positionals) > 2 and positionals[2] == "list"


def guard_hub(args, *, label: str = "hq") -> None:
    """Gate a bd invocation forwarded to the hub/HQ aggregate: allow read verbs (and a bare
    help/no-verb invocation) plus hq-native control-plane writes plus publish-only/read-safe
    ``bd dolt`` verbs; refuse everything else with a pointer to the correct write paths.

    Allowlist (in priority order):
      1. No verb / ``--help`` invocations — let bd render its own help.
      2. Read verbs (list, ready, show, stats, search).
      3. HQ-native writes — positionals contain an hq-prefixed bead id (e.g. ``hq-123``).
      4. Publish-only/read-safe ``bd dolt`` verbs — push/status/remote-list (see
         :func:`_is_dolt_publish_safe`). ``bd dolt push`` publishes the store that's already
         there (the same write ``hq init``'s first-wiring path performs via
         ``engine.push_state``); it creates nothing, so it is not the footgun this guard exists
         to catch (bh-ohx2).

    Everything else (product-hive bead ids, bare ``create``, bare ``dolt pull``/``sync``, etc.)
    raises ``typer.Exit(1)``.

    ``label`` names the command surface the caller actually invoked — ``"hq"`` for
    ``bh hq bd …`` (the canonical, non-deprecated path) or ``"hub"`` for the deprecated
    ``bh hub bd …`` alias — so the refusal message names the real command instead of a
    hardcoded guess (bh-ohx2 also fixed the message mislabeling an ``hq`` invocation as
    ``hub``). Both surfaces route through this SAME guard and the SAME resolved store
    (``hub._aggregation_target()`` — HQ once one is registered, else the legacy hub); ``label``
    is cosmetic, not a second code path."""
    positionals = _positionals(args)
    verb = positionals[0] if positionals else ""
    if not verb or verb in READ_VERBS:
        return
    if _is_hq_native_write(args):
        return  # hq-native control-plane write — allowed into the HQ store (the aggregate)
    if _is_dolt_publish_safe(args):
        return  # publishes/reads state already there — creates nothing, no stranding risk
    typer.echo(
        f"✗ `{config.BINARY_ALIAS} {label} bd {verb}` — the hub is a READ-ONLY cross-hive "
        "cache; a write here strands a bead (permanent orphan — sync is ADDITIVE, so it never "
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
    :func:`publish_refusal`).

    The OTHER passthrough gate — "may this host write this hive at all?" — is
    :func:`guard_bd_write`, applied per target rather than once, since `-a`/`-r` fan out across
    hives that hold different leases."""
    refusal = publish_refusal(args, actor)
    if refusal is not None:
        typer.echo(f"✗ {refusal}", err=True)
        raise typer.Exit(1)


# ---- the passthrough's host-lease gate (bh-edvs) ---------------------------------------------
# `bh work claim` refuses when this host is not the hive's leased primary; `bh bd update --claim`
# — the same write, through the passthrough — did not. Closing that needs no new lock: the lease
# (`refs/bh/lease/<prefix>` in HQ), the predicate (`primary_state`, local-only) and the seam
# (this module, called from `bd.passthrough`) all already exist.
#
# READ ALLOWLIST, and everything else is treated as a write. Fail-closed is the correct
# direction here and the asymmetry is not close: an unknown verb wrongly classed as a write
# costs a spurious refusal ONLY on a hive some other host holds a lease for — which is never,
# until a second host adopts — while an unknown verb wrongly classed as a read is precisely the
# hole this exists to close, silently, forever. When bd grows a verb, the cost of forgetting it
# here is a legible refusal that names itself, not a bypass.
BD_READ_VERBS = frozenset(
    {
        "blocked",
        "children",
        "comments",
        "context",
        "count",
        "dep",  # `dep add` writes, but it is gated below by its own subcommand check
        "diff",
        "duplicates",
        "export",
        "find-duplicates",
        "graph",
        "help",
        "history",
        "lint",
        "list",
        "query",
        "ready",
        "search",
        "show",
        "stale",
        "state",
        "statuses",
        "status",
        "types",
        "version",
    }
)

# Read verbs whose own subcommands are not all reads — `dep list` reads, `dep add` writes. Only
# the listed subcommands stay reads; anything else under that verb is a write.
_BD_READ_SUBCOMMANDS = {"dep": frozenset({"list", "show", "tree"})}


def bd_verb(args) -> str:
    """The bd subcommand in `args`, ignoring leading global flags (`-C <dir>`, `--actor x`).
    Empty when there is none (a bare `bh bd`, or flags only)."""
    skip_value = False
    for token in args or []:
        if skip_value:
            skip_value = False
            continue
        if token.startswith("-"):
            skip_value = token in ("-C", "--actor", "--db", "-r", "--repo")
            continue
        return token
    return ""


# ---- the intake tier (bh-lkbas) --------------------------------------------------------
# FILING is not EXECUTING, and only one of the two can collide.
#
# The lease exists to serialize writes to a shared bead store, and the ADR is specific about
# WHICH hazard it serializes — beads#4796: "two hosts each running `bd create --parent
# <epic>` before syncing allocate the SAME child id, and the next pull hits an add/add PK
# collision plus a both-changed `child_counters` conflict. Neither auto-resolves."
#
# That hazard is a property of the CHILD-ID COUNTER, not of bead creation as such. A
# parentless `bd create` mints a RANDOM top-level id (bd's own help spells one: `--parent
# bd-a3f8e9`), so two hosts filing concurrently allocate DIFFERENT ids and the merge is an
# additive union — no PK collision, and no counter to conflict over. Allowing this one
# operation shape from a non-primary host therefore does not reintroduce the condition the
# lease exists to prevent, which is the soundness question bh-lkbas insists be answered
# before any tier is built rather than after.
#
# SCOPE — the narrowest thing that answers the need (file a bug or feature request from a
# laptop while the executor owns execution for the hive):
#     allowed   `bd create` with NEITHER --parent NOR --id
#     gated     everything else, unchanged: assign/claim/submit/merge, `bh plan file`, any
#               update to an EXISTING bead, and a parented or explicitly-id'd create
#
# `--id` is disqualifying for exactly the same reason `--parent` is: an operator-chosen id is
# precisely the collision a random one cannot be. ROLE is deliberately not consulted — a
# `viewer` laptop filing a bug is the motivating case, and roles already govern the only thing
# they should (whether a host may hold the lease at all, via `host_lease.ttl_for_role`).
_INTAKE_VERB = "create"
_INTAKE_DISQUALIFYING_FLAGS = ("--parent", "--id")


def is_intake_create(args) -> bool:
    """True for a standalone ``bd create`` — a new TOP-LEVEL bead with a randomly-minted id,
    the one write shape that cannot collide across hosts and so needs no lease (bh-lkbas).

    False for a parented or explicitly-id'd create, both of which can collide and stay gated.
    Matches ``--flag value`` and ``--flag=value`` spellings alike."""
    if bd_verb(args) != _INTAKE_VERB:
        return False
    return not any(
        a == flag or a.startswith(flag + "=")
        for a in (args or [])
        for flag in _INTAKE_DISQUALIFYING_FLAGS
    )


def is_bd_write(args) -> bool:
    """Whether `args` names a bd verb that could MUTATE the hive in a way the host lease has
    to serialize — see :data:`BD_READ_VERBS` for why an unknown verb counts as a write, and
    :func:`is_intake_create` for the one write deliberately left ungated."""
    verb = bd_verb(args)
    if not verb:
        return False  # bare `bh bd` / flags only: bd prints help, writes nothing
    if is_intake_create(args):
        return False  # filing a fresh top-level bead cannot collide: no lease required
    if verb not in BD_READ_VERBS:
        return True
    subs = _BD_READ_SUBCOMMANDS.get(verb)
    if subs is None:
        return False
    rest = list(args)[list(args).index(verb) + 1 :]
    return bd_verb(rest) not in subs


def bd_write_refusal(args, cwd, *, cfg=None) -> str:
    """The refusal text for a passthrough WRITE verb this host may not run against `cwd`'s
    hive, or ``""`` to allow — the same decision, predicate and wording `bh work`'s own write
    verbs get (:func:`guard_primary` / :func:`_primary_refusal`), applied at the one seam where
    bh sees a bd-shaped invocation before it happens.

    Returns rather than raising, unlike :func:`guard_primary`, because it is called PER TARGET
    from `bd.passthrough`'s fan-out: under `-a`/`-r` each target is a different hive with its
    own lease, and a refusal must fail THAT hive while the rest still run. `route.fan_out`
    already turns a non-zero per-target result into exactly that, and into a plain exit 1 for a
    single-target run — so returning the text preserves both behaviours without a second
    convention.

    **Honest limit, stated the way `prepush.py` states its own:** this gates `bh bd`, not a
    genuinely raw `bd` — nothing in bh can. It is early, legible failure. The enforcement is
    still the epoch fence beside the data at push time (`host_fence.py`, `bh-ukit.2`)."""
    if not is_bd_write(args):
        return ""
    # `passthrough` deliberately passes an EMPTY cfg in cwd mode (the common case) to skip a
    # config load on a plain forward. An empty cfg has no `managed_repos`, so `primary_state`
    # resolves no hive, returns None, and the gate silently allows everything — which is the
    # bug this whole bead exists to close, reintroduced one layer down. Load it here instead:
    # only writes pay for it, and reads (the hot path) still skip it entirely.
    cfg = cfg if cfg else config.load()
    state = primary_state(cfg=cfg, hive_dir=cwd)
    if state is None:
        return ""  # multi-host model not in force here (see `primary_state`)
    prefix, this_host, lease = state
    if lease.held_by(this_host):
        return ""
    return _not_primary(
        prefix,
        this_host,
        lease,
        verb=f"bd {bd_verb(args)}",
        reason="bd write verb forwarded through the passthrough for a hive this host does not "
        "hold the host lease for",
    )
