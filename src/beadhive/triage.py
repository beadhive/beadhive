"""Intake triage surface — the source-agnostic queue + type-aware dispositions (epic
, bead).

The hive manager needs incoming reports *visible* and *disposable*, not buried in backlog. This
module is the triage/dispose side of the funnel `ws report` (bead) fills:

  * **Source-agnostic queue.** A report lands as `intake:untriaged` (the shared vocabulary in
    `ws/state.py`, bead) regardless of where it came from. Queue MEMBERSHIP is
    the `intake:untriaged` state; the intake CHANNEL is the closed `origin` dimension (`report` |
    `github` | `import`, bead) — reports carry an explicit `origin:report` label,
    while imported beads (github / legacy) derive their channel
    from the native `source_system` on read via `state.channel_of`. So cross-hive reports and both
    imports share ONE queue. `list_intake` keys on the intake label (the source-agnostic part) and
    optionally narrows to one resolved `origin` channel client-side — NOT raw `source_system`.

  * **Dedup on entry + at triage.** `find_dupes` reuses the beads-native `bd find-duplicates`
    (mechanical/AI) — we surface likely dupes rather than reimplement dedup. Feature requests
    especially collide with existing backlog.

  * **Type-aware dispositions.** `accept` (set type/priority, clear intake -> backlog), `reject`
    (close with a reporter-visible reason), `reroute` (re-file a mis-routed report into the right
    hive, or bounce it to the superintendent), `promote` (hand to the planner — the adopt path is
    the sibling bead; here we only hand off). Each clears the intake dimension via
    an event-sourced `bd set-state` transition to a terminal value (`intake:accepted` etc.), never
    a silently-yanked label — consistent with.

All writes go through sanctioned beads-native primitives (`bd -C … update|close|set-state`, and
`ws report` for reroute) with `--actor` provenance — never the guarded `bd github push/sync`.
"""

from __future__ import annotations

import json

import typer

from . import bd
from .state import (
    INTAKE_UNTRIAGED,
    ORIGIN_DIM,
    STATE_DIMENSIONS,
    channel_of,
    disposition_state,
    is_untriaged_intake,
)

# The intake CHANNEL values that share the ONE intake queue — the funnel key that makes the surface
# source-agnostic. This is the closed `origin` dimension, NOT raw
# `source_system` (reports ride `origin:report`; imports derive their channel from `source_system`
# on read). `bd find-duplicates` / triage never branch on the channel.
CHANNELS = STATE_DIMENSIONS[ORIGIN_DIM]


def _channel_of(row) -> str:
    """The resolved intake CHANNEL of a bead row — the closed `origin` dimension
    , NOT raw `source_system`. Reports carry an explicit `origin:` label;
    imported beads derive their channel from the native `source_system` on read. `state.channel_of`
    resolves both (label-first). '' when neither yields a known channel."""
    return channel_of(row.get("labels"), row.get("source_system")) or ""


# ---- queue ------------------------------------------------------------------


def list_intake(cwd, source: str = ""):
    """Untriaged intake beads for a hive, source-agnostic (keyed on the `intake:untriaged` label so
    any channel — report|github|import — shares one queue). `source` narrows to one resolved
    `origin` channel client-side (bd has no channel list filter). Returns a list of bead rows (empty
    on read failure)."""
    rows = bd.json(["list", "--label", INTAKE_UNTRIAGED, "--status", "open"], cwd) or []
    if not isinstance(rows, list):
        return []
    if source:
        rows = [r for r in rows if _channel_of(r) == source]
    return rows


# ---- dedup (reuse bd find-duplicates; never reimplement) ---------------------


def find_dupes(cwd, threshold: float = 0.5, method: str = "mechanical"):
    """Likely-duplicate pairs across a hive's open issues via the beads-native `bd find-duplicates`
    (mechanical by default — no API key; `ai` for semantic). Returns the list of pair dicts
    (`issue_a_id`, `issue_b_id`, `similarity`, …), empty on read failure."""
    data = bd.json(
        ["find-duplicates", "--threshold", str(threshold), "--method", method], cwd
    )
    if not isinstance(data, dict):
        return []
    pairs = data.get("pairs")
    return pairs if isinstance(pairs, list) else []


def dupes_touching(pairs, ids):
    """The subset of `pairs` where either side is one of `ids` — i.e. a dupe involving a bead we
    care about (a fresh report on entry, or the intake queue at triage)."""
    wanted = set(ids)
    return [p for p in pairs if p.get("issue_a_id") in wanted or p.get("issue_b_id") in wanted]


def intake_payload(cwd, source: str = "", threshold: float = 0.5) -> dict:
    """Build the {rows, dupes} payload for the intake inbox.

    Same dict `ws work intake --json` emits. Backed by `list_intake` + `find_dupes`
    (via `dupes_touching`). Safe to call from sync contexts."""
    rows = list_intake(cwd, source)
    ids = [r.get("id") for r in rows]
    pairs = dupes_touching(find_dupes(cwd, threshold=threshold), ids)
    return {"rows": rows, "dupes": pairs}


# ---- dispositions (type-aware) ----------------------------------------------


def _require_untriaged(bead, cwd):
    """(data, error): the bead's JSON, plus an error message if it isn't an untriaged intake bead
    (so a disposition never re-triages an already-fielded or non-intake bead)."""
    data = bd.show(bead, cwd)
    if data is None:
        return None, f"{bead} not found"
    if not is_untriaged_intake(data.get("labels")):
        return data, f"{bead} is not an untriaged intake bead — nothing to triage"
    return data, ""


def _clear_intake(bead, cwd, actor, disposition, reason):
    """Event-sourced clear: transition the intake dimension to the disposition's terminal value
    (`bd set-state`, consistent with). Returns (exit, error)."""
    value = disposition_state(disposition)
    res = bd.run(
        ["set-state", bead, f"intake={value}", "--reason", reason], cwd, actor, capture=True
    )
    if res.returncode:
        return res.returncode, f"could not clear intake state: {bd.err_line(res)}"
    return 0, ""


def accept(cwd, bead, actor, issue_type: str = "", priority: str = ""):
    """Accept a report into backlog: set type/priority (type-aware — both optional), then clear the
    intake state. The bead stays open as normal backlog. Returns (exit, error, message)."""
    _data, err = _require_untriaged(bead, cwd)
    if err:
        return 1, err, ""
    update = []
    if issue_type:
        update += ["--type", issue_type]
    if priority:
        update += ["--priority", priority]
    if update:
        res = bd.run(["update", bead, *update], cwd, actor, capture=True)
        if res.returncode:
            return res.returncode, f"bd update failed: {bd.err_line(res)}", ""
    code, err = _clear_intake(bead, cwd, actor, "accept", "accepted into backlog")
    if err:
        return code, err, ""
    detail = f" ({', '.join(update[i] for i in range(1, len(update), 2))})" if update else ""
    return 0, "", f"✓ accepted {bead} → backlog{detail}"


def reject(cwd, bead, actor, reason: str):
    """Reject a report: clear intake, then close it with a reporter-visible reason. Returns
    (exit, error, message)."""
    if not reason:
        return 1, "reject requires a --reason (reporter-visible)", ""
    _data, err = _require_untriaged(bead, cwd)
    if err:
        return 1, err, ""
    code, err = _clear_intake(bead, cwd, actor, "reject", f"rejected: {reason}")
    if err:
        return code, err, ""
    res = bd.run(["close", bead, "--reason", reason], cwd, actor, capture=True)
    if res.returncode:
        return res.returncode, f"bd close failed: {bd.err_line(res)}", ""
    return 0, "", f"✓ rejected {bead}: {reason}"


def reroute(cwd, bead, actor, to_hive: str = "", superintendent: str = "", cfg=None):
    """Re-file a mis-routed report, type-aware. Exactly one destination:

    * `to_hive` — re-file into the right hive (reusing `ws report`, so provenance + intake are
      re-stamped in the target), then close the original as rerouted.
    * `superintendent` — bounce to the superintendent: reassign the bead to their seat and LEAVE it
      as untriaged intake, so it stays in the fleet-wide inbox (`ws hub intake`) for them to route.

    Returns (exit, error, message)."""
    if bool(to_hive) == bool(superintendent):
        return 1, "reroute needs exactly one of --to <hive> or --super <seat>", ""
    data, err = _require_untriaged(bead, cwd)
    if err:
        return 1, err, ""

    if superintendent:
        res = bd.run(["assign", bead, superintendent], cwd, actor, capture=True)
        if res.returncode:
            return res.returncode, f"bounce to superintendent failed: {bd.err_line(res)}", ""
        return 0, "", f"✓ bounced {bead} → {superintendent} (stays in the fleet-wide inbox)"

    from . import report

    title = str(data.get("title") or "")
    rtype = str(data.get("issue_type") or "bug")
    rtype = rtype if rtype in report.REPORT_TYPES else "bug"  # type-aware, bd-valid fallback
    code, err, new_id = report.file_report(to_hive, title, rtype, actor, cfg=cfg)
    if err:
        return code, f"reroute re-file failed: {err}", ""
    reason = f"rerouted to {to_hive} as {new_id}"
    code, cerr = _clear_intake(bead, cwd, actor, "reroute", reason)
    if cerr:
        return code, cerr, ""
    res = bd.run(["close", bead, "--reason", reason], cwd, actor, capture=True)
    if res.returncode:
        return res.returncode, f"bd close failed: {bd.err_line(res)}", ""
    return 0, "", f"✓ rerouted {bead} → {to_hive} ({new_id})"


def promote(cwd, bead, actor):
    """Hand a report to the planner: transition intake to `promoted` — the queue the planner's
    adopt path reads. This is a clean HAND-OFF only; adopting the report into a
    gated epic molecule is jf5k's job, NOT done here. Returns (exit, error, message)."""
    _data, err = _require_untriaged(bead, cwd)
    if err:
        return 1, err, ""
    code, err = _clear_intake(bead, cwd, actor, "promote", "handed to the planner for adoption")
    if err:
        return code, err, ""
    return 0, "", f"✓ promoted {bead} → planner"


# ---- rendering (ws work intake) ---------------------------------------------


def _dupe_note(pairs, bead_id) -> str:
    """A one-line ' likely dup of <ids>' suffix for a bead with surfaced duplicates ('' if none)."""
    others = []
    for p in pairs:
        if p.get("issue_a_id") == bead_id:
            others.append(p.get("issue_b_id"))
        elif p.get("issue_b_id") == bead_id:
            others.append(p.get("issue_a_id"))
    others = [o for o in others if o]
    return f"  ⚠ likely dup of {', '.join(others)}" if others else ""


def print_intake(cwd, source: str = "", dupes: bool = True, as_json: bool = False, threshold=0.5):
    """Render the untriaged intake queue for a hive. With `dupes`, annotate each row that
    `bd find-duplicates` flags as a likely duplicate. `as_json` emits `{rows, dupes}` for a machine
    consumer. Prints via typer and raises no exit on an empty queue."""
    payload = intake_payload(cwd, source, threshold=threshold)
    rows = payload["rows"]
    pairs = payload["dupes"] if dupes else []

    if as_json:
        typer.echo(json.dumps({"rows": rows, "dupes": pairs}))
        return

    label = f" ({source})" if source else ""
    if not rows:
        typer.echo(f"✓ no untriaged intake{label} — the queue is clear")
        return
    typer.echo(f"untriaged intake{label}: {len(rows)}")
    for r in rows:
        src = _channel_of(r) or "?"
        typer.echo(
            f"  {r.get('id')}  [{r.get('issue_type', '?')}/{src}]  {r.get('title', '')}"
            + _dupe_note(pairs, r.get("id"))
        )
    typer.echo("  dispose: bh work accept|reject|reroute|promote <id>")
