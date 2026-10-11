"""``bh hive frames ACTION HIVE_ID`` — the operator verb for ``open``-policy frame refs.

bh-sfp9o.5, trust-profiles ADR section 4 ("Merging frame refs"). The procedure is
:mod:`beadhive.frame_merge`; this module resolves the hive, renders, and maps refusals to exit 1.

* ``status`` — read-only: the recorded outcome per frame ref of the hive (any host).
* ``merge [--frame F]`` — on the writer: merge every published ``refs/dolt/frame/<id>`` (or one)
  into local ``main`` without ``--strategy``. A ref with a conflict is held unmerged and its
  rows are recorded; the others still merge. Exit 1 while any ref is held.
* ``resolve --frame F --bead B --take main|frame`` — on the writer: take ``main``'s or the
  frame's side for one recorded conflicting bead; once every recorded bead of the frame has a
  side, the merge completes. Refused without ``--take``: nothing is resolved by default.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import frame_merge
from .writer_adopt import DataUnreachable

__all__ = ["ACTIONS", "run"]

ACTIONS = ("status", "merge", "resolve")


def _fail(message: str) -> None:
    typer.echo(f"✗ {message}", err=True)
    raise typer.Exit(1)


def _emit(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _render(prefix: str, outcomes: list[frame_merge.FrameMergeOutcome]) -> None:
    if not outcomes:
        typer.echo(f"✓ {prefix}: no frame refs")
        return
    data = {"hives": {prefix: [o.as_dict() for o in outcomes]}}
    for line in frame_merge.doctor_lines(data):
        typer.echo(line)


def _writer(prefix: str, hive_dir: Path, cfg) -> frame_merge.WriterContext:
    try:
        return frame_merge.writer_context(hive_dir, cfg=cfg)
    except frame_merge.FrameMergeRefused as exc:
        _fail(str(exc))
        raise  # unreachable


def run(
    action: str,
    *,
    cfg,
    entry: dict,
    hive_dir: Path,
    frame: str = "",
    bead: str = "",
    take: str = "",
    as_json: bool = False,
) -> None:
    prefix = str(entry.get("prefix", ""))
    if action == "status":
        outcomes = [o for _, o in sorted(frame_merge.load().get(prefix, {}).items())]
        if as_json:
            _emit({"hive": prefix, "frames": [o.as_dict() for o in outcomes]})
        else:
            _render(prefix, outcomes)
        return
    if action == "merge":
        ctx = _writer(prefix, hive_dir, cfg)
        try:
            outcomes = frame_merge.merge_hive(ctx, frames=[frame] if frame else None)
        except DataUnreachable as exc:
            _fail(f"{prefix}: cannot list the frame refs: {exc}")
            return
        if as_json:
            _emit({"hive": prefix, "frames": [o.as_dict() for o in outcomes]})
        else:
            _render(prefix, outcomes)
        if any(o.held for o in outcomes):
            raise typer.Exit(1)
        return
    if not frame or not bead:
        _fail("resolve needs --frame <id> and --bead <bead> (and --take main|frame)")
    ctx = _writer(prefix, hive_dir, cfg)
    try:
        outcome = frame_merge.resolve(ctx, frame, bead, take or None)
    except frame_merge.FrameMergeRefused as exc:
        _fail(str(exc))
        return
    if as_json:
        _emit(outcome.as_dict())
    elif outcome.status == frame_merge.Status.CONFLICT and outcome.unresolved:
        typer.echo(
            f"✓ {prefix}: {bead} takes {take}'s side; still to resolve on "
            f"{outcome.ref}: {', '.join(outcome.unresolved)}"
        )
    else:
        _render(prefix, [outcome])
    if outcome.status == frame_merge.Status.FAILED or (
        outcome.status == frame_merge.Status.CONFLICT and not outcome.unresolved
    ):
        raise typer.Exit(1)
