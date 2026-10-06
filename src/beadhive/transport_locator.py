"""Where a hive's Dolt git-transport bare repo lives (or why it is not here) — beside
:mod:`beadhive.store_locator`, which answers the sibling "where is the store" facts.

Moved out of ``host_fence`` (bh-vwbxy, ADR F5 / bh-32379 P-R1); behaviour is unchanged.
Typer-free; no subprocess, only filesystem facts plus the configured server endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import dolt_health, store_locator

# Where Dolt stages its git transport, relative to ONE DATABASE directory (see
# `transport_lookup` — measured, not assumed). Mode moves the parent above it; the layout from
# the database down is identical, which is the whole reason this is one glob and not two.
_TRANSPORT_UNDER_DB = ".dolt/git-remote-cache/*/repo.git"
# ...and the same thing relative to a parent holding SEVERAL databases (embedded's `.beads/
# embeddeddolt/`, which is private to one hive — the server's parent is not, see the lookup).
_DOLT_TRANSPORT_GLOB = f"*/{_TRANSPORT_UNDER_DB}"

# What `transport_lookup` found, when it found no repos. Three genuinely different answers that
# an empty list used to collapse into one — and collapsing them is how a safety mechanism reads
# "disarmed" as "not needed" (bh-areg.6).
FOUND = "found"  # repos is non-empty
NONE = "none"  # this hive has no dolt transport at all (nodb/JSONL): nothing to fence
NOT_FOUND = "not-found"  # dolt-backed, locally reachable, but bd has not created it yet
UNREACHABLE = "unreachable"  # server mode against a NON-LOCAL server: on another machine

# Hosts that mean "the store is on this machine". Anything else is (c)-remote (`bh-3mik`): the
# transport repo lives in the server's data dir, on the server's disk, so no local hook and no
# local push can reach it (bh-ukit.2's verdict table).
_LOCAL_SERVER_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})


@dataclass(frozen=True)
class TransportLookup:
    """Where this hive's git transport is, or *why it isn't here* — see :data:`FOUND`,
    :data:`NONE`, :data:`NOT_FOUND`, :data:`UNREACHABLE`.

    The state is the point. ``repos == []`` used to be the whole answer and it meant three
    incompatible things at once, one of which ("the fence has nowhere to run") is a disarmed
    safety mechanism and two of which are fine. A caller that cannot tell them apart cannot
    tell a nodb hive from a broken fence."""

    repos: list[Path]
    state: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the absence of repos (if any) is BENIGN — nothing here needs fencing, or
        bd simply hasn't staged it yet. ``UNREACHABLE`` is the one that is not."""
        return self.state in (FOUND, NONE, NOT_FOUND)


def transport_lookup(hive_dir: Path) -> TransportLookup:
    """Every bare repo Dolt uses to stage `hive_dir`'s git transport — the repos that actually
    perform the ``git push`` — plus WHY there are none when there are none.

    **Measured, not assumed.** bh-ytbb.7 measured the embedded layout against bd 1.1.0;
    bh-ukit.2 measured both modes against bd HEAD-af076b6 and bd 1.1.0. bh-tfapu then pinned
    current shipped bd with Git trace2 rather than relying on hook execution. What that
    established:

      * The push fires from a hidden bare repo at ``<db>/.dolt/git-remote-cache/<hash>/
        repo.git``, in BOTH modes — **never** from the hive's own checkout, whose hook does not
        fire for a data push at all.
      * Only the PARENT differs: ``<hive>/.beads/embeddeddolt/`` embedded, versus
        ``~/.beads/shared-server/dolt/`` under bd's shared server, where the push runs inside
        the server process. That is why the parent comes from
        :func:`store_locator.store_dir` and the glob below is parent-relative.
      * ``refs/dolt/data`` is **not** a local ref in that repo — in either mode, on either bd
        version. The local side of the push is a transient
        ``refs/dolt/blobstore/origin/dolt/data/<uuid>``. This function's previous docstring
        claimed the opposite ("the repos that actually hold a LOCAL ``refs/dolt/data``"); that
        was wrong when written, and it is why the ADR's
        ``git push --atomic … origin refs/dolt/data refs/bh/epoch`` cannot be issued by bh from
        here. See ``docs/spikes/bh-ukit.2-fence-under-a-dolt-server.md``.
      * Current bd supplies ``core.hooksPath=/dev/null`` to that exact real Git process. A hook
        installed in the located repo therefore does not fire and confers no authority.

    **Scope differs by mode, deliberately.** Embedded's parent is private to the hive, so every
    database under it belongs to this hive (this repo carries ``bh`` and a legacy ``beads``) and
    all of them are returned — the caller picks by matching a candidate's ``origin`` against the
    hive's remote, never by directory name. The server's parent is shared by EVERY hive on the
    host, so returning its whole glob would hand a caller other hives' transport repos — and
    ``prepush`` would bake this hive's id into another hive's hook. Under a server the lookup is
    therefore scoped to this hive's own database (``store_locator.database_dir``)."""
    if not (Path(hive_dir) / ".beads").is_dir():
        return TransportLookup([], NONE, f"{hive_dir} has no .beads/ — not a bd-backed hive")

    mode = store_locator.dolt_mode(hive_dir)
    if mode == "server":
        host, _port = dolt_health.server_endpoint()
        if host not in _LOCAL_SERVER_HOSTS:
            return TransportLookup(
                [],
                UNREACHABLE,
                f"this hive's store is served by {host}, not this machine — its transport repo "
                f"is on that host's disk, so no local hook or push can reach it (bh-3mik)",
            )
        root, glob = store_locator.database_dir(hive_dir), _TRANSPORT_UNDER_DB
    elif mode == "embedded" or store_locator.has_embedded_store(hive_dir):
        root, glob = store_locator.embedded_store_dir(hive_dir), _DOLT_TRANSPORT_GLOB
    else:
        return TransportLookup(
            [], NONE, f"{hive_dir} records no dolt store (nodb/JSONL) — nothing to fence"
        )

    repos = sorted(p for p in root.glob(glob) if p.is_dir())
    if repos:
        return TransportLookup(repos, FOUND)
    return TransportLookup(
        [],
        NOT_FOUND,
        f"no transport repo under {root} yet — bd creates it lazily on the first "
        f"`bd dolt push` for this hive",
    )


def transport_repos(hive_dir: Path) -> list[Path]:
    """Just the repos from :func:`transport_lookup` — the list-only form every existing caller
    already expects. Prefer the lookup itself anywhere the *reason* for an empty list matters."""
    return transport_lookup(hive_dir).repos
