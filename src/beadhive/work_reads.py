"""Read-only ``bh work`` producers and command implementations.

The public command names remain registered and importable from :mod:`beadhive.work`.  This module
owns forwarding, readiness payload construction, truncation handling, and release-aware ordering;
all payload schemas, stream bytes, ordering, telemetry, and exit codes are compatibility contracts.

``bh work ready --json`` now selects the ``work.ready.list`` API route (bh-p76tk.1, finishing what
bh-mu5yb.1 built and byte-proved but did not wire: see ``packages/beadhive-core/README.md``'s
"bh work ready" section) for exactly the one shape that can be reproduced byte-for-byte without a
second-guess: an UNBOUNDED read (``--limit 0``, explicit or auto-widened by
:func:`widen_narrowed_ready_args` for any narrowing flag) whose narrowing flags all have a typed
`QueueCommands.list_ready` equivalent (see :func:`_api_ready_kwargs`). Every other shape stays a
named CLI-compatibility route, selected before execution, never as a retry after an API call:
human (non-``--json``) output (bd's own terminal rendering has no typed-data equivalent),
``--mol``/``--mol-type`` narrowing (genuinely no HTTP equivalent), ``--gated`` (composes
`release_order.merge_sequence` locally over whatever rows it is handed — orthogonal to their
source, not re-plumbed here), and a CAPPED read (bd's own default limit, or an explicit non-zero
``--limit``): :class:`beadhive_beads_client.ReadyPage` carries no total-count field to reproduce
bd's own "Showing X of Y ready issues" truncation notice byte-for-byte, so a capped read is never
attempted over the API at all. :func:`beadhive.work_queue.open_ready` is the composition seam
(mirroring :func:`beadhive.work_queue.claim_next` / :func:`beadhive.work_queue.open_children`
exactly): it resolves the hive's one supervised Beads v1.3 service and reports ``None`` to mean
"select the CLI-compatibility forward instead" whenever the service or a required capability is
genuinely unavailable, decided before any Beads read is attempted. ``bh work schedule``'s children
fetch, which already threads an explicit ``entry``/``main`` through
:func:`beadhive.worktree.locate`, is routed the same way — see
:func:`beadhive.work_dispatch.impl_schedule_payload`.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import typer

from . import bd, bd_cli, otel, registry, release_order, work_guards, work_queue, worktree
from . import schedule as schedule_mod
from .config_consumer_ports import work_settings as config

READ_CTX = {"allow_extra_args": True, "ignore_unknown_options": True}
READY_LIMIT_FLAGS = {"-n", "--limit"}
READY_NARROWING_FLAGS = {
    "-l",
    "--label",
    "--label-any",
    "--exclude-label",
    "-t",
    "--type",
    "--exclude-type",
    "-p",
    "--priority",
    "-a",
    "--assignee",
    "-u",
    "--unassigned",
    "--parent",
    "--mol",
    "--mol-type",
    "--has-metadata-key",
    "--metadata-field",
}
READY_SHOWING_RE = re.compile(r"Showing (\d+) of (\d+) ready issues")
READY_TRUNCATED_EXIT = 3

#: `bh work ready` narrowing flags mapped to their `QueueCommands.list_ready` keyword — every flag
#: in `READY_NARROWING_FLAGS` except `--mol`/`--mol-type`, which genuinely have no HTTP equivalent
#: (see `packages/beadhive-core/README.md`'s "bh work ready" table) and so are absent here on
#: purpose: :func:`_api_ready_kwargs` treats an unmapped flag as "select CLI instead".
_READY_FLAG_KWARG = {
    "-l": "label",
    "--label": "label",
    "--label-any": "label_any",
    "--exclude-label": "exclude_label",
    "-t": "type_",
    "--type": "type_",
    "--exclude-type": "exclude_type",
    "-p": "priority",
    "--priority": "priority",
    "-a": "assignee",
    "--assignee": "assignee",
    "--parent": "parent",
    "--has-metadata-key": "has_metadata_key",
    "--metadata-field": "metadata_field",
}
#: Repeatable narrowing flags: every occurrence accumulates into one list kwarg.
_READY_MULTI_KWARGS = frozenset(
    {"label", "label_any", "exclude_label", "exclude_type", "metadata_field"}
)
#: `-u`/`--unassigned` takes no value — a bare boolean flag, unlike every other narrowing flag.
_READY_BOOL_FLAGS = frozenset({"-u", "--unassigned"})


class MoleculeReadinessError(Exception):
    """A molecule-readiness read failed or returned an unusable shape."""


def forward_read(sub_args, cwd):
    emit_forward(bd_cli.routes(cwd).forward(sub_args))


def emit_forward(result):
    """Write a captured ``bd`` forward's output verbatim and exit with its code."""
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    raise typer.Exit(result.returncode)


def brief(bead: str, hive: str = ""):
    otel.set_bead(bead)
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    work_guards.print_brief(cfg, entry, bead, bd.show(bead, main))


def reorder_ready_lines(text: str, ordered_ids) -> str:
    positions = {bead: index for index, bead in enumerate(ordered_ids)}
    lines = text.splitlines(keepends=True)

    def id_of(line: str):
        return next((token for token in line.split() if token in positions), None)

    slots = [index for index, line in enumerate(lines) if id_of(line) is not None]
    reordered = sorted((lines[index] for index in slots), key=lambda line: positions[id_of(line)])
    for slot, line in zip(slots, reordered, strict=True):
        lines[slot] = line
    return "".join(lines)


def count_avoided_conflicts(beads, order, estimator, strategy) -> None:
    positions = {bead: index for index, bead in enumerate(order)}
    for first, second in zip(beads, beads[1:], strict=False):
        verdict = release_order.start_verdict(second, [first], estimator=estimator)
        if verdict.likelihood < schedule_mod.DEFERRAL_LIKELIHOOD:
            continue
        first_index = positions.get(str(first.get("id") or ""))
        second_index = positions.get(str(second.get("id") or ""))
        if first_index is None or second_index is None or abs(first_index - second_index) == 1:
            continue
        otel.record_conflict_avoided({"bh.release.strategy": strategy})


def forward_ready_ordered(args, cwd, strategy, fix_churn_budget, estimator) -> None:
    beads = bd_cli.ready_rows(cwd, args)
    if not isinstance(beads, list) or not beads:
        emit_forward(bd_cli.ready(cwd, args))
        return
    order = release_order.merge_sequence(
        beads, strategy=strategy, fix_churn_budget=fix_churn_budget
    )
    count_avoided_conflicts(beads, order, estimator, strategy)
    positions = {bead: index for index, bead in enumerate(order)}
    if "--json" in args:
        ordered = sorted(
            beads, key=lambda bead: positions.get(str(bead.get("id") or ""), len(order))
        )
        sys.stdout.write(json.dumps(ordered, indent=2) + "\n")
        return
    result = bd_cli.ready(cwd, args)
    if result.stdout:
        sys.stdout.write(reorder_ready_lines(result.stdout, order))
    if result.stderr:
        sys.stderr.write(result.stderr)
    raise typer.Exit(result.returncode)


def readiness_json(args, cwd):
    result = bd_cli.routes(cwd).json_forward(args)
    if result.returncode != 0:
        raise MoleculeReadinessError(bd.err_detail(result))
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise MoleculeReadinessError(f"invalid JSON from bd {' '.join(args)}") from exc


def molecule_readiness_payload(molecule: str, cwd) -> dict:
    children = readiness_json(["show", molecule, "--children"], cwd)
    members = children.get(molecule) if isinstance(children, dict) else None
    if not isinstance(members, list) or not all(isinstance(row, dict) for row in members):
        raise MoleculeReadinessError(f"cannot read molecule {molecule}")

    explained = readiness_json(["ready", "--include-ephemeral", "--explain", "--limit", "0"], cwd)
    ready_rows = readiness_json(["ready", "--include-ephemeral", "--limit", "0"], cwd)
    if not isinstance(explained, dict) or not isinstance(ready_rows, list):
        raise MoleculeReadinessError("bd ready returned an unexpected shape")
    ready_ids = {
        str(row.get("id") or "") for row in ready_rows if isinstance(row, dict) and row.get("id")
    }
    blocked = {
        str(row.get("id") or ""): row
        for row in explained.get("blocked", [])
        if isinstance(row, dict) and row.get("id")
    }
    steps = []
    for row in members:
        step_id = str(row.get("id") or "")
        status = str(row.get("status") or "")
        blockers = blocked.get(step_id, {}).get("blocked_by", [])
        if status == "closed":
            readiness = "done"
        elif step_id in blocked:
            readiness = "blocked"
        elif step_id in ready_ids:
            readiness = "ready"
        elif status and status != "open":
            readiness = status
        else:
            readiness = "pending"
        steps.append(
            {
                "id": step_id,
                "title": str(row.get("title") or ""),
                "status": status,
                "readiness": readiness,
                "blocked_by": blockers if isinstance(blockers, list) else [],
            }
        )
    return {"molecule": molecule, "steps": steps}


def render_molecule_readiness(payload: dict) -> None:
    typer.echo(f"Molecule {payload['molecule']}")
    for step in payload["steps"]:
        typer.echo(f"  [{step['readiness']}] {step['id']}: {step['title']}")
        for blocker in step["blocked_by"]:
            typer.echo(
                f"    ← blocked by {blocker.get('id', '?')}: "
                f"{blocker.get('title', '')} [{blocker.get('status', 'unknown')}]"
            )


def readiness(molecule: str, hive: str = "", as_json: bool = False):
    otel.set_bead(molecule)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    try:
        payload = molecule_readiness_payload(molecule, cwd)
    except MoleculeReadinessError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
    else:
        render_molecule_readiness(payload)


def ready_arg_name(token: str) -> str:
    return token.split("=", 1)[0]


def ready_has_flag(args, names) -> bool:
    return any(ready_arg_name(arg) in names for arg in args)


def widen_narrowed_ready_args(args: list[str]) -> list[str]:
    if ready_has_flag(args, READY_LIMIT_FLAGS):
        return args
    if not ready_has_flag(args, READY_NARROWING_FLAGS):
        return args
    return [*args, "-n", "0"]


def ready_truncated_exit(args, result, *, as_json: bool) -> int:
    if result.returncode != 0 or ready_has_flag(args, READY_LIMIT_FLAGS):
        return result.returncode
    haystack = result.stderr if as_json else result.stdout
    match = READY_SHOWING_RE.search(haystack or "")
    if not match or match.group(1) == match.group(2):
        return result.returncode
    if as_json:
        return READY_TRUNCATED_EXIT
    typer.echo(f"⚠ {match.group(0)} — pass -n 0 for the full list", err=True)
    return result.returncode


def _api_ready_kwargs(args: list[str]) -> dict[str, Any] | None:
    """Translate `bh work ready`'s CLI argv into `QueueCommands.list_ready` kwargs, or ``None`` to
    mean "select the CLI-compatibility route instead" — decided from the parsed args alone,
    before any Beads call is attempted, never as a retry after one.

    ``None`` whenever any token is not one of ``--json``, one of the limit flags, or one of
    :data:`_READY_FLAG_KWARG`'s narrowing flags — in particular `--mol`/`--mol-type` (no HTTP
    equivalent) and every other `bd ready` flag `bh` forwards verbatim today (`--sort`,
    `--explain`, `--brief`, `--claim`, `--plain`, `--pretty`, `--max-rows`,
    `--include-ephemeral`, `--include-deferred`, `--label-pattern`, `--label-regex`, any `bd`
    global flag). ``--gated`` is accepted here (skipped, like ``--json``) only because callers
    never pass it in practice — `ready()` only calls this on the non-``--gated`` branch — kept
    tolerant rather than a caller-ordering trap.
    """
    kwargs: dict[str, Any] = {}
    multi: dict[str, list[str]] = {}
    i = 0
    n = len(args)
    while i < n:
        token = args[i]
        name, has_eq, inline = token.partition("=")
        if name in ("--json", "--gated"):
            i += 1
            continue
        if name in _READY_BOOL_FLAGS:
            kwargs["unassigned"] = True
            i += 1
            continue
        if has_eq:
            value = inline
            i += 1
        else:
            if i + 1 >= n:
                return None
            value = args[i + 1]
            i += 2
        if name in READY_LIMIT_FLAGS:
            try:
                kwargs["limit"] = int(value)
            except ValueError:
                return None
            continue
        kw = _READY_FLAG_KWARG.get(name)
        if kw is None:
            return None
        if kw == "priority":
            try:
                kwargs["priority"] = int(value)
            except ValueError:
                return None
        elif kw in _READY_MULTI_KWARGS:
            multi.setdefault(kw, []).append(value)
        else:
            kwargs[kw] = value
    for key, values in multi.items():
        kwargs[key] = values
    return kwargs


def ready_via_api(cwd, entry, args: list[str]) -> bool:
    """Attempt the `work.ready.list` route for an UNBOUNDED `bh work ready --json` read
    (bh-p76tk.1) — pre-execution selection, decided from ``args`` alone before any Beads call:

    * :func:`_api_ready_kwargs` returning ``None`` (an unmapped flag) or a resolved ``limit``
      other than ``0`` (a capped read bd's own truncation notice can't be reproduced for, see the
      module docstring) both mean "select the CLI-compatibility forward instead", decided here,
      never by catching an API failure.
    * Once the shape qualifies, :func:`beadhive.work_queue.open_ready` is tried; it returns
      ``None`` for the same service/capability-unavailable fallback set every other queue seam
      uses, in which case this also selects CLI-compatibility.

    Returns ``False`` to mean "select the CLI-compatibility forward instead"; on success this
    writes the byte-identical response itself and raises the exit, so a caller never falls
    through to the CLI forward after a successful API read.
    """
    kwargs = _api_ready_kwargs(args)
    if kwargs is None or kwargs.get("limit", 100) != 0:
        return False
    rows = work_queue.open_ready(cwd, entry, **kwargs)
    if rows is None:
        return False
    sys.stdout.write(work_queue.encode_ready_rows(rows))
    raise typer.Exit(0)


def forward_ready_plain(args, cwd) -> None:
    result = bd_cli.ready(cwd, args)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    raise typer.Exit(ready_truncated_exit(args, result, as_json="--json" in args))


def emit_start_gated_ready(cfg, entry, cwd, args) -> None:
    result = bd_cli.ready(cwd, args)
    try:
        beads = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        beads = None
    if not isinstance(beads, list):
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        raise typer.Exit(result.returncode)
    order = [str(bead.get("id") or "") for bead in beads]
    deferrals = schedule_mod.start_gate(
        beads, order, estimator=config.release_conflict_estimator(cfg, entry)
    )
    strategy = str(config.release_value(cfg, entry, "strategy", "") or "")
    for _deferral in deferrals:
        otel.record_deferred_start({"bh.release.strategy": strategy})
    deferred = {deferral.id for deferral in deferrals}
    for bead in beads:
        bead["deferred"] = str(bead.get("id") or "") in deferred
    sys.stdout.write(json.dumps(beads, indent=2) + "\n")
    if result.stderr:
        sys.stderr.write(result.stderr)
    raise typer.Exit(ready_truncated_exit(args, result, as_json=True))


def ready(ctx: typer.Context, hive: str = ""):
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    args = widen_narrowed_ready_args(list(ctx.args))
    if "--json" in args and "--gated" not in args:
        entry = registry.entry_for_dir(cfg, cwd) or {}
        if str(config.release_value(cfg, entry, "strategy", "") or ""):
            emit_start_gated_ready(cfg, entry, cwd, args)
            return
        if ready_via_api(cwd, entry, args):
            return
    if "--gated" in args:
        entry = registry.entry_for_dir(cfg, cwd)
        strategy = str(config.release_value(cfg, entry, "strategy", "") or "")
        if strategy:
            forward_ready_ordered(
                args,
                cwd,
                strategy,
                config.release_fix_churn_budget(cfg, entry),
                config.release_conflict_estimator(cfg, entry),
            )
            return
    forward_ready_plain(args, cwd)


def issue(ctx: typer.Context, bead: str, hive: str = ""):
    otel.set_bead(bead)
    cfg = config.load()
    forward_read(["show", bead, *ctx.args], registry.hive_dir_for(cfg, hive))


def list_(ctx: typer.Context, hive: str = ""):
    cfg = config.load()
    forward_read(["list", *ctx.args], registry.hive_dir_for(cfg, hive))
