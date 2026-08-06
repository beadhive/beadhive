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
- ``--dry-run`` parallelizes the read-only status pass (ThreadPoolExecutor, like
  sync_remote's assessment pass); a live sync is a WRITE and runs serially per hive.
- Conflicts are data: a paused sync prints the conflicted tables + the re-run instruction
  and lands the hive in the offending list instead of half-merging.
- Every peer is synced by NAME, one ``--peer`` per call — see :func:`_sync_peers` for the bd
  defect that forces it and the condition for dropping the workaround (bh-rx3p).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import typer

from . import config, engine, registry

_STATUS_WORKERS = 4  # read-only federation_status calls; matches sync_remote's fleet pass

STRATEGIES = ("ours", "theirs")

_HEADER = ("hive", "peer", "reachable", "ahead", "behind", "conflicts")


def _hive_id(entry) -> str:
    return f"{entry['provider']}/{entry['org']}/{entry['repo']}"


def _targets(cfg, hive_id: str | None) -> list[dict]:
    """The hive entries this run addresses — one resolved hive, or (``hive_id=None``) every
    registered hive. HQ is excluded either way: local-only by design, no federation peer."""
    real_hives = registry.hives(cfg)
    if hive_id:
        entry = registry.resolve_hive(cfg, hive_id)
        if entry not in real_hives:
            typer.echo("✗ HQ is local-only by design — it has no federation peer", err=True)
            raise typer.Exit(1)
        return [entry]
    return real_hives


def _status_rows(hive_id: str, fs) -> tuple[list[tuple[str, ...]], bool]:
    """Table rows for one hive's ``FederationStatus`` + whether its peer state is unverifiable.
    An unreachable peer's counts are NOT trustworthy — render ``unknown (reason)``, never 0/0."""
    if not fs.ok:
        return [(hive_id, "-", f"unknown ({fs.error})", "?", "?", "?")], True
    if not fs.peers:
        return [(hive_id, "(no peers)", "-", "-", "-", "-")], False
    rows: list[tuple[str, ...]] = []
    unknown = False
    for p in fs.peers:
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


def _status_pass(eng, entries: list[dict]) -> list[str]:
    """--dry-run: read-only fleet federation status, parallel (never calls ``sync_state``).
    Renders the two-axis table; returns the hive ids whose peer state could not be verified."""
    paths = [registry.hive_dir(e) for e in entries]
    with ThreadPoolExecutor(max_workers=_STATUS_WORKERS) as pool:
        statuses = list(pool.map(eng.federation_status, paths))

    rows: list[tuple[str, ...]] = []
    offending: list[str] = []
    for entry, fs in zip(entries, statuses, strict=True):
        hive_id = _hive_id(entry)
        hive_rows, unknown = _status_rows(hive_id, fs)
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


def _sync_peers(eng, hive_dir) -> tuple[str | None, ...]:
    """The peers to sync this hive with — one EXPLICIT ``--peer`` each. ``(None,)`` (one
    unqualified call, i.e. bd's own default) when the hive reports no peers.

    ponytail: WORKAROUND for an upstream bd defect, not a design. Present in bd 1.1.0 (dev) and
    still at gastownhall/beads 8c7db45 (2026-08-06) — introduced by 6190dd9 (2026-01-21), the
    commit that first implemented the command, so it has never worked.

    ``bd federation sync`` with no ``--peer`` is documented as "syncs with all configured
    peers", but ``cmd/bd/federation.go`` enumerates them as::

        for _, r := range remotes {
            if r.Name != "origin" {   // <-- drops the peer we register
                peers = append(peers, r.Name)
            }
        }

    ...then reports the resulting empty list as "no federation peers configured".
    :data:`host_provision.PEER_NAME` is exactly ``origin``, so a hive carrying only the standard
    bh-registered peer syncs NOTHING while ``federation list-peers`` and ``federation status``
    — neither of which filters — both resolve it fine. That three-way disagreement is the whole
    bug; ``--dry-run`` rides ``federation_status`` and so never sees it.

    The ``--peer <name>`` path skips the enumeration entirely and goes straight to the fetch
    (verified: an invented name reaches a dolt fetch error, not "no peers configured"), so
    naming each peer sidesteps it. Looping preserves bd's documented "all configured peers"
    meaning rather than quietly narrowing it to the first — and a second peer named anything
    but ``origin`` is one bd would sync and one it would silently skip.

    REMOVE WHEN bd's no-``--peer`` enumeration sees what ``list-peers`` sees; this collapses
    back to a single unqualified ``sync_state(hive_dir, strategy=strategy)``.
    """
    return eng.list_peers(hive_dir) or (None,)


def _live_pass(eng, entries: list[dict], strategy: str | None) -> list[str]:
    """Live sync, SERIAL per hive (writes never ride the thread pool). Returns the hive ids
    that failed or paused on conflicts — once per hive, however many peers it has."""
    offending: list[str] = []
    for entry in entries:
        hive_id = _hive_id(entry)
        hive_dir = registry.hive_dir(entry)
        peers = _sync_peers(eng, hive_dir)
        failed = False
        for peer in peers:
            # Unqualified for a single peer, so the common line stays about the HIVE; the peer
            # is only named when there is more than one and the line would otherwise be ambiguous.
            label = hive_id if len(peers) == 1 else f"{hive_id} [{peer}]"
            outcome = eng.sync_state(hive_dir, peer=peer, strategy=strategy)
            if outcome.ok:
                typer.echo(f"✓ {label}: synced")
                continue
            failed = True
            if outcome.paused:
                typer.echo(f"✗ {label}: sync paused — conflicted table(s):", err=True)
                for table in outcome.conflicts:
                    typer.echo(f"    - {table}", err=True)
                typer.echo(
                    "    re-run with --strategy ours|theirs, or resolve manually via bd "
                    "(bd federation sync)",
                    err=True,
                )
            else:
                typer.echo(f"✗ {label}: sync failed — {outcome.error}", err=True)
        if failed:
            offending.append(hive_id)
    if offending:
        typer.echo(f"\n✗ {len(offending)} hive(s) failed or paused:", err=True)
        for hive_id in offending:
            typer.echo(f"    - {hive_id}", err=True)
    return offending


def hive_sync(
    *, hive_id: str | None = None, strategy: str | None = None, dry_run: bool = False
) -> list[str]:
    """Sync the targeted hive(s) with their federation peer (or preview with ``dry_run``).
    Returns the offending hive ids — failed, paused-on-conflicts, or (dry-run) unverifiable.
    Never raises for a per-hive failure; the CLI decides the exit code."""
    cfg = config.load()
    entries = _targets(cfg, hive_id)
    if not entries:
        typer.echo("no syncable hives registered (HQ is local-only and always skipped)")
        return []
    eng = engine.get_engine(cfg)
    if dry_run:
        return _status_pass(eng, entries)
    return _live_pass(eng, entries, strategy)
