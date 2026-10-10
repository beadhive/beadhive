"""Publish a replica's writes under the ``open`` write policy to ``refs/dolt/frame/<id>``.

bh-sxhnq, trust-profiles ADR section 4 ("``open`` never forwards"). A non-primary write under
``open`` lands on the replica's own Dolt ``main``; publishing it must never move the hive's
``main`` (``refs/dolt/data``), which only the writer pushes. The replica instead pushes its
local ``main`` through a second Dolt remote, ``frame-<id>``, configured on the same Git remote
with its own data ref (``--ref refs/dolt/frame/<id>``, ``bh-sieai`` section 9). The writer
fetches that ref and merges it into ``main`` without ``--strategy`` (bh-sfp9o.5).

The remote's URL carries a trailing ``/`` so it differs from ``origin``'s: a Dolt sql-server
shares an open remote by URL and would otherwise read and write ``refs/dolt/data`` for both
(``bh-jbb6r`` E8). Nothing here merges, resets or force-pushes; a non-fast-forward on the
frame's own ref is reported, never overwritten.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .write_policy import FRAME_REF, FRAME_REMOTE

_FRAME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class PublishError(RuntimeError):
    """The frame ref could not be published; ``main`` was not touched."""


def frame_ref(frame: str) -> str:
    if not _FRAME_ID.fullmatch(frame or ""):
        raise PublishError(f"frame id {frame!r} cannot name a frame-private data ref")
    return FRAME_REF.format(frame=frame)


def frame_remote(frame: str) -> str:
    frame_ref(frame)
    return FRAME_REMOTE.format(frame=frame)


def private_url(url: str) -> str:
    """`url` made distinct from the hive remote's own URL (``bh-jbb6r`` E8)."""
    return url if url.endswith("/") else f"{url}/"


def _git_ref(params) -> str:
    if isinstance(params, str):
        try:
            params = json.loads(params or "{}")
        except ValueError:
            return ""
    return str((params or {}).get("git_ref") or "") if isinstance(params, dict) else ""


def _quote(value: str) -> str:
    from .fence_schema import quote

    return quote(value)


def ensure_remote(engine, frame: str) -> str:
    """Configure the ``frame-<id>`` remote on `engine`'s store (idempotent); returns its name.

    Reads ``dolt_remotes`` for the hive remote's URL; re-adds the frame remote when its URL or
    data ref drifted."""
    name, ref = frame_remote(frame), frame_ref(frame)
    rows = engine.query("SELECT name, url, params FROM dolt_remotes")
    remotes = {str(row.get("name")): row for row in rows}
    origin = remotes.get(engine.remote)
    if origin is None or not origin.get("url"):
        raise PublishError(
            f"the store has no {engine.remote!r} remote: nowhere to publish {ref} — pull the "
            "hive first"
        )
    url = private_url(str(origin["url"]))
    current = remotes.get(name)
    if current is not None:
        if str(current.get("url")) == url and _git_ref(current.get("params")) == ref:
            return name
        engine.execute([f"CALL DOLT_REMOTE('remove', {_quote(name)})"])
    added = f"CALL DOLT_REMOTE('add', '--ref', {_quote(ref)}, {_quote(name)}, {_quote(url)})"
    engine.execute([added])
    return name


def publish(hive_dir: Path, frame: str, *, node=None) -> str:
    """Push the replica's local ``main`` to ``refs/dolt/frame/<frame>``; returns the ref.

    Raises :class:`PublishError` (nothing published, ``main`` untouched) when the store is not
    on this host, has no hive remote, or the push fails or is not a fast-forward."""
    from . import fence_data

    ref = frame_ref(frame)
    node = node if node is not None else fence_data.node_for(Path(hive_dir))
    if node is None:
        raise PublishError(f"{hive_dir}: no local bead store to publish {ref} from")
    engine = node.engine
    try:
        name = ensure_remote(engine, frame)
        pusher = copy.copy(engine)
        pusher.remote = name
        landed = pusher.push()
    except (fence_data.FenceError, fence_data.DataUnreachable) as exc:
        raise PublishError(f"publishing {ref} failed: {exc}") from exc
    if not landed:
        raise PublishError(
            f"{ref} is not a fast-forward of this replica's main (it was published from a "
            "history this replica no longer has); nothing was overwritten — pull the hive, "
            "then publish again"
        )
    return ref
