"""ws-layer write-guard for bd verbs forwarded through the hub and the `ws bd` passthrough.

bd has no notion of *where* it is safe to write, so ws gates two footguns bd will not protect
against itself:

  1. `ws hub bd create` (any mutating verb) mints a bead in the hub's READ cache — stranded as a
     permanent orphan. bd repo sync is ADDITIVE (empirically verified,):
     it imports source-rig beads alongside native ones, so a hub-native bead is *never* auto-wiped
     — it persists indefinitely with no source-rig home and no AGF workflow. The hub is a read-only
     cross-rig aggregate; only read verbs make sense there. We **allowlist** reads (simpler and
     safer than chasing a denylist of writes).

     Exception — hq-native (control-plane) writes: when the Factory HQ store IS the aggregate,
     writes that target an existing hq-prefixed bead (e.g. ``bd update hq-123``) are canonical
     control-plane operations and are explicitly allowed. A product-rig bead written directly into
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


def _positionals(args) -> list[str]:
    """The positional (non-flag) tokens of a bd arg vector, order-stable."""
    return [a for a in args if not a.startswith("-")]


def _is_contributor(actor: str) -> bool:
    """Whether `actor` names a contributor seat (contrib/<name>) — the only seat allowed to
    publish to an external tracker (mirrors the seat prefixes in work.py)."""
    return actor.startswith(_CONTRIB_PREFIX)


def _is_hq_native_write(args) -> bool:
    """True when the positional args (after the verb) contain an hq-prefixed bead id.

    An hq-prefixed id (e.g. ``hq-123``) signals a canonical control-plane write that belongs
    natively in the Factory HQ store — the one class of mutating write that is explicitly
    allowed through the hub guard even when the aggregate IS the HQ store. Product-rig ids
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

    Everything else (product-rig bead ids, bare ``create``, etc.) raises ``typer.Exit(1)``."""
    positionals = _positionals(args)
    verb = positionals[0] if positionals else ""
    if not verb or verb in READ_VERBS:
        return
    if _is_hq_native_write(args):
        return  # hq-native control-plane write — allowed into the HQ store (the aggregate)
    typer.echo(
        f"✗ `ws hub bd {verb}` — the hub is a READ-ONLY cross-rig cache; a write here strands a "
        "bead (permanent orphan — sync is ADDITIVE, so it never self-heals).\n"
        "  File a report with `ws report`, escalate a tool problem with `ws escalate`, "
        "or create in the owning rig: `ws -r <rig> bd create`.",
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


def guard_bd(args, actor: str) -> None:
    """Gate a raw bd invocation forwarded through `ws bd`. Only `github push`/`github sync` are
    guarded here (`create`/`import` are handled + allowed upstream; reads are harmless) — every
    other verb passes through untouched.

    A publish verb is denied for every seat except a contributor, and even a contributor may only
    take the gated single-item path (`bd github push --issues <one-id>`) — never a bare sync, and
    never more than one bead. Raises `typer.Exit(1)` on refusal."""
    positionals = _positionals(args)
    if len(positionals) < 2 or positionals[0] != "github":
        return
    sub = positionals[1]
    if sub not in _PUBLISH_SUBVERBS:
        return

    if not _is_contributor(actor):
        typer.echo(
            f"✗ `bd github {sub}` is denied for seat {actor!r} — publishing to an external tracker "
            "is the contributor seat's job (contrib/<name>), behind a human publication gate.\n"
            "  Stage the signal with `ws report` or escalate a tool problem with `ws escalate`; "
            "the contributor files it upstream.",
            err=True,
        )
        raise typer.Exit(1)

    ids = _github_issue_ids(args)
    if sub != "push" or len(ids) != 1:
        typer.echo(
            "✗ bare `bd github sync`/`push` is refused — bd has no sync-eligibility filter, so it "
            "would push local beads to a PUBLIC tracker.\n"
            "  The only safe publish is one bead at a time: `bd github push --issues <one-id>`.",
            err=True,
        )
        raise typer.Exit(1)
