"""The ``bd`` read routes: one bead, a state dimension, a parent's rows, and the ready set.

These are the CLI-compatibility implementations the shell's ``beadhive.bd`` read helpers
(``show`` / ``state`` / ``child_rows`` / ``children``) forward to, and the ``bd ready`` reads
behind `bh work ready`, `bh work next`'s pick/claim loop and the local loop's poll — each the
``bd`` side of a route beadhive-core serves over the Beads API when a capable session opens
(``work.issue.get``, ``work.state.get``, ``work.swarm.inspect``, ``work.issue.list``,
``work.ready.list``, ``work.dispatch.poll``). Every function takes the caller's
:class:`~beadhive_bd_cli.transport.BdTransport` first.
"""

from __future__ import annotations

from typing import Any

from .transport import BdResult, BdTransport

__all__ = [
    "child_rows",
    "children",
    "has_parent_edge",
    "ready",
    "ready_rows",
    "show",
    "state",
    "status_snapshot",
]


def status_snapshot(bd: BdTransport, cwd: Any, *, timeout: float = 10.0) -> BdResult:
    """Read the HQ Beads database status with bounded, activity-free JSON argv."""
    return bd.run(["status", "--json", "--no-activity"], cwd, capture=True, timeout=timeout)


def show(bd: BdTransport, bead: Any, cwd: Any, *, strict: bool = False) -> dict | None:
    """The bead's JSON object (bd show may return a single object or a 1-list), or None.

    ``strict`` is forwarded to the transport's ``json`` (raise, rather than return None, when the
    ``bd`` binary itself is absent — see the shell's ``bd.BinaryMissing``)."""
    data = bd.json(["show", str(bead)], cwd, strict=strict)
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def state(bd: BdTransport, bead: Any, dim: str, cwd: Any) -> str:
    """Current value of a state dimension via `bd state <bead> <dim>` ('' if unset)."""
    res = bd.run(["state", str(bead), dim], cwd, capture=True)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def child_rows(
    bd: BdTransport,
    parent: Any,
    cwd: Any,
    extra: Any = None,
    *,
    include_closed: bool = False,
) -> list[dict] | None:
    """Rows selected by ``bd list --parent`` without imposing parent-edge membership.

    This is the shared query seam for two deliberately different consumers: molecule readers
    narrow the result to real parent edges in :func:`children`, while lifecycle-event readers
    retain bd's historical dotted-id stream even when an old event no longer carries that edge.
    ``include_closed`` owns the load-bearing ``--all`` spelling in one place: state-change event
    beads are born closed, so omitting it turns a populated history into a plausible empty list.

    Returns ``None`` on read failure and ``[]`` only for a successful, genuinely empty read.
    ``--limit 0`` keeps long histories from silently truncating at bd's default window.
    """
    flags = list(extra or [])
    if include_closed and "--all" not in flags:
        flags.append("--all")
    rows = bd.json(["list", "--parent", str(parent), "--limit", "0", *flags], cwd)
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


def children(
    bd: BdTransport,
    epic: Any,
    cwd: Any,
    extra: Any = None,
    *,
    include_closed: bool = False,
) -> list[dict] | None:
    """`bd list --parent <epic>` filtered to rows that are ACTUALLY children by the parent EDGE.

    bd resolves `--parent` by dotted-id PREFIX, not by the edge, so a bead deliberately detached
    from its epic still comes back on the strength of its id alone (bh-89mrf). That made
    re-parenting a no-op against every consumer of this list: `bhui-5mhu.3` reported `parent: None`
    and was absent from the reverse dep tree, yet still blocked `bh work finish bhui-5mhu` as an
    open child. Detaching a bead has to mean it stops gating the molecule.

    Trusting the edge is a strict narrowing of bd's answer — a real child carries it — so a row
    without it was never ours to count. bd states the edge TWO ways in one row: a top-level
    `parent`, and a `parent-child` entry in `dependencies` (the form `bd dep tree` walks). Either
    counts: `parent` is simply ABSENT from a parentless row (not null — measured: 439 of 723 rows
    in this hive carry no such key), so a reader that trusted only one representation would decide
    membership on which field bd happened to emit. Returns None on a bd read failure, keeping
    `json()`'s contract so callers can still tell "cannot list" from "no children".

    `--limit 0` defeats bd's default 50-row window. An epic with more than 50 children would
    otherwise under-report its open ones and `bh work finish` would land an INCOMPLETE molecule
    silently — the same window that already hid an open review gate from approve (bh-pwi2)."""
    rows = child_rows(bd, epic, cwd, extra, include_closed=include_closed)
    if not isinstance(rows, list):
        return None
    return [r for r in rows if has_parent_edge(r, str(epic))]


def has_parent_edge(row: dict, epic: str) -> bool:
    """True iff `row` carries a parent edge to `epic` in either representation bd emits."""
    if str(row.get("parent") or "") == epic:
        return True
    deps = row.get("dependencies")
    return isinstance(deps, list) and any(
        isinstance(d, dict)
        and d.get("type") == "parent-child"
        and str(d.get("depends_on_id") or "") == epic
        for d in deps
    )


def ready(bd: BdTransport, cwd: Any, args: Any = ()) -> BdResult:
    """`bd ready <args>`, captured — the byte-for-byte forward `bh work ready` renders."""
    return bd.run(["ready", *list(args)], cwd, capture=True)


def ready_rows(bd: BdTransport, cwd: Any, args: Any = ()) -> list | None:
    """`bd ready <args> --json`'s parsed rows, or None on failure (``--json`` is appended here;
    callers pass ``args`` without it)."""
    rows = bd.json(["ready", *[a for a in args if a != "--json"]], cwd)
    return rows if isinstance(rows, list) else None
