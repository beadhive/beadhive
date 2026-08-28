"""hive_sync.py — bidirectional bead-state sync with each hive's federation peer.

`bh hive sync` (this module) is the *pull+push* path for authoritative dolt bead state: per
hive it drives ``Engine.sync_state`` (`bd federation sync`) — or, with ``--dry-run``, the
read-only ``Engine.federation_status``. It is DISTINCT from `bh sync` (hub hydration): that
verb re-exports every hive's issues into the hub's index; this one moves the dolt state
channel itself between a hive and its remote peer.

Rules (bh-wty3 plan):
- HQ (``kind=hq``) is local-only by design and always skipped — same filter as ``hub.sync``.
- UNKNOWN is first-class: an unreachable/unverifiable peer renders ``unknown (reason)`` as
  loudly as a failure and counts as offending — never a fabricated 0/0.
- ``--dry-run`` parallelizes the read-only status pass (``fleet.fanout``, shape B, like
  sync_remote's assessment pass); a live sync is a WRITE and runs serially per hive.
- Conflicts are data: a paused sync prints the conflicted tables + the re-run instruction
  and lands the hive in the offending list instead of half-merging.
- NO PEER TOWNS IS A STATE, NOT A FAULT (bh-libi): a hive with nobody to federate with is
  reported and skipped, never counted as offending — which is what ``--dry-run`` has always
  done (``(no peers)``), and what the live pass now does too.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import typer

from . import config, engine, fleet, jsonout, registry

_STATUS_WORKERS = 4  # read-only federation_status calls; matches sync_remote's fleet pass
_COMPARISON_SCHEMA = 1
_COMPARISON_STALE_SECONDS = 300

STRATEGIES = ("ours", "theirs")

_HEADER = ("hive", "peer", "reachable", "ahead", "behind", "conflicts")


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _remote_stamp(value: str) -> datetime | None:
    """Parse bd's ``Status.LastSync`` without treating its Go zero-time as evidence."""
    if not value or value.startswith("0001-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _source_revision(hive_id: str, relative_to: str | None, facts: dict) -> str:
    encoded = json.dumps(
        ["dolt-comparison-v1", hive_id, relative_to, facts],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _comparison(
    hive_id: str,
    peer,
    *,
    relative_to: str | None = None,
    observed_at: datetime | None = None,
    failure: str = "",
    stale_after: int = _COMPARISON_STALE_SECONDS,
) -> dict:
    """One stable local-versus-remote comparison record.

    Counts are nullable on purpose: ``0`` is measured equality on that axis; ``None`` means
    the axis was not comparable.  This pure builder is shared by the CLI and tests so clients
    never need to infer a state from human prose or from a safety classification.
    """
    observed = (observed_at or _now()).astimezone(UTC)
    remote_observed = _remote_stamp(getattr(peer, "remote_observed_at", "")) if peer else None
    relation = relative_to or (str(getattr(peer, "peer", "")) or None)
    ahead: int | None = None
    behind: int | None = None
    reason = failure

    if peer is None and not failure:
        state = "unconfigured"
        reason = "no federation peer is configured"
    elif peer is None:
        state = "unavailable"
    elif not peer.reachable:
        state = "unavailable"
        reason = peer.reach_error or failure or "peer unreachable"
    elif peer.ahead < 0 or peer.behind < 0:
        state = "incomparable"
        reason = "the remote answered without comparable revision counts"
    else:
        ahead, behind = peer.ahead, peer.behind
        if peer.has_conflicts or (ahead > 0 and behind > 0):
            state = "diverged"
        elif ahead > 0:
            state = "ahead"
        elif behind > 0:
            state = "behind"
        else:
            state = "equal"
        if remote_observed is not None:
            age = (observed - remote_observed).total_seconds()
            if age < 0 or age > stale_after:
                state = "stale"
                reason = "remote comparison observation is outside the freshness window"

    coverage_state = {
        "equal": "complete",
        "ahead": "complete",
        "behind": "complete",
        "diverged": "complete",
        "stale": "partial",
        "incomparable": "partial",
        "unconfigured": "none",
        "unavailable": "none",
    }[state]
    facts = {
        "state": state,
        "ahead": ahead,
        "behind": behind,
        "remoteObservedAt": _stamp(remote_observed) if remote_observed else None,
        "reason": reason or None,
    }
    return {
        "hive": hive_id,
        "relativeTo": relation,
        "ahead": ahead,
        "behind": behind,
        "comparisonState": state,
        "observedAt": _stamp(observed),
        "remoteObservedAt": facts["remoteObservedAt"],
        "sourceRevision": _source_revision(hive_id, relation, facts),
        "coverage": {
            "state": coverage_state,
            "counts": "known" if ahead is not None and behind is not None else "unknown",
            "reason": reason or None,
        },
    }


def comparison_payload(
    entries: list[dict],
    statuses: list,
    *,
    peer: str | None = None,
    observed_at: datetime | None = None,
    stale_after: int = _COMPARISON_STALE_SECONDS,
) -> dict:
    """Versioned read-only comparison page for an already-bounded federation status pass."""
    observed = observed_at or _now()
    comparisons: list[dict] = []
    for entry, status in zip(entries, statuses, strict=True):
        hive_id = _hive_id(entry)
        if not status.ok:
            comparisons.append(
                _comparison(
                    hive_id,
                    None,
                    relative_to=peer if peer and peer != "all" else None,
                    observed_at=observed,
                    failure=status.error or "federation status unavailable",
                    stale_after=stale_after,
                )
            )
            continue
        selected = status.peers
        if peer and peer != "all":
            selected = tuple(value for value in selected if value.peer == peer)
        if not selected:
            comparisons.append(
                _comparison(
                    hive_id,
                    None,
                    relative_to=peer if peer and peer != "all" else None,
                    observed_at=observed,
                    stale_after=stale_after,
                )
            )
            continue
        comparisons.extend(
            _comparison(
                hive_id,
                value,
                observed_at=observed,
                stale_after=stale_after,
            )
            for value in selected
        )

    states = {item["coverage"]["state"] for item in comparisons}
    if not states or states <= {"none"}:
        overall = "none"
    elif states <= {"complete"}:
        overall = "complete"
    else:
        overall = "partial"
    return jsonout.envelope(
        "hive sync peers --dry-run",
        _COMPARISON_SCHEMA,
        {
            "observedAt": _stamp(observed),
            "comparisons": comparisons,
            "coverage": {
                "state": overall,
                "requestedHives": len(entries),
                "returnedComparisons": len(comparisons),
            },
            "networkPolicy": {
                "mode": "bounded-fetch",
                "readOnly": True,
                "timeoutSeconds": engine.FEDERATION_TIMEOUT,
                "maxConcurrency": _STATUS_WORKERS,
            },
        },
    )


def _hive_id(entry) -> str:
    return f"{entry['provider']}/{entry['org']}/{entry['repo']}"


def _targets(cfg, hive_id: str | None, hive_ids: list[str] | None = None) -> list[dict]:
    """The hive entries this run addresses — one or more resolved hives (``hive_id``, or the
    plural ``hive_ids``), or (both unset) every registered hive. HQ is excluded either way:
    local-only by design, no federation peer.

    "By design" is now a STATEMENT WITH AN EXPIRY DATE (bh-ab25i). It was a hard constraint
    while HQ's Dolt path carried a per-host derived aggregate alongside its own beads — bd's
    one-database-per-remote-path rule made federating that store incoherent. bh-89wxf.2 removed
    the aggregate, so HQ is now a legitimate federation participant and this exclusion becomes
    correct to FIX rather than to delete. Deliberately out of scope there; tracked in bh-ab25i."""
    real_hives = registry.hives(cfg)
    ids = list(hive_ids) if hive_ids else ([hive_id] if hive_id else [])
    if ids:
        entries = []
        for one in ids:
            entry = registry.resolve_hive(cfg, one)
            if entry not in real_hives:
                typer.echo("✗ HQ is local-only by design — it has no federation peer", err=True)
                raise typer.Exit(1)
            entries.append(entry)
        return entries
    return real_hives


def _status_rows(
    hive_id: str, fs, *, peer: str | None = None
) -> tuple[list[tuple[str, ...]], bool]:
    """Table rows for one hive's ``FederationStatus`` + whether its peer state is unverifiable.
    An unreachable peer's counts are NOT trustworthy — render ``unknown (reason)``, never 0/0.
    ``peer`` (unset or ``"all"`` = every peer) narrows the rendered rows to one named peer —
    filtered client-side; ``bd federation status`` has no per-peer fetch to skip the others."""
    if not fs.ok:
        return [(hive_id, "-", f"unknown ({fs.error})", "?", "?", "?")], True
    peers = fs.peers
    if peer and peer != "all":
        peers = tuple(p for p in peers if p.peer == peer)
    if not peers:
        return [(hive_id, "(no peers)", "-", "-", "-", "-")], False
    rows: list[tuple[str, ...]] = []
    unknown = False
    for p in peers:
        if p.reachable:
            conflicts = "yes" if p.has_conflicts else "no"
            rows.append((hive_id, p.peer, "yes", str(p.ahead), str(p.behind), conflicts))
        else:
            reason = p.reach_error or "unreachable"
            rows.append((hive_id, p.peer, f"unknown ({reason})", "?", "?", "?"))
            unknown = True
    return rows, unknown


def _render_table(rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(row[i]) for row in (_HEADER, *rows)) for i in range(len(_HEADER))]
    for row in (_HEADER, *rows):
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


def _status_pass(
    eng, entries: list[dict], *, peer: str | None = None, as_json: bool = False
) -> list[str]:
    """--dry-run: read-only fleet federation status, parallel (never calls ``sync_state``).
    Renders the two-axis table; returns the hive ids whose peer state could not be verified."""
    paths = [registry.hive_dir(e) for e in entries]

    # SHAPE B (`fleet.fanout`): federation status is a per-hive engine call over the network,
    # not a row the shared server holds. Keeps its own smaller cap — these are network-bound.
    def bounded_status(path):
        return eng.federation_status(path, timeout=engine.FEDERATION_TIMEOUT)

    statuses = fleet.fanout(bounded_status, paths, workers=_STATUS_WORKERS)

    if as_json:
        jsonout.emit(comparison_payload(entries, statuses, peer=peer))
        return [
            _hive_id(entry)
            for entry, status in zip(entries, statuses, strict=True)
            if not status.ok
            or any(
                not value.reachable or value.ahead < 0 or value.behind < 0
                for value in (
                    tuple(v for v in status.peers if v.peer == peer)
                    if peer and peer != "all"
                    else status.peers
                )
            )
        ]

    rows: list[tuple[str, ...]] = []
    offending: list[str] = []
    for entry, fs in zip(entries, statuses, strict=True):
        hive_id = _hive_id(entry)
        hive_rows, unknown = _status_rows(hive_id, fs, peer=peer)
        rows.extend(hive_rows)
        if unknown:
            offending.append(hive_id)
    _render_table(rows)

    if offending:
        typer.echo(
            f"\n✗ {len(offending)} hive(s) could not be verified (unknown ≠ in-sync):",
            err=True,
        )
        for hive_id in offending:
            typer.echo(f"    - {hive_id}", err=True)
    return offending


def _live_pass(
    eng, entries: list[dict], strategy: str | None, *, peer: str | None = None
) -> list[str]:
    """Live sync, SERIAL per hive (writes never ride the thread pool). Returns the hive ids
    that failed or paused on conflicts — a hive with no peer towns is NOT one of them."""
    offending: list[str] = []
    sync_peer = peer if peer and peer != "all" else None
    for entry in entries:
        hive_id = _hive_id(entry)
        outcome = eng.sync_state(registry.hive_dir(entry), peer=sync_peer, strategy=strategy)
        if outcome.ok:
            typer.echo(f"✓ {hive_id}: synced")
            continue
        if outcome.no_peers:
            # Federation is hive-to-hive. Having nobody to federate WITH is the normal state of
            # every hive in a single-town fleet, so it is reported and skipped rather than
            # failed — otherwise `bh host provision` fails step 7 and `_step_adopt` fail-closes
            # on a host that is in fact perfectly provisioned. Upstream is a different channel.
            typer.echo(
                f"• {hive_id}: no federation peers — nothing to sync "
                "(upstream moves via `bh hive sync-remote`)"
            )
            continue
        offending.append(hive_id)
        if outcome.paused:
            typer.echo(f"✗ {hive_id}: sync paused — conflicted table(s):", err=True)
            for table in outcome.conflicts:
                typer.echo(f"    - {table}", err=True)
            typer.echo(
                "    re-run with --strategy ours|theirs, or resolve manually via bd "
                "(bd federation sync)",
                err=True,
            )
        else:
            typer.echo(f"✗ {hive_id}: sync failed — {outcome.error}", err=True)
    if offending:
        typer.echo(f"\n✗ {len(offending)} hive(s) failed or paused:", err=True)
        for hive_id in offending:
            typer.echo(f"    - {hive_id}", err=True)
    return offending


def hive_sync(
    *,
    hive_id: str | None = None,
    hive_ids: list[str] | None = None,
    peer: str | None = None,
    strategy: str | None = None,
    dry_run: bool = False,
    as_json: bool = False,
) -> list[str]:
    """Sync the targeted hive(s) with their federation peer (or preview with ``dry_run``).
    ``peer`` (unset or ``"all"`` = every peer) narrows to one named federation peer. Returns
    the offending hive ids — failed, paused-on-conflicts, or (dry-run) unverifiable. Never
    raises for a per-hive failure; the CLI decides the exit code."""
    if as_json and not dry_run:
        raise ValueError("JSON comparison is read-only and requires dry_run=True")
    cfg = config.load()
    entries = _targets(cfg, hive_id, hive_ids)
    if not entries:
        if as_json:
            jsonout.emit(comparison_payload([], []))
        else:
            typer.echo("no syncable hives registered (HQ is local-only and always skipped)")
        return []
    eng = engine.get_engine(cfg)
    if dry_run:
        return _status_pass(eng, entries, peer=peer, as_json=as_json)
    return _live_pass(eng, entries, strategy, peer=peer)
