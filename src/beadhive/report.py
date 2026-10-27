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

from . import (
    bd,
    config,
    engine,
    hub,
    intake_services,
    registry,
    validate,  # noqa: F401 - `report.validate` is the label-check seam tests patch
)
from .modules.intake import ORIGIN_REPORT, REPORT_TYPES

__all__ = ["REPORT_TYPES", "entry_dupes", "file_report"]


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


def file_report(
    hive, title, report_type, actor, cfg=None, description: str = "", *, origin=ORIGIN_REPORT
) -> tuple[int, str, str]:
    """File a report bead into a hive we own. Returns `(exit, error, new_id)`; callers render
    `error`. The bead lands born-native with the target triplet, the requested type, the closed
    `origin` intake channel + reporter (`bd --actor`) provenance, and `intake=untriaged`
    queue state — NO `source_system` overload.

    The filing policy (type gate, label check, create, stamp) is `IntakeService.file_report` over
    a `BdReportStore`, composed in `intake_services`; this function keeps hive resolution,
    clone-on-demand and the push.

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

    # Validate ONLY the new bead's own labels (target triplet + closed origin/intake channel) —
    # NOT the target hive's whole DB. A cross-hive reporter has no authority over the target's
    # pre-existing label debt and must not be deadlocked by it. The service asserts this before
    # the create.
    code, error, new_id = intake_services.file_report(
        cfg,
        entry,
        target,
        title,
        report_type,
        actor,
        description=description,
        origin=origin,
        binary=config.BINARY_ALIAS,
    )
    if error:
        return code, error, new_id

    if pushed:
        push = engine.get_engine(cfg).push_state(target, actor=actor, message=f"report: {title}")
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
    cfg = cfg if cfg is not None else config.load()
    try:
        entry = registry.resolve_hive(cfg, hive)
    except Exception:
        return []
    target, _pushed = _target(cfg, entry)
    if target is None:
        return []
    return intake_services.entry_dupes(target, new_id, threshold)
