"""`ws report <hive> <title>` — the INTERNAL terminal of cross-hive report intake (epic
). Files a bug/feature/chore into a hive **we own**, landing it as untriaged
intake for triage.

ONE verb, TWO callers, SAME path: a hive manager peer-directs a report into the owning hive, and
the superintendent uses the *same* `ws report <hive>` to route an ambiguous/cross-cutting report
to whichever hive it belongs to — the only difference is which `<hive>` they name. The external-hive
terminal (staging an outbound candidate) is the sibling bead and is out of
scope here.

Write path (identical for a cloned hive and a clone-on-demand one — the "one write path"):
  1. `bd -C <hive> create` a single born-native bead, carrying the TARGET hive's provider/org/repo
     triplet and the requested type. Plain `create` — NOT the old `import` + `source_system=report`
     workaround: a cross-hive report is born-native with no `external_ref`, so its channel must NOT
     overload the sync-coupled native `source_system` column (see `ws/state.py`).
  2. `bd -C <hive> set-state <id> origin=report` — the closed intake-CHANNEL dimension
     (event-sourced, same mechanism as `intake`); stamps provenance the queryable, validate-clean
     way.
  3. `bd -C <hive> set-state <id> intake=untriaged` — event-sourced intake queue state from the
     shared vocabulary in `ws/state.py` (bead), NOT an ad-hoc label.

Reporter identity stays on `bd --actor`; system-of-record (`source_system`/`external_ref`) is left
untouched — reserved for external mirrors, not born-native reports. This RETIRES the import +
`source_system=report` workaround (and the ws side of follow-up bead).

A hive we own but haven't cloned is fetched on demand by reusing `hub._fetch_cache` (blobless
clone + bootstrap — one write path, no bespoke dolt-push write); the new bead is then committed +
pushed back with bd's native `bd dolt` verbs. The write-guard (bead) only blocks
`bd github push/sync`, so this sanctioned path (`create` / `set-state` / `dolt push`) is never
gated.
"""

from __future__ import annotations

import json

from . import bd, config, hub, registry, validate
from .state import INTAKE_UNTRIAGED, ORIGIN_REPORT

# `--type` accepts the intake-relevant issue types; bd owns the full type vocabulary, we gate the
# user-facing surface to the ones a cross-hive report should be filed as.
REPORT_TYPES = frozenset({"bug", "feature", "chore"})

def _state_arg(label) -> str:
    """A `<dim>:<value>` label-cache constant → the `<dim>=<value>` arg `bd set-state` takes.

    Keeps the state vocabulary single-owned in `ws/state.py` rather than re-spelling dims here."""
    return label.replace(":", "=", 1)


def _target(cfg, entry):
    """`(dir, pushed)` — the hive directory to write into. A cloned hive writes into its on-disk
    `.beads` and needs no push; an uncloned hive is fetched on demand into the hub cache (reusing
    `hub._fetch_cache`) and its new bead is pushed back. Returns `(None, False)` when an uncloned
    hive has no remote beads data to fetch."""
    d = registry.hive_dir(entry)
    if (d / ".beads").is_dir():
        return d, False
    cache = hub._fetch_cache(cfg, entry)
    return (cache, True) if cache is not None else (None, False)


def _create_bead(title, report_type, ident, target, actor, description="") -> tuple[int, str, str]:
    """Create a born-native bead via plain `bd -C <target> --json create` — the target triplet +
    requested type, and NO `source_system` overload (retires the import workaround). Provenance
    (the intake channel + reporter) is stamped separately: `origin` via `set-state`, reporter via
    `--actor`. `description` (bh-u0qd) is passed through to `bd create -d` only when non-empty.
    Returns `(exit, error, new_id)` — `id` read from the `--json` create payload."""
    provider, org, repo = ident
    triplet = f"provider:{provider},org:{org},repo:{repo}"
    args = ["--json", "create", title, "--type", report_type, "-l", triplet]
    if description:
        args += ["-d", description]
    res = bd.run(args, target, actor, capture=True)
    if res.returncode:
        return res.returncode, f"bd create failed: {bd.err_line(res)}", ""
    try:
        new_id = (json.loads(res.stdout or "{}") or {}).get("id") or ""
    except json.JSONDecodeError:
        new_id = ""
    if not new_id:
        return 1, "bd create reported no new bead id", ""
    return 0, "", new_id


def _set_state(label, new_id, target, actor):
    """`bd set-state <id> <dim>=<value>` for a `ws/state.py` label-cache constant."""
    reason = f"filed via {config.BINARY_ALIAS} report"
    return bd.run(
        ["set-state", new_id, _state_arg(label), "--reason", reason],
        target,
        actor,
        capture=True,
    )


def file_report(
    hive, title, report_type, actor, cfg=None, description: str = "", *, origin=ORIGIN_REPORT
) -> tuple[int, str, str]:
    """File a report bead into a hive we own. Returns `(exit, error, new_id)`; callers render
    `error`. The bead lands born-native with the target triplet, the requested type, the closed
    `origin` intake channel + reporter (`bd --actor`) provenance, and `intake=untriaged`
    queue state — NO `source_system` overload.

    `description` (bh-u0qd) is the report body; defaults to "" so existing callers are unchanged.

    The `origin` keyword-only parameter defaults to ``ORIGIN_REPORT`` (the ``ws report`` channel)
    and is overridden to ORIGIN_ESCALATION by ws escalate. All other
    callers omit it and get the original behaviour unchanged."""
    cfg = cfg if cfg is not None else config.load()
    if report_type not in REPORT_TYPES:
        allowed = ", ".join(sorted(REPORT_TYPES))
        return 1, f"--type must be one of {allowed} (got {report_type!r})", ""

    entry = registry.resolve_hive(cfg, hive)  # raises typer.Exit on no/ambiguous match
    target, pushed = _target(cfg, entry)
    if target is None:
        return 1, f"hive {hive!r} is not cloned and has no remote beads data to file into", ""

    ident = (entry["provider"], entry["org"], entry["repo"])
    # Validate ONLY the new bead's own labels (target triplet + closed origin/intake channel) —
    # NOT the target hive's whole DB. A cross-hive reporter has no authority over the target's
    # pre-existing label debt and must not be deadlocked by it. The record we
    # write carries only registry-valid labels, which is what we assert here before the create.
    provider, org, repo = ident
    new_labels = [
        f"provider:{provider}",
        f"org:{org}",
        f"repo:{repo}",
        origin,
        INTAKE_UNTRIAGED,
    ]
    bad = validate.bead_violations(cfg, f"{entry['prefix']}-intake", new_labels)
    if bad:
        return 1, "report bead would carry invalid labels: " + "; ".join(bad), ""

    code, error, new_id = _create_bead(title, report_type, ident, target, actor, description)
    if error:
        return code, error, ""

    # Provenance = closed `origin` channel; queue membership = `intake` — two orthogonal dims,
    # both event-sourced via set-state from the shared ws/state.py vocabulary (not ad-hoc labels).
    for label, what in ((origin, "origin"), (INTAKE_UNTRIAGED, "intake state")):
        state = _set_state(label, new_id, target, actor)
        if state.returncode:
            msg = f"filed {new_id} but could not set {what}: {bd.err_line(state)}"
            return state.returncode, msg, new_id

    if pushed:
        bd.run(["dolt", "commit", "-m", f"report: {title}"], target, actor, capture=True)
        push = bd.run(["dolt", "push"], target, actor, capture=True)
        if push.returncode:
            msg = f"filed {new_id} in the cache but push to its hive failed: {bd.err_line(push)}"
            return push.returncode, msg, new_id

    return 0, "", new_id


def entry_dupes(hive, new_id, cfg=None, threshold: float = 0.5):
    """Likely duplicates of a freshly-filed report — dedup ON ENTRY (the triage side runs the same
    `bd find-duplicates` pass at triage). Best-effort: reuses the SAME target resolution as
    `file_report`, so it reads the very DB the report landed in (cloned or clone-on-demand cache),
    and returns [] rather than raising if the hive can't be resolved/read. Feature requests
    especially collide with existing backlog, so surfacing dupes here keeps the queue clean."""
    from . import triage

    cfg = cfg if cfg is not None else config.load()
    try:
        entry = registry.resolve_hive(cfg, hive)
    except Exception:
        return []
    target, _pushed = _target(cfg, entry)
    if target is None:
        return []
    return triage.dupes_touching(triage.find_dupes(target, threshold=threshold), [new_id])
