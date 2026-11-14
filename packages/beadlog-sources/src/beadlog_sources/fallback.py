"""The opt-in merge fallback (``backfill.collate.identify.merge_fallback = commits``, ADR D1).

A merge unit whose identification ends residual, NOISE or SKIP projects nothing, so the commits it
swept in appear nowhere in the plan (the reporting half of the fix, :func:`~beadlog_sources.
identify.hidden_commits`, is always on). With the fallback on, :func:`merge_fallback` replaces such
a merge by the units the ``commit`` rule would have made of its commits and identifies those in
the same run. Residual is only known after B5, which runs after B3, so it is a deterministic
second B3, B4 and B5 pass over exactly those merges, never a B3 option:

1. the merges to release are the ``merge``-rule units that ended residual, NOISE or SKIP (a branch
   sync, which claims only itself, has nothing to release), never one an operator decided (verdict
   step ``decision``: the decision wins, and its commits stay reported as hidden), and hold at
   least one commit that
   ``include_types`` admits (a merge with none stays, so what it hides stays reported);
2. B3 runs again with those merges released (:func:`~beadlog_sources.cluster.cluster`'s
   ``release``): their commits go to the ``commit`` rule, the merges inside them are looked at as
   first-parent merges of their own, and area epics are rebuilt;
3. B4 runs again; B5 runs over the units that are new or whose shape (or attached docs) changed,
   and every other unit keeps its first-pass verdict. The judge sees the first pass's answers as
   its prior and spends only what the first pass left of ``max_calls_per_run``.

A merge a second pass newly exposes is not released again: its commits, if it ends residual, are
reported as hidden. The commit units' keys are the keys the commit rule would make, so a second
run (after applying) finds the beads they made by ``external_ref`` and proposes nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import replace

from beadlog import BeadEvent

from .attach import AttachOptions, attach
from .cluster import ANY_TYPE, ClusterOptions, cluster
from .collate import ISSUE, RULE_MERGE, Collation, Unit
from .identify import (
    COMMIT_PREFIX,
    HIDDEN_DECISIONS,
    STEP_DECISION,
    STEPS,
    IdentifyOptions,
    IdentifyRun,
    commit_subjects,
    identify_units,
    prior_verdicts,
)


def _releasable(
    collation: Collation,
    include_types: Iterable[str],
    decisions: Mapping[str, str],
    operator: Collection[str] = (),
) -> list[str]:
    """The merge units to replace by their commits (see the module doc)."""
    assert collation.indexes is not None
    types = set(include_types)
    out = []
    for unit in collation.units:
        if unit.rule != RULE_MERGE or unit.kind != ISSUE or unit.noise:
            continue
        if decisions.get(unit.key, "residual") not in HIDDEN_DECISIONS:
            continue
        if unit.key in operator:
            continue  # an operator decision (step 0) always wins
        for key in unit.artifacts[1:]:
            art = collation.indexes.artifact(key)
            if art is None or not key.startswith(COMMIT_PREFIX) or len(art.attrs["parents"]) >= 2:
                continue
            if ANY_TYPE in types or art.attrs["cc_type"] in types:
                out.append(unit.key)
                break
    return out


def merge_fallback(
    first: Collation,
    resolved: Collation,
    baseline: Iterable[BeadEvent],
    *,
    cluster_options: ClusterOptions | None,
    attach_options: AttachOptions | None,
    options: IdentifyOptions,
    run: IdentifyRun | None,
    calls: int = 0,
) -> Collation:
    """``first`` (B5 done) with its residual, NOISE and SKIP merge units replaced by their commits.

    ``resolved`` is the B2 collation ``first`` was built from (it carries the B1 indexes);
    ``calls`` is what the first identify pass spent of the judge's call cap. Returns ``first``
    itself when no merge qualifies.
    """
    baseline = tuple(baseline)
    cluster_options = cluster_options or ClusterOptions()
    decided = {r["unit"]: r["class"] for r in first.verdicts if r["class"] is not None}
    operator = {r["unit"] for r in first.verdicts if r["step"] == STEP_DECISION}
    released = _releasable(first, cluster_options.include_types, decided, operator)
    if not released:
        return first
    second = attach(cluster(resolved, cluster_options, release=released), attach_options)
    before = {u.key: u for u in first.units}
    docs_before = {u.key: first.docs_of(u.key) for u in first.units}
    changed: list[Unit] = [
        u
        for u in second.units
        if before.get(u.key) != u or docs_before.get(u.key) != second.docs_of(u.key)
    ]
    keep = {u.key for u in second.units} - {u.key for u in changed}
    run = run or IdentifyRun()
    again = replace(
        run,
        prior={**run.prior, **prior_verdicts(first)},
        max_calls_per_run=max(0, run.cap() - calls),
        confirm=True,  # the first pass was confirmed; this one only spends what is left
    )
    docs: dict[str, list[str]] = {}
    for found in second.attachments:
        docs.setdefault(found.unit, []).append(found.doc)
    result = identify_units(
        changed,
        baseline,
        options,
        again,
        docs,
        commit_subjects(second) if options.merge_identity == "commits" else None,
    )
    verdicts = [r for r in first.verdicts if r["unit"] in keep] + list(result.verdicts)
    verdicts.sort(key=lambda r: (r["unit"], STEPS.index(r["step"])))
    residual = [r for r in first.residual if r["unit"] in keep] + list(result.residual)
    residual.sort(key=lambda r: r["unit"])
    return replace(
        second,
        verdicts=tuple(verdicts),
        residual=tuple(residual),
        options={**second.options, "identify": options.to_json()},
    )


__all__ = ["merge_fallback"]
