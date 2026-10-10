"""``hive.adopt_existing`` — read a hive's bead-store identity instead of deriving one (bh-m2irb).

Onboarding an EXISTING hive used to invent its identity: ``bh hive add`` derived a prefix from
the org code and repo name (``br-nvidia-hackathon`` for a hive whose beads are all ``nvhack-*``,
bh-ajgot), and a fresh host's ``bd bootstrap`` resolved bd's generic ``beads`` database, which
collides with every other hive on the shared server (bh-6csoa). The identity was knowable the
whole time — it lives IN the store, as ``issue_prefix`` in bd's ``config`` table and
``_project_id`` in its ``metadata`` table — but ``.beads/metadata.json`` is untracked for a
zero-footprint hive, so a clone cannot recover it from git.

With ``hive.adopt_existing: true`` (host-scoped, default off) registration asks the store first:

1. **the local checkout** — a ``.beads`` store already on this host (``bd config list`` for the
   prefix, ``.beads/metadata.json`` for the project id);
2. **the remote's data ref** — ``refs/dolt/data`` on the hive's git remote, read through a
   shallow ``dolt clone`` into a throwaway directory under bh's own cache (the caller passes
   it; never ``/tmp``, which is RAM on the factory hosts), with an isolated ``DOLT_ROOT_PATH``
   so it needs no dolt user config and touches no server;
3. **otherwise derive**, exactly as before — a hive with no store anywhere has nothing to adopt.

Every probe here is read-only and never raises: "cannot answer" (no store, unreachable remote,
missing binary, timeout) is the same ``None`` to every caller, which then derives.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import store_locator
from .run import run

#: Seconds a remote identity probe (``git ls-remote`` + shallow ``dolt clone`` + one query) may
#: take before it is abandoned and the caller derives instead. ``hive.adopt_probe_timeout``.
DEFAULT_PROBE_TIMEOUT = 120

#: bd's own generic database names. A store resolving to one of these has no per-hive identity on
#: a shared server — adopting it is exactly the collision bh-6csoa hit.
GENERIC_DATABASES = frozenset({"beads", "dolt"})

_IDENTITY_QUERY = (
    "SELECT `key`, `value` FROM `config` WHERE `key` = 'issue_prefix' "
    "UNION ALL SELECT `key`, `value` FROM `metadata` WHERE `key` = '_project_id'"
)


@dataclass(frozen=True)
class StoreIdentity:
    """What a hive's own bead store says it is."""

    prefix: str
    project_id: str = ""
    source: str = ""  # where it was read from, for the operator-facing note

    @property
    def database(self) -> str:
        """The shared-server database this identity lands on: the sanitized prefix, the same
        collision-free name ``hub.cache_database`` and ``migrate-storage`` derive."""
        return store_locator.sanitize_database_name(self.prefix)


# ---- config -----------------------------------------------------------------


def _hive_section(cfg) -> dict:
    section = (cfg or {}).get("hive") if hasattr(cfg, "get") else None
    return section if hasattr(section, "get") else {}


def adopt_existing_enabled(cfg) -> bool:
    """``hive.adopt_existing`` — opt-in, default off."""
    return _hive_section(cfg).get("adopt_existing") is True


def probe_timeout(cfg) -> float:
    """``hive.adopt_probe_timeout`` seconds, falling back to the default on anything unusable."""
    value = _hive_section(cfg).get("adopt_probe_timeout", DEFAULT_PROBE_TIMEOUT)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_PROBE_TIMEOUT)
    return seconds if seconds > 0 else float(DEFAULT_PROBE_TIMEOUT)


# ---- local checkout ---------------------------------------------------------


def _local_store_present(hive_dir: Path) -> bool:
    """Whether ``hive_dir``'s store can be ASKED without waiting on bd's not-found retries.

    A server-mode checkout whose database is not on this host's shared server (a fresh clone
    of a furnished hive with tracked metadata) makes bd retry for over a minute before failing;
    an identity probe must not pay that. Filesystem facts only (``store_locator``)."""
    if not (hive_dir / ".beads").is_dir():
        return False
    mode = store_locator.dolt_mode(hive_dir)
    if mode == "embedded":
        return store_locator.embedded_store_dir(hive_dir).is_dir()
    if mode == "server":
        return (store_locator.server_store_dir() / store_locator.server_database(hive_dir)).is_dir()
    return False


def read_local(hive_dir: Path | str) -> StoreIdentity | None:
    """The identity of the store already in ``hive_dir``, or ``None`` when there is none here."""
    from . import bd  # lazy: bd imports config/registry, which this leaf module must not

    path = Path(hive_dir)
    if not _local_store_present(path):
        return None
    prefix = bd.store_prefix(path)
    if not prefix:
        return None
    return StoreIdentity(prefix, store_locator.project_id(path), f"the bead store in {path}")


# ---- remote data ref --------------------------------------------------------


def dolt_remote_url(git_url: str) -> str:
    """The Dolt git-remote form of a git clone URL — dolt reads ``refs/dolt/data`` through its
    own ``git+<scheme>://`` remotes (verified against dolt 2.3.5): scp-like
    ``git@host:owner/repo.git`` -> ``git+ssh://git@host/owner/repo.git``, ``https://`` ->
    ``git+https://``, a local path or ``file://`` -> ``git+file://``. ``""`` when unparseable."""
    url = (git_url or "").strip()
    if not url:
        return ""
    if url.startswith("git+"):
        return url
    if "://" in url:
        return f"git+{url}"
    if url.startswith("/"):
        return f"git+file://{url}"
    if ":" in url:  # scp-like: [user@]host:path
        host, path = url.split(":", 1)
        if host and path and "/" not in host:
            return f"git+ssh://{host}/{path.lstrip('/')}"
    return ""


def _remote_has_data(git_url: str, timeout: float) -> bool:
    try:
        res = run(
            ["git", "ls-remote", git_url, "refs/dolt/data"],
            check=False,
            capture=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return getattr(res, "returncode", 1) == 0 and bool((getattr(res, "stdout", "") or "").strip())


def _parse_rows(stdout: str) -> dict[str, str]:
    """``{key: value}`` from ``dolt sql -r json`` output (one JSON document per statement)."""
    out: dict[str, str] = {}
    decoder = json.JSONDecoder()
    text = (stdout or "").strip()
    while text:
        try:
            doc, end = decoder.raw_decode(text)
        except ValueError:
            break
        rows = doc.get("rows", []) if isinstance(doc, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("key") and row.get("value") is not None:
                out[str(row["key"])] = str(row["value"]).strip()
        text = text[end:].lstrip()
    return out


def read_remote(
    git_url: str, *, scratch: Path, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> StoreIdentity | None:
    """The identity the hive's remote ``refs/dolt/data`` carries, or ``None``.

    ``git ls-remote`` first (cheap: no data ref, no clone), then a depth-1 ``dolt clone`` of
    the data ref into a throwaway directory under ``scratch`` and one embedded query — no
    server, no bd, no change to the hive or to this host's shared server. The directory is
    removed however the probe ends. Callers pass bh's on-disk cache (``scratch_dir``), never
    the system temp dir, which is RAM-backed on the factory hosts."""
    dolt_url = dolt_remote_url(git_url)
    if not dolt_url or not _remote_has_data(git_url, timeout):
        return None
    try:
        Path(scratch).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix="probe-") as tmp:
            scratch_dir = Path(tmp)
            root = scratch_dir / "root"
            root.mkdir()
            env = {**os.environ, "DOLT_ROOT_PATH": str(root)}
            env.pop("DOLT_CLI_PASSWORD", None)
            database = scratch_dir / "db"
            cloned = run(
                ["dolt", "clone", "--depth", "1", "--single-branch", dolt_url, str(database)],
                cwd=str(scratch_dir),
                env=env,
                check=False,
                capture=True,
                timeout=timeout,
            )
            if getattr(cloned, "returncode", 1) != 0:
                return None
            queried = run(
                ["dolt", "sql", "-r", "json", "-q", _IDENTITY_QUERY],
                cwd=str(database),
                env=env,
                check=False,
                capture=True,
                timeout=timeout,
            )
            if getattr(queried, "returncode", 1) != 0:
                return None
            values = _parse_rows(getattr(queried, "stdout", "") or "")
    except (subprocess.TimeoutExpired, OSError):
        return None
    prefix = values.get("issue_prefix", "")
    if not prefix:
        return None
    return StoreIdentity(prefix, values.get("_project_id", ""), f"refs/dolt/data on {git_url}")


# ---- resolution ---------------------------------------------------------------


def scratch_dir(cache_dir: Path) -> Path:
    """Where remote probes clone: a fixed subdirectory of bh's own cache dir."""
    return Path(cache_dir) / "adopt-probe"


def resolve(
    hive_dir: Path | str | None,
    git_url: str = "",
    *,
    scratch: Path,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> StoreIdentity | None:
    """Local checkout first, then the remote's data ref; ``None`` when neither has a store."""
    if hive_dir:
        found = read_local(Path(hive_dir))
        if found is not None:
            return found
    if git_url:
        return read_remote(git_url, scratch=scratch, timeout=timeout)
    return None


__all__ = (
    "DEFAULT_PROBE_TIMEOUT",
    "GENERIC_DATABASES",
    "StoreIdentity",
    "adopt_existing_enabled",
    "dolt_remote_url",
    "probe_timeout",
    "read_local",
    "read_remote",
    "resolve",
    "scratch_dir",
)
