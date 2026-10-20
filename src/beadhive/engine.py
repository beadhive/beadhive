"""Engine seam — the swappable operations `bh` needs from a beads-compatible backend.

This module owns the one documented `bd` process boundary. ``BdEngine.invoke`` preserves the
real variants callers need — hive flag vs ambient cwd, optional process-cwd pinning, capture vs
stream, environment and actor attribution — while applying one default timeout, timeout result,
and exit-0/no-work policy. Higher operations compose that primitive; `bd` remains the only
implementation. Modeled on dolt.py's container-backend dispatch: a config key (`beads.engine`)
selects a thin implementation, not a plugin framework.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import bd as bd_mod
from . import config

FEDERATION_TIMEOUT = 60.0  # seconds — federation status is a real network fetch per peer
# seconds — dolt push/pull is a real bead-data transfer, so this is deliberately generous
# (2x FEDERATION_TIMEOUT) to avoid tripping on a large but legitimate sync. It exists to bound
# a WEDGED remote, not to police slow ones: past this, `_state_call` degrades to the warning
# path `work._pull_state` already documents rather than hanging the hive (bh-uxew).
STATE_TIMEOUT = 120.0
FSCK_TIMEOUT = 300
MAX_FSCK_TIMEOUT = 900


def _state_store_bytes(cwd) -> int:
    from . import store_locator

    total = 0
    root = store_locator.database_dir(Path(cwd))
    try:
        for path in root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except OSError:
        return total
    return total


def _fsck_timeout(cwd) -> int:
    gib = 1024**3
    extra = ((_state_store_bytes(cwd) + gib - 1) // gib) * 60
    return min(MAX_FSCK_TIMEOUT, FSCK_TIMEOUT + extra)


FSCK_ENV = "BEADS_FSCK_TIMEOUT"
CAP_ENV = "BH_STATE_TIMEOUT_CAP"
TRANSFER_MARGIN = 60.0
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNIT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value) -> float | None:
    """Seconds for a Go-style duration (``30m``, ``1h30m``, ``900s``) or bare seconds; None if
    unparseable or non-positive."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        parts = _DURATION.findall(text)
        if not parts or "".join(n + u for n, u in parts) != text:
            return None
        seconds = sum(float(n) * _UNIT[u] for n, u in parts)
    return seconds if seconds > 0 else None


def is_long_transfer(args) -> bool:
    """True for bd verbs that move a whole Dolt store (push/pull/clone/bootstrap/backup sync or
    restore); every other passthrough verb keeps the short STATE_TIMEOUT."""
    words = [str(a) for a in args if not str(a).startswith("-")]
    if not words:
        return False
    if words[0] == "dolt":
        return len(words) > 1 and words[1] in {"push", "pull", "clone"}
    if words[0] == "bootstrap":
        return True
    return words[0] == "backup" and len(words) > 1 and words[1] in {"sync", "restore"}


@dataclass(frozen=True)
class TransferTimeout:
    seconds: float
    source: str
    raise_hint: str


def transfer_timeout(args, cwd, *, env=None) -> TransferTimeout:
    """The bounded wait for a passthrough bd verb. Short verbs keep STATE_TIMEOUT. Long store
    transfers get the fsck allowance plus a transfer margin: an explicit BEADS_FSCK_TIMEOUT is
    honoured as-is; otherwise the store-sized allowance (as push_state), capped by the
    cap (BH_STATE_TIMEOUT_CAP, default MAX_FSCK_TIMEOUT). Never below STATE_TIMEOUT."""
    if not is_long_transfer(args):
        return TransferTimeout(STATE_TIMEOUT, "default STATE_TIMEOUT", "")
    environ = os.environ if env is None else env
    hint = f"set {FSCK_ENV} (e.g. {FSCK_ENV}=30m) or {CAP_ENV} (e.g. {CAP_ENV}=1h)"
    explicit = parse_duration(environ.get(FSCK_ENV))
    if explicit is not None:
        total = max(STATE_TIMEOUT, explicit + TRANSFER_MARGIN)
        return TransferTimeout(
            total, f"{FSCK_ENV}={environ.get(FSCK_ENV)} + {TRANSFER_MARGIN:g}s", hint
        )
    cap, source = float(MAX_FSCK_TIMEOUT), "default cap"
    configured = parse_duration(environ.get(CAP_ENV))
    if configured is not None:
        cap, source = configured, CAP_ENV
    gib = 1024**3
    sized = FSCK_TIMEOUT + ((_state_store_bytes(cwd) + gib - 1) // gib) * 60
    fsck = max(float(FSCK_TIMEOUT), min(cap, float(sized)))
    return TransferTimeout(
        max(STATE_TIMEOUT, fsck + TRANSFER_MARGIN),
        f"store-sized fsck allowance {fsck:g}s ({source}) + {TRANSFER_MARGIN:g}s",
        hint,
    )


def timeout_message(args, timeout: float, info: TransferTimeout | None) -> str:
    base = f"bd {' '.join(str(arg) for arg in args)} timed out after {timeout:g}s"
    if info is None or not info.raise_hint:
        return base
    msg = f"{base} (timeout source: {info.source}). To raise it, {info.raise_hint}."
    words = [str(a) for a in args if not str(a).startswith("-")]
    if words[:2] == ["dolt", "push"]:
        msg += " Supported path: bh hive sync remotes <hive> --push."
    return msg


@dataclass(frozen=True)
class FederationPeer:
    """One peer row from `bd federation status --json`. When `reachable` is False the counts
    are NOT trustworthy (bd reports -1/unknown); never read 0/0 as in-sync then."""

    peer: str
    url: str = ""
    reachable: bool = False
    reach_error: str = ""
    ahead: int = 0  # Status.LocalAhead
    behind: int = 0  # Status.LocalBehind
    has_conflicts: bool = False
    remote_observed_at: str = ""  # Status.LastSync; empty/zero means no dated knowledge


@dataclass(frozen=True)
class FederationStatus:
    """Outcome of `bd federation status --json`. `ok` means the command ran AND parsed;
    False ⇒ `error` says why ("timeout" | "parse-error" | stderr tail)."""

    ok: bool
    error: str = ""
    pending_changes: int = 0
    peers: tuple[FederationPeer, ...] = ()


@dataclass(frozen=True)
class SyncOutcome:
    """Outcome of `bd federation sync --json`. `paused` means bd hit conflicts with no
    strategy given and stopped; `conflicts` carries the conflicted table names. `no_peers`
    says bd found no peer TOWNS to federate with — a STATE, not a fault (see `_NO_PEERS`)."""

    ok: bool
    error: str = ""
    paused: bool = False
    conflicts: tuple[str, ...] = ()
    no_peers: bool = False


@dataclass(frozen=True)
class DoltRemoteEntry:
    """One registered Dolt remote as bd's server-side configuration reports it. ``url`` is the
    raw registration (it is compared, never rendered: render with ``dolt_remote.redact_url``)."""

    name: str
    url: str


@dataclass(frozen=True)
class RemoteListing:
    """Outcome of ``bd dolt remote list --json``. ``ok`` False means the read failed and the
    registration is UNKNOWN — never read an empty ``remotes`` as "no remote" then."""

    ok: bool
    error: str = ""
    remotes: tuple[DoltRemoteEntry, ...] = ()

    def get(self, name: str) -> DoltRemoteEntry | None:
        return next((remote for remote in self.remotes if remote.name == name), None)


@dataclass(frozen=True)
class RegistryRead:
    """Outcome of :meth:`Engine.read_registry`. ``ok`` False means the registry is UNKNOWN:
    never read an empty ``snapshot`` as "nothing registered" then. ``error`` is value-free."""

    ok: bool
    error: str = ""
    snapshot: object = None  # a dolt_remote.RegistrySnapshot when ok


@dataclass(frozen=True)
class RegistryOutcome:
    """Result of a registry-mutating operation (replace, cut-over, restore). ``before`` is the
    snapshot captured BEFORE any mutation; ``after`` is a fresh read-back. ``restored`` is True
    when a failure path put ``before`` back (and the read-back proves it). ``error`` and
    ``detail`` are redacted."""

    ok: bool
    error: str = ""
    before: object = None
    after: object = None
    restored: bool = False
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheRebuild:
    """Result of :meth:`Engine.rebuild_transport_cache`. ``state``: ``not-applicable`` (the
    remote is not a Git remote, so there is no transport cache), ``present`` (pull already
    works), ``rebuilt``, or ``failed``."""

    state: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state != "failed"


def _extra_env(env, extra_env):
    """``env`` (or the current environment) plus ``extra_env``; ``None`` when neither is set so
    the bd child keeps inheriting exactly as before."""
    if env is None and not extra_env:
        return None
    merged = dict(os.environ if env is None else env)
    merged.update(extra_env or {})
    return merged


def _unsupported(args, target, op):
    """An explicit, typed refusal shaped like a failed bd call (exit 2, nothing ran)."""
    dolt_remote = importlib.import_module("beadhive.dolt_remote")
    error = dolt_remote.UnsupportedRemoteOperation(target, op)
    result = subprocess.CompletedProcess(
        args=["bd", *args], returncode=2, stdout="", stderr=f"bh: {error}\n"
    )
    result.unsupported = error
    return result


def _refused(args, why: str):
    return subprocess.CompletedProcess(
        args=["bd", *args], returncode=1, stdout="", stderr=f"bh: {why}\n"
    )


_SYNC_REMOTE_KEY = "sync.remote"


def _hint_line(url: str) -> str:
    return f"{_SYNC_REMOTE_KEY}: {json.dumps(url)}\n"


#: The only ``metadata.json`` keys a store-less ``.beads/`` may carry before a clone: the
#: shared-server identity stamp that names the database the clone lands in (bh-rz379; the same
#: stamp hub hydration writes before its bootstrap, bh-fnn3d / bh-g5ujg).
_CLONE_IDENTITY_KEYS = frozenset({"dolt_mode", "dolt_database"})


def _initialised_store(beads: Path) -> bool:
    """Whether ``beads`` already holds a store a clone must not land over: Dolt data on disk,
    or a ``metadata.json`` recording more than the pre-clone identity stamp."""
    if any((beads / marker).exists() for marker in ("embeddeddolt", "dolt")):
        return True
    path = beads / "metadata.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and set(data) <= _CLONE_IDENTITY_KEYS)


def _drop_hint(config_path: Path, url: str, prior: str | None) -> None:
    """Remove the one-call bootstrap hint, keeping anything bd itself wrote meanwhile; restore
    the file's absence when bh created it and nothing else landed."""
    if not config_path.is_file():
        return
    hint = _hint_line(url)
    lines = [
        line
        for line in config_path.read_text().splitlines(keepends=True)
        if line != hint and line.rstrip("\n") != hint.rstrip("\n")
    ]
    if prior is None and not "".join(lines).strip():
        config_path.unlink()
        return
    config_path.write_text("".join(lines))


def _int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _stderr_tail(res) -> str:
    lines = (getattr(res, "stderr", "") or "").strip().splitlines()
    return lines[-1] if lines else ""


def _cross_line(res) -> str:
    """First stdout/stderr line starting with bd's failure glyph `✗`, else ''."""
    for stream in (getattr(res, "stdout", ""), getattr(res, "stderr", "")):
        for line in (stream or "").splitlines():
            if line.strip().startswith("✗"):
                return line.strip()
    return ""


def _sync_failure_marker(data: dict, res) -> str:
    """Name the in-band failure marker of a rc=0 federation sync, or '' when it is clean."""
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        peer = result.get("Peer") or "?"
        if result.get("Error") not in (None, ""):
            return f"Error from peer {peer}: {result['Error']!r}"
        if result.get("Merged") is False:
            return f"Merged: false for peer {peer}"
    cross = _cross_line(res)
    return f"✗ line: {cross}" if cross else ""


#: bd's word for "this store has no peer TOWNS", the one sync error that is not a fault.
#: Reproduced against the real binary (bd HEAD-af076b6) on a throwaway store whose only remote
#: was named `origin`: `bd federation sync --json` exits 1 with
#: ``{"error":"no federation peers configured (use 'bd federation add-peer' to add peers)"}``.
#: Matched as a substring so the parenthetical hint can change upstream without silently
#: reclassifying the state as a failure.
#:
#: bd counts as peers only OTHER BEADS INSTANCES: `runFederationSync` enumerates the store's dolt
#: remotes and skips the one named `origin`, because `origin` is UPSTREAM — hydrated by `bd
#: bootstrap`, moved afterwards by `bd dolt push`/`pull`, not something to federate with. So a
#: hive whose only remote is `origin` reports this, and that is correct: it has no peer towns.
_NO_PEERS = "no federation peers configured"


def _conflict_tables(val) -> list[str]:
    """Conflicted table names from a sync result's `Conflicts` value, defensively: bd emits a
    list; accept strings or dicts carrying a Table key, ignore anything else."""
    names = []
    for item in val if isinstance(val, list) else []:
        if isinstance(item, str) and item:
            names.append(item)
        elif isinstance(item, dict):
            table = item.get("Table") or item.get("table")
            if table:
                names.append(str(table))
    return names


def _forwarded(cwd, env):
    """The forward write path (bh-g7dlo): ``(env, refusal)`` for a bd bh runs in ``cwd``.

    A checkout this frame forwards (``bh hive forward point``) gets bd's server environment for
    the current primary (re-pointed first when the cached placement moved); every other
    checkout keeps ``env`` unchanged. A refused forward is returned as ``refusal`` and the
    caller fails closed without running bd. Resolved dynamically like ``host_fence`` below, to
    keep the forward module out of this legacy cycle."""
    if cwd is None:
        return env, None
    hive_forward = importlib.import_module("beadhive.hive_forward")
    try:
        if hive_forward.ensure_current(cwd) is None:
            return env, None
        return hive_forward.bd_env(cwd, base=env), None
    except hive_forward.ForwardError as exc:
        return env, str(exc)


def _forward_noop(cwd, args):
    """A state push/pull in a forwarded checkout is the primary's job: report and skip."""
    if cwd is None:
        return None
    hive_forward = importlib.import_module("beadhive.hive_forward")
    marker = hive_forward.read_marker(cwd)
    if marker is None:
        return None
    return subprocess.CompletedProcess(
        args=["bd", *args],
        returncode=0,
        stdout=(
            f"forwarded to {marker.frame or '?'}: the hive's primary publishes its data; "
            f"nothing to {' '.join(args[:2])} here\n"
        ),
        stderr="",
    )


class Engine(Protocol):
    """The operations `bh` needs from a beads-compatible backend."""

    name: str

    def invoke(
        self,
        args: list[str],
        *,
        cwd=None,
        actor: str = "",
        capture: bool = False,
        text_input=None,
        timeout: float = STATE_TIMEOUT,
        pin_process_cwd: bool = False,
        env=None,
        hive_aware: bool = True,
        no_work_markers: tuple[str, ...] = (),
        timeout_info=None,
    ):
        """Invoke the backend through the one bounded subprocess seam.

        ``hive_aware`` scopes through ``-C``; ``pin_process_cwd`` additionally sets the
        child's OS cwd for bd paths that consult git config. ``capture=False`` is the streaming
        variant. ``no_work_markers`` promotes bd's known exit-0 failure reports to a normal
        non-zero result instead of asking each caller to rediscover that edge case.
        """
        ...

    def passthrough(
        self,
        args: list[str],
        cwd,
        actor: str = "",
        capture: bool = False,
        text_input=None,
        timeout: float = STATE_TIMEOUT,
        pin_process_cwd: bool = False,
    ):
        """Issue management (create/list/dep/close/…) — an arbitrary bd-shaped subcommand
        scoped to `cwd`, attributed to `actor` when given. `timeout` (seconds) bounds the
        child so a wedged backend cannot block forever.

        `pin_process_cwd=True` (bh-s08me) additionally spawns the child with its OWN process
        cwd set to `cwd`, not just `bd -C <cwd>` on the command line. `bd`'s `-C` scopes which
        beads DB it opens but NOT which git repo it reads/writes GIT-CONFIG-backed keys
        against (e.g. `beads.role`) — those resolve off the real process cwd bd inherited, so
        `bd -C <hive-A> config get beads.role` run from inside hive B's directory silently
        answers for hive B. Opt-in (default False, unchanged shape for every other caller):
        only the git-config-backed callers need their process cwd pinned."""
        ...

    def export_jsonl(self, cwd, out_path, *, env=None):
        """Export `cwd`'s issues to the interchange JSONL at `out_path` (hub hydration)."""
        ...

    def stream_export_command(self, cwd, out_path) -> list[str]:
        """The export argv a host-local stream supervisor may own as a process tree.

        This is deliberately separate from :meth:`export_jsonl`: ordinary hydration keeps its
        historical call shape, while a long-lived stream can put the backend in a dedicated
        process group and cancel that whole group when its consumer disappears.
        """
        ...

    def list_gates(self, cwd):
        """List every open and resolved gate in ``cwd`` for one stream refresh."""
        ...

    def stream_gate_list_command(self, cwd) -> list[str]:
        """The all-states gate-list argv a host-local stream supervisor may own."""
        ...

    def import_jsonl(self, cwd, args: list[str]):
        """Run a `bd import`-shaped invocation (args carries flags + the JSONL source) in
        `cwd`."""
        ...

    def push_state(
        self,
        cwd,
        actor: str = "",
        message: str = "",
        *,
        remote: str = "",
        force: bool = False,
        extra_env=None,
    ):
        """Publish authoritative bead state (commit + push for `bd`/Dolt). `remote` targets a
        named remote instead of the default; `force` overwrites remote changes (`bd dolt push
        --remote/--force`). `extra_env` adds host credentials to the push child only."""
        ...

    def pull_state(self, cwd, *, remote: str = "", extra_env=None):
        """Refresh `cwd`'s bead state from the authoritative remote. `remote` pulls from a
        named remote instead of the default (`bd dolt pull --remote`). `extra_env` adds host
        credentials to the pull child only."""
        ...

    def bootstrap(self, cwd, *, env=None):
        """Fresh-clone hydration — materialize bead state with no prior local store."""
        ...

    def backup(self, cwd, dest, *, actor: str = ""):
        """Full-fidelity backup of `cwd`'s bead state to `dest` (a filesystem path) taken OVER
        THE CONNECTION to whichever engine is serving it — never by locating and copying a
        directory on local disk. This is what lets it work identically for `bd`'s embedded,
        owned, shared, and (future) external/remote-host modes: the connection doesn't care
        where the bytes physically live (bh-areg.1 design constraint)."""
        ...

    def backup_restore(self, cwd, source, *, actor: str = ""):
        """Restore `cwd`'s bead state from a full-fidelity backup at `source` (a filesystem
        path previously produced by `backup`) — the `backup` counterpart, also over the
        connection, force-overwriting the live database in place."""
        ...

    def state_channel(self, cwd) -> str:
        """The channel authoritative state rides — e.g. `refs/dolt/data` for `bd`/Dolt."""
        ...

    def federation_status(self, cwd, *, timeout: float = FEDERATION_TIMEOUT) -> FederationStatus:
        """Read-only peer sync status (`bd federation status`). Does a real network fetch
        per peer — callers own when to pay it."""
        ...

    def list_peers(self, cwd) -> tuple[str, ...]:
        """The configured peer NAMES — local state only, no network fetch (unlike
        `federation_status`), so it is cheap enough to probe speculatively."""
        ...

    def add_peer(self, cwd, name: str, url: str):
        """Register `url` as a federation peer named `name`. NOT idempotent — probe
        `list_peers` first."""
        ...

    def sync_state(
        self,
        cwd,
        *,
        peer: str | None = None,
        strategy: str | None = None,
        timeout: float = FEDERATION_TIMEOUT * 2,
    ) -> SyncOutcome:
        """Bidirectional peer sync (`bd federation sync`). With conflicts and no `strategy`
        (`ours`|`theirs`), bd pauses and reports the conflicted tables."""
        ...

    def list_remotes(self, cwd) -> RemoteListing:
        """The Dolt remotes registered in ``cwd``'s store (bd's server-side configuration —
        authoritative, unlike filesystem config). Local read; never a network fetch."""
        ...

    def get_remote(self, cwd, name: str) -> DoltRemoteEntry | None:
        """One named registration, or None (also None when the listing itself failed)."""
        ...

    def add_remote(self, cwd, name: str, url: str, *, extra_env=None):
        """Register ``url`` as ``name``. Refused (exit 2, typed) for a family bh does not
        implement. Idempotent for the same URL; REFUSES (exit 1) a different URL under an
        existing name, because bd 1.3.0's ``dolt remote add`` silently re-points it."""
        ...

    def remove_remote(self, cwd, name: str):
        """Drop the ``name`` registration. Removes configuration only — never remote data."""
        ...

    def replace_remote(self, cwd, name: str, url: str, *, extra_env=None):
        """Re-point ``name`` at ``url`` (remove + add, restoring the old URL if the add fails).
        An explicit operation for migration tooling; reconciliation never calls it."""
        ...

    def clone_remote(self, cwd, url: str, *, env=None, extra_env=None):
        """Initialise ``cwd``'s store from ``url`` with full history (``bd bootstrap`` against
        an explicit source). Refuses an already-initialised store; never treats a failed clone
        as an empty remote."""
        ...

    def read_registry(self, cwd) -> RegistryRead:
        """EVERY Dolt-level remote and backup registration of ``cwd``'s database, read below
        bd's listing (``dolt_remotes`` / ``dolt_backups``) and annotated with whether bd's own
        listing shows each remote. Local read; never a network fetch. A failed read is
        ``ok=False``, never an empty registry."""
        ...

    def query_rows(self, cwd, query: str) -> list[dict]:
        """Rows of a read-only SQL ``query`` against ``cwd``'s database in EITHER engine mode
        (server: ``bd sql``; embedded: the Dolt CLI on the database directory). Raises
        ``RuntimeError`` (value-free message) when the query cannot be answered, so a caller can
        never mistake a failed read for an empty result."""
        ...

    def add_backup(self, cwd, url: str, *, replace: bool = False):
        """Register ``url`` as the bd backup slot. Probe-then-add: refused when a remote
        already claims the address (Dolt would refuse it anyway, invisibly to bd), and refused
        when the slot holds a different URL unless ``replace``."""
        ...

    def remove_backup(self, cwd):
        """Drop the bd backup slot registration. Configuration only — never backup data."""
        ...

    def restore_registry(self, cwd, snapshot, *, extra_env=None) -> RegistryOutcome:
        """Make ``cwd``'s remote and backup registrations EXACTLY ``snapshot`` and prove it by
        read-back. Registration only; never moves data."""
        ...

    def cutover_remote(
        self, cwd, name: str, url: str, *, retain_as: str, extra_env=None
    ) -> RegistryOutcome:
        """Re-point the remote called ``name`` (the HQ descriptor's own name — never assume
        ``origin``) at ``url``, keeping the previous registration as ``retain_as``. The
        registry is captured first; any failure restores it exactly."""
        ...

    def rebuild_transport_cache(self, cwd, name: str, *, extra_env=None) -> CacheRebuild:
        """Rebuild (never copy) the Git transport cache of remote ``name`` when a pull reports
        it missing (bh-j42f0). A no-op for every non-Git family."""
        ...

    def remote_channel(self, cwd, name: str) -> str:
        """The channel state rides on remote ``name``: ``refs/dolt/data`` only for a Git
        remote, else the object-store location. ``unknown`` when unregistered."""
        ...


class BdEngine:
    """The `bd` (Dolt) adapter — today's only implementation.

    `cwd=None` contract (audited bh-r7mq.1, which fixed one violation): `None` means "inherit
    the caller's process cwd" — the sentinel `route.targets`' default no-`-a`/`-r` mode hands
    down. The invocation seam omits ``-C`` when cwd is None and passes the same sentinel through
    unchanged when process-cwd pinning is explicitly requested; it never manufactures the
    literal directory ``"None"``."""

    name = "bd"

    def invoke(
        self,
        args,
        *,
        cwd=None,
        actor="",
        capture=False,
        text_input=None,
        timeout=STATE_TIMEOUT,
        pin_process_cwd=False,
        env=None,
        hive_aware=True,
        no_work_markers=(),
        timeout_info=None,
    ):
        """Run one bounded bd process while preserving its real invocation variants."""
        cmd = ["bd"]
        if hive_aware and cwd is not None:
            cmd += ["-C", str(cwd)]
        if actor:
            cmd += ["--actor", actor]
        cmd += list(args)
        if hive_aware:
            env, refusal = _forwarded(cwd, env)
            if refusal is not None:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr=f"bh: {refusal}\n"
                )
        kw = {"check": False, "capture": capture, "timeout": timeout}
        if text_input is not None:
            kw["text_input"] = text_input
        if pin_process_cwd:
            kw["cwd"] = cwd if cwd is None else str(cwd)
        if env is not None:
            kw["env"] = env
        try:
            result = bd_mod._run(cmd, **kw)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=124,
                stdout="",
                stderr=timeout_message(args, timeout, timeout_info),
            )

        if result.returncode == 0 and no_work_markers:
            output = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"
            folded = output.casefold()
            marker = next((item for item in no_work_markers if item.casefold() in folded), "")
            if marker:
                prior = (getattr(result, "stderr", "") or "").rstrip()
                detail = f"Error: bd exited 0 but reported failure: {marker}"
                promoted = subprocess.CompletedProcess(
                    args=getattr(result, "args", cmd),
                    returncode=1,
                    stdout=getattr(result, "stdout", "") or "",
                    stderr=f"{prior}\n{detail}".lstrip(),
                )
                promoted.bd_no_work = True
                return promoted
        return result

    def passthrough(
        self,
        args,
        cwd,
        actor="",
        capture=False,
        text_input=None,
        timeout=STATE_TIMEOUT,
        pin_process_cwd=False,
    ):
        # Extracted from bd.py's `run()` (the shared bd-invocation helper work/plan/report/
        # triage all call).
        return self.invoke(
            args,
            cwd=cwd,
            actor=actor,
            capture=capture,
            text_input=text_input,
            timeout=timeout,
            pin_process_cwd=pin_process_cwd,
        )

    def _state_call(self, args, cwd, actor="", *, timeout=STATE_TIMEOUT, env=None):
        """Run a network-touching dolt state verb under a bounded timeout.

        A wedged remote surfaces as a NON-ZERO CompletedProcess (exit 124, the conventional
        timeout code) rather than an exception, so callers' existing returncode checks handle
        it unchanged — `work._pull_state` warns and continues, exactly as its docstring already
        promises ("any other pull failure is a warning, not a hard stop"). Without this a hung
        `bd dolt pull` wedges `bh work claim`/`resume` for the whole hive, because a hang is
        not a failure: the call never returns, so there is no returncode to inspect (bh-uxew).
        """
        return self.invoke(args, cwd=cwd, actor=actor, capture=True, timeout=timeout, env=env)

    def export_jsonl(self, cwd, out_path, *, env=None):
        # Extracted from hub.py's `sync()` (per-hive export ahead of hub `repo add`/`sync`).
        return self.invoke(["export", "-o", str(out_path)], cwd=cwd, env=env, capture=True)

    def stream_export_command(self, cwd, out_path):
        """Return the same export command for the stream's process-tree supervisor."""

        return ["bd", "-C", str(cwd), "export", "-o", str(out_path)]

    def list_gates(self, cwd):
        """Read every gate once; ``--limit 0`` avoids bd's default 50-row truncation."""

        return self.invoke(
            ["gate", "list", "--limit", "0", "--all", "--json"], cwd=cwd, capture=True
        )

    def stream_gate_list_command(self, cwd):
        return ["bd", "-C", str(cwd), "gate", "list", "--limit", "0", "--all", "--json"]

    def import_jsonl(self, cwd, args):
        # Extracted from bd.py's `import_labeled()` final write. `cwd` here is a real
        # subprocess `cwd=` kwarg (not a `-C <path>` cmd-line flag like the other methods
        # below), so it must be passed through UNSTRINGIFIED: `cwd=None` is subprocess's
        # "inherit the parent's cwd" sentinel (the default no `-a`/`-r` passthrough route,
        # route.targets' "cwd" mode, hits this with cwd=None), and `str(None)` silently
        # becomes the literal directory name "None" — bh-r7mq.1, a regression from b089341's
        # extraction (the original inline body used `cwd=cwd`).
        return self.invoke(
            ["import", *args],
            cwd=cwd,
            capture=True,
            hive_aware=False,
            pin_process_cwd=True,
        )

    def push_state(self, cwd, actor="", message="", *, remote="", force=False, extra_env=None):
        # Extracted from report.py's `file_report()` cache-push tail: commit (result unchecked,
        # matching the original — an empty commit is not itself a failure) then push.
        # Both go through `_state_call`: the push is the network leg, and the commit can itself
        # block on the dolt LOCK a wedged sibling process is holding.
        skipped = _forward_noop(cwd, ["dolt", "push"])
        if skipped is not None:
            return skipped
        committed = self._state_call(["dolt", "commit", "-m", message], cwd, actor=actor)
        args = ["dolt", "push"]
        if remote:
            args += ["--remote", remote]
        if force:
            args.append("--force")
        # Cut-over hives only (dormant elsewhere: a legacy hive answers None without a read).
        # Server mode refuses the push when that commit failed (the guard's marks would stay
        # behind), and a frame whose remote names a higher bh_writer.epoch never pushes main: it
        # diverts its unpublished commits to frame/<id>/orphan-<epoch>-<n> and resets (bh-4z3oz).
        fence_orphan = importlib.import_module("beadhive.fence_orphan")
        try:
            cfg = config.load()
        except (RuntimeError, ValueError) as exc:
            return subprocess.CompletedProcess(
                args=["bd", *args],
                returncode=1,
                stdout="",
                stderr=f"epoch-fence preflight refused state push: {exc}",
            )
        refused = fence_orphan.managed_preflight(cwd, commit_result=committed, cfg=cfg)
        if refused:
            return subprocess.CompletedProcess(
                args=["bd", *args], returncode=1, stdout="", stderr=refused
            )
        # Current bd deliberately invokes its internal Git transport with
        # `core.hooksPath=/dev/null`; the transport pre-push hook is therefore not an
        # enforcement point. Reserve the authoritative REMOTE fence immediately before the
        # opaque operation and verify the exact ticket immediately after it. This is the
        # strongest available managed boundary, but remains sequenced rather than atomic (the
        # postflight error is explicit that data may already have landed).
        # Legacy engine.py already sits on a large owned import cycle with bd. Importing the
        # fence eagerly would pull the safety module into that cycle, so resolve this literal
        # module at the operation boundary (the architecture checker permits this for legacy
        # modules and still verifies non-legacy dynamic imports).
        host_fence = importlib.import_module("beadhive.host_fence")
        gitref = importlib.import_module("beadhive.gitref")
        fence_remote = self._fence_remote(cwd, remote, cfg)
        try:
            reservation = (
                host_fence.reserve_managed_push(fence_remote, cwd=cwd, cfg=cfg)
                if fence_remote
                else None
            )
        except (
            host_fence.FenceError,
            gitref.RemoteUnreachable,
            RuntimeError,
            ValueError,
        ) as exc:
            return subprocess.CompletedProcess(
                args=["bd", *args],
                returncode=1,
                stdout="",
                stderr=f"epoch-fence preflight refused state push: {exc}",
            )

        store_bytes = _state_store_bytes(cwd)
        fsck_timeout = _fsck_timeout(cwd)
        push_env = dict(os.environ)
        push_env.update(extra_env or {})
        push_env.setdefault("BEADS_FSCK_TIMEOUT", str(fsck_timeout))
        started = time.monotonic()
        pushed = self._state_call(args, cwd, actor=actor, timeout=fsck_timeout + 60.0, env=push_env)
        elapsed = time.monotonic() - started
        stderr = (getattr(pushed, "stderr", "") or "").lower()
        if pushed.returncode and "fsck" in stderr and "timed out" in stderr:
            prior = (getattr(pushed, "stderr", "") or "").rstrip()
            pushed = subprocess.CompletedProcess(
                args=getattr(pushed, "args", ["bd", *args]),
                returncode=pushed.returncode,
                stdout=getattr(pushed, "stdout", "") or "",
                stderr=(
                    f"{prior}\nIntegrity scan timed out after {elapsed:.1f}s for a "
                    f"{store_bytes}-byte local store. Retry safely with "
                    f"BEADS_FSCK_TIMEOUT={min(MAX_FSCK_TIMEOUT, fsck_timeout * 2)} "
                    "bh hive sync --push; the timeout alone does not indicate corruption. "
                    "For a running shared server use SQL `CALL DOLT_GC()`; use offline "
                    "`dolt gc` only when that server is stopped."
                ),
            )
        if pushed.returncode:
            # A push rejected as a non-fast-forward may have lost a race with an adopt that
            # landed after the preflight: re-check, and divert rather than retry a stale main.
            rejected = fence_orphan.is_non_fast_forward(pushed)
            refused = fence_orphan.managed_preflight(cwd, cfg=cfg) if rejected else None
            if refused:
                prior = (getattr(pushed, "stderr", "") or "").rstrip()
                return subprocess.CompletedProcess(
                    args=getattr(pushed, "args", ["bd", *args]),
                    returncode=pushed.returncode,
                    stdout=getattr(pushed, "stdout", "") or "",
                    stderr=f"{prior}\n{refused}".lstrip(),
                )
            return pushed
        if reservation is None:
            return pushed
        try:
            host_fence.verify_managed_push(fence_remote, cwd=cwd, reservation=reservation)
        except (
            host_fence.FenceError,
            gitref.RemoteUnreachable,
            RuntimeError,
            ValueError,
        ) as exc:
            prior = (getattr(pushed, "stderr", "") or "").rstrip()
            detail = f"epoch-fence postflight failed: {exc}"
            return subprocess.CompletedProcess(
                args=getattr(pushed, "args", ["bd", *args]),
                returncode=1,
                stdout=getattr(pushed, "stdout", "") or "",
                stderr=f"{prior}\n{detail}".lstrip(),
            )
        return pushed

    def _fence_remote(self, cwd, remote, cfg):
        """The remote the Git epoch fence addresses for a push to ``remote``, or ``""`` when no
        Git-ref fence applies. The name is the caller's (the HQ descriptor's own name); with no
        name given it is the sole registered remote. ``origin`` is only the last-resort default
        for a hive that registers several. The fence is a Git ref (``capabilities.git_ref_fence``)
        so a file/S3/hosted remote is never fenced through a Git remote of the same name.
        Resolved only for a hive already in the multi-host model, so a legacy push makes no
        extra bd call."""
        name = remote or "origin"
        guard = importlib.import_module("beadhive.guard")
        try:
            in_force = guard.primary_state(cfg=cfg, hive_dir=Path(cwd)) is not None
        except Exception:  # noqa: BLE001 - dormancy probe; reserve_managed_push re-decides
            return name
        if not in_force:
            return name
        listing = self.list_remotes(cwd)
        if not listing.ok:
            return name
        if not remote and len(listing.remotes) == 1:
            name = listing.remotes[0].name
        entry = listing.get(name)
        if entry is None:
            return name
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        target = dolt_remote.classify(entry.url)
        return name if target.capabilities.git_ref_fence else ""

    def pull_state(self, cwd, *, remote="", extra_env=None):
        skipped = _forward_noop(cwd, ["dolt", "pull"])
        if skipped is not None:
            return skipped
        args = ["dolt", "pull"]
        if remote:
            args += ["--remote", remote]
        return self._state_call(args, cwd, env=_extra_env(None, extra_env))

    # ---- remote lifecycle (bh-b1scz.3; docs/design/hq-dolt-remote-descriptor-adr.md) ----

    def list_remotes(self, cwd):
        # Verified output shape (bd 1.3.0, real binary): `[]` with no remotes, else
        # `[{"name","url","sql_url","status"}]`. Local state only: bd reads the SQL-backed
        # `dolt_remotes` the running server serves, which is the authoritative registration.
        res = self.invoke(["dolt", "remote", "list", "--json"], cwd=cwd, capture=True)
        if res.returncode == 124:
            return RemoteListing(ok=False, error="timeout")
        if res.returncode != 0:
            return RemoteListing(ok=False, error=_stderr_tail(res) or f"exit {res.returncode}")
        try:
            data = json.loads(res.stdout or "")
        except ValueError:
            data = None
        if not isinstance(data, list):
            return RemoteListing(ok=False, error="parse-error")
        remotes = tuple(
            DoltRemoteEntry(name=str(row.get("name") or ""), url=str(row.get("url") or ""))
            for row in data
            if isinstance(row, dict) and row.get("name")
        )
        return RemoteListing(ok=True, remotes=remotes)

    def get_remote(self, cwd, name):
        listing = self.list_remotes(cwd)
        return listing.get(name) if listing.ok else None

    def add_remote(self, cwd, name, url, *, extra_env=None):
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        args = ["dolt", "remote", "add", str(name), str(url)]
        target = dolt_remote.classify(url)
        if not target.allows(dolt_remote.RemoteOp.ADD):
            return _unsupported(args, target, dolt_remote.RemoteOp.ADD)
        # Measured on bd 1.3.0: `dolt remote add` with an EXISTING name exits 0 and silently
        # re-points it. An add must never be a hidden replace, so probe first.
        listing = self.list_remotes(cwd)
        if not listing.ok:
            return _refused(args, f"cannot read current remotes ({listing.error}); nothing added")
        prior = listing.get(str(name))
        if prior is not None:
            if dolt_remote.same_remote(prior.url, str(url)):
                return subprocess.CompletedProcess(
                    args=["bd", *args], returncode=0, stdout="", stderr=""
                )
            return _refused(
                args,
                f"remote {name!r} is already registered at "
                f"{dolt_remote.redact_url(prior.url)}; not replaced (use replace_remote)",
            )
        blocked = self._backup_claims(cwd, str(url), args, "added")
        if blocked is not None:
            return blocked
        return self.invoke(args, cwd=cwd, capture=True, env=_extra_env(None, extra_env))

    def remove_remote(self, cwd, name):
        return self.invoke(["dolt", "remote", "remove", str(name)], cwd=cwd, capture=True)

    def replace_remote(self, cwd, name, url, *, extra_env=None):
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        args = ["dolt", "remote", "add", str(name), str(url)]
        target = dolt_remote.classify(url)
        if not target.allows(dolt_remote.RemoteOp.REPLACE):
            return _unsupported(args, target, dolt_remote.RemoteOp.REPLACE)
        listing = self.list_remotes(cwd)
        if not listing.ok:
            return _refused(
                args, f"cannot read current remotes ({listing.error}); nothing replaced"
            )
        prior = listing.get(str(name))
        blocked = self._backup_claims(cwd, str(url), args, "replaced")
        if blocked is not None:
            return blocked
        # bd's add re-points an existing name in one call, so a replace is a single add; the
        # prior URL is restored if that add fails part-way.
        added = self.invoke(args, cwd=cwd, capture=True, env=_extra_env(None, extra_env))
        if added.returncode and prior is not None:
            restored = self.invoke(
                ["dolt", "remote", "add", str(name), prior.url], cwd=cwd, capture=True
            )
            note = "restored the previous URL" if restored.returncode == 0 else "RESTORE FAILED"
            added = subprocess.CompletedProcess(
                args=getattr(added, "args", ["bd", *args]),
                returncode=added.returncode,
                stdout=getattr(added, "stdout", "") or "",
                stderr=f"{(getattr(added, 'stderr', '') or '').rstrip()}\nbh: {note} for {name}\n",
            )
        return added

    def clone_remote(self, cwd, url, *, env=None, extra_env=None):
        """Full-history initialisation from an explicit URL (bd 1.3.0 evidence, ADR "Bootstrap
        from a descriptor"): ``bd bootstrap`` clones when ``sync.remote`` names a source and
        exits non-zero when the source holds no Dolt data. ``bd init --remote`` is deliberately
        NOT used: it reads "no data" as empty and mints a fresh store, which would turn a wrong
        URL or a failed authentication into a silently empty hive.

        The URL is handed to bd through ``.beads/config.yaml`` for this one call and removed
        afterwards (the file is tracked in a furnished hive); push and pull then use the
        registration bootstrap created."""
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        args = ["bootstrap", "--non-interactive"]
        target = dolt_remote.classify(url)
        if not target.allows(dolt_remote.RemoteOp.CLONE):
            return _unsupported(args, target, dolt_remote.RemoteOp.CLONE)
        from . import store_locator

        beads = store_locator.embedded_store_dir(Path(cwd)).parent  # the hive's .beads/
        if _initialised_store(beads):
            return _refused(args, "refusing to clone over an already-initialised beads store")
        config_path = beads / "config.yaml"
        prior = config_path.read_text() if config_path.is_file() else None
        kept = [
            line
            for line in (prior or "").splitlines(keepends=True)
            if not line.startswith(f"{_SYNC_REMOTE_KEY}:")
        ]
        if kept and not kept[-1].endswith("\n"):
            kept[-1] += "\n"
        beads.mkdir(parents=True, exist_ok=True)
        config_path.write_text("".join(kept) + _hint_line(url))
        try:
            result = self.invoke(
                args,
                cwd=cwd,
                env=_extra_env(env, extra_env),
                capture=True,
                hive_aware=False,
                pin_process_cwd=True,
            )
        finally:
            _drop_hint(config_path, url, prior)
        return result

    # ---- registration inspection (bh-ksmt0.3; remote-migration-transaction-adr.md) ----

    def _sql_rows(self, cwd, query):
        """Rows of a read-only ``query`` as ``(rows, error)``; exactly one is meaningful.

        Below bd's abstraction on purpose: ``bd dolt remote list`` hides remotes and has no
        backup listing (bh-wov4i). Server mode asks the database through ``bd sql`` (the SQL
        path bd refuses only for embedded); embedded mode has no bd SQL path, so the one Dolt
        binary reads the database directory directly."""
        store_locator = importlib.import_module("beadhive.store_locator")
        if cwd is not None and store_locator.is_embedded_mode(Path(cwd)):
            database = store_locator.embedded_database_dir(Path(cwd))
            if not database.is_dir():
                return None, "store-missing"
            run = importlib.import_module("beadhive.run").run
            try:
                res = run(
                    ["dolt", "sql", "-q", query, "-r", "json"],
                    cwd=str(database),
                    check=False,
                    capture=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                return None, "timeout"
        else:
            res = self.invoke(["sql", "--json", query], cwd=cwd, capture=True, timeout=60)
        if res.returncode == 124:
            return None, "timeout"
        if res.returncode != 0:
            return None, _stderr_tail(res) or f"exit {res.returncode}"
        try:
            data = json.loads(res.stdout or "")
        except ValueError:
            return None, "parse-error"
        # `dolt sql -r json` prints `{}` (no `rows` key) for an empty result set.
        rows = data.get("rows", []) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return None, "parse-error"
        return [row for row in rows if isinstance(row, dict)], ""

    def query_rows(self, cwd, query):
        rows, error = self._sql_rows(cwd, query)
        if rows is None:
            raise RuntimeError(f"sql read failed: {error}")
        return rows

    def read_registry(self, cwd):
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        kind = dolt_remote.RegistryKind
        remote_rows, error = self._sql_rows(cwd, "SELECT name, url FROM dolt_remotes")
        if remote_rows is None:
            return RegistryRead(ok=False, error=f"remotes: {error}")
        backup_rows, error = self._sql_rows(cwd, "SELECT name, url FROM dolt_backups")
        if backup_rows is None:
            return RegistryRead(ok=False, error=f"backups: {error}")
        listing = self.list_remotes(cwd)
        shown = {r.name for r in listing.remotes} if listing.ok else None

        def entries(which, rows, listed):
            return tuple(
                dolt_remote.RegistryEntry(
                    which,
                    str(row.get("name") or ""),
                    str(row.get("url") or ""),
                    listed(str(row.get("name") or "")),
                )
                for row in rows
                if row.get("name")
            )

        snapshot = dolt_remote.RegistrySnapshot(
            remotes=entries(
                kind.REMOTE, remote_rows, lambda n: None if shown is None else n in shown
            ),
            backups=entries(kind.BACKUP, backup_rows, lambda _n: None),
        )
        return RegistryRead(ok=True, snapshot=snapshot)

    def _backup_claims(self, cwd, url, args, verb):
        """A refusal when a registered BACKUP already claims ``url`` (Dolt would refuse the
        remote anyway, with an error naming something bd cannot list), else ``None``. An
        unreadable registry also refuses: a conflict cannot be ruled out."""
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        read = self.read_registry(cwd)
        if not read.ok:
            return _refused(args, f"cannot read the Dolt registry ({read.error}); nothing {verb}")
        claim = read.snapshot.claimants(url, kind=dolt_remote.RegistryKind.BACKUP)
        if claim:
            return _refused(
                args,
                f"{claim[0].describe()} already claims this address; a remote and a backup "
                f"cannot share one; nothing {verb}",
            )
        return None

    def _dolt_write(self, cwd, kind, op, name, url=None, *, extra_env=None):
        """One registration write with no bd command: hidden remotes and non-default backups
        (the cases the seam cannot reach). Server mode goes through ``DOLT_REMOTE`` /
        ``DOLT_BACKUP``, embedded through the Dolt CLI on the database directory."""
        store_locator = importlib.import_module("beadhive.store_locator")
        argv = [str(name)] + ([str(url)] if op == "add" else [])
        if store_locator.is_embedded_mode(Path(cwd)):
            run = importlib.import_module("beadhive.run").run
            database = store_locator.embedded_database_dir(Path(cwd))
            return run(
                ["dolt", kind, op, *argv],
                cwd=str(database),
                check=False,
                capture=True,
                timeout=60,
                env=_extra_env(None, extra_env),
            )

        def quote(value):
            return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"

        proc = "DOLT_REMOTE" if kind == "remote" else "DOLT_BACKUP"
        call = f"CALL {proc}({', '.join(quote(a) for a in [op, *argv])})"
        return self.invoke(["sql", call], cwd=cwd, capture=True, env=_extra_env(None, extra_env))

    def add_backup(self, cwd, url, *, replace=False):
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        args = ["backup", "add", str(url)]
        read = self.read_registry(cwd)
        if not read.ok:
            return _refused(args, f"cannot read the Dolt registry ({read.error}); nothing added")
        snapshot = read.snapshot
        claim = snapshot.claimants(str(url), kind=dolt_remote.RegistryKind.REMOTE)
        if claim:
            return _refused(
                args,
                f"{claim[0].describe()} already claims this address; a backup cannot share it; "
                "nothing added",
            )
        slot = snapshot.backup(dolt_remote.BD_BACKUP_SLOT)
        if slot is not None:
            if dolt_remote.same_remote(slot.url, str(url)):
                return subprocess.CompletedProcess(
                    args=["bd", *args], returncode=0, stdout="", stderr=""
                )
            if not replace:
                return _refused(
                    args,
                    f"backup slot is already registered at {slot.redacted}; not replaced "
                    "(pass replace=True)",
                )
        return self._state_call(args, cwd)

    def remove_backup(self, cwd):
        return self._state_call(["backup", "remove"], cwd)

    def restore_registry(self, cwd, snapshot, *, extra_env=None):
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        kind = dolt_remote.RegistryKind
        notes: list[str] = []
        failed = False

        def attempt(label, res):
            nonlocal failed
            if res.returncode:
                failed = True
                notes.append(f"{label}: {_stderr_tail(res) or 'exit ' + str(res.returncode)}")
                return False
            return True

        # Converge, do not assume one pass is enough: `bd backup remove` also drops bd's other
        # backup registration (`backup_export`) as collateral, which a single pass computed from
        # the entry diff would never put back (measured on bd 1.3.0). Each pass re-reads.
        for _pass in range(3):
            read = self.read_registry(cwd)
            if not read.ok:
                return RegistryOutcome(ok=False, error=f"cannot read the registry: {read.error}")
            delta = snapshot.diff(read.snapshot)
            if delta.empty:
                break
            failed = False
            for which, name in (*delta.added, *delta.changed):
                slot = which == kind.BACKUP and name == dolt_remote.BD_BACKUP_SLOT
                if slot and (which, name) in delta.changed:
                    continue  # `bd backup add` replaces the slot; removing it drops collateral
                if which == kind.REMOTE:
                    res = self.remove_remote(cwd, name)
                    if res.returncode:
                        res = self._dolt_write(cwd, "remote", "remove", name)
                else:
                    res = (
                        self.remove_backup(cwd)
                        if slot
                        else self._dolt_write(cwd, "backup", "remove", name)
                    )
                attempt(f"remove {which.value} {name!r}", res)
            for which, name in (*delta.removed, *delta.changed):
                want = snapshot.remote(name) if which == kind.REMOTE else snapshot.backup(name)
                if want is None:
                    continue
                if which == kind.REMOTE:
                    res = self.invoke(
                        ["dolt", "remote", "add", name, want.url],
                        cwd=cwd,
                        capture=True,
                        env=_extra_env(None, extra_env),
                    )
                elif name == dolt_remote.BD_BACKUP_SLOT:
                    res = self._state_call(["backup", "add", want.url], cwd)
                else:
                    res = self._dolt_write(
                        cwd, "backup", "add", name, want.url, extra_env=extra_env
                    )
                attempt(f"add {which.value} {name!r}", res)
        after = self.read_registry(cwd)
        if not after.ok:
            return RegistryOutcome(
                ok=False,
                error=f"restored but could not read back: {after.error}",
                before=snapshot,
                detail=tuple(notes),
            )
        remaining = snapshot.diff(after.snapshot)
        if failed or not remaining.empty:
            return RegistryOutcome(
                ok=False,
                error=f"registry does not match the snapshot: {remaining.describe()}",
                before=snapshot,
                after=after.snapshot,
                detail=tuple(notes),
            )
        return RegistryOutcome(ok=True, before=snapshot, after=after.snapshot, restored=True)

    def cutover_remote(self, cwd, name, url, *, retain_as, extra_env=None):
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        name, retain_as, url = str(name), str(retain_as), str(url)
        if not name or not retain_as or name == retain_as:
            return RegistryOutcome(ok=False, error="cut-over needs distinct non-empty names")
        target = dolt_remote.classify(url)
        if not target.allows(dolt_remote.RemoteOp.REPLACE):
            return RegistryOutcome(
                ok=False,
                error=str(
                    dolt_remote.UnsupportedRemoteOperation(target, dolt_remote.RemoteOp.REPLACE)
                ),
            )
        read = self.read_registry(cwd)
        if not read.ok:
            return RegistryOutcome(ok=False, error=f"cannot read the registry: {read.error}")
        before = read.snapshot
        prior = before.remote(name)
        if prior is None:
            return RegistryOutcome(
                ok=False,
                before=before,
                error=f"remote {name!r} is not registered; nothing to cut over",
            )
        if dolt_remote.same_remote(prior.url, url):
            return RegistryOutcome(
                ok=False, before=before, error=f"remote {name!r} already addresses the target"
            )
        taken = before.remote(retain_as)
        if taken is not None:
            return RegistryOutcome(
                ok=False,
                before=before,
                error=f"retain name {retain_as!r} is already registered at {taken.redacted}",
            )
        claim = before.claimants(url)
        if claim:
            return RegistryOutcome(
                ok=False, before=before, error=f"{claim[0].describe()} already claims the target"
            )

        def fail(why):
            back = self.restore_registry(cwd, before, extra_env=extra_env)
            return RegistryOutcome(
                ok=False,
                error=why if back.ok else f"{why}; RESTORE FAILED: {back.error}",
                before=before,
                after=back.after,
                restored=back.ok,
                detail=back.detail,
            )

        kept = self.invoke(
            ["dolt", "remote", "add", retain_as, prior.url],
            cwd=cwd,
            capture=True,
            env=_extra_env(None, extra_env),
        )
        if kept.returncode:
            return fail(f"could not retain {name!r} as {retain_as!r}: {_stderr_tail(kept)}")
        swapped = self.replace_remote(cwd, name, url, extra_env=extra_env)
        if swapped.returncode:
            return fail(f"could not re-point {name!r}: {_stderr_tail(swapped)}")
        after = self.read_registry(cwd)
        if not after.ok:
            return fail(f"cannot read back the registry: {after.error}")
        live, kept_entry = after.snapshot.remote(name), after.snapshot.remote(retain_as)
        if live is None or live.url != url or kept_entry is None or kept_entry.url != prior.url:
            return fail("registry does not read back as intended")
        return RegistryOutcome(ok=True, before=before, after=after.snapshot)

    def rebuild_transport_cache(self, cwd, name, *, extra_env=None):
        dolt_remote = importlib.import_module("beadhive.dolt_remote")
        entry = self.get_remote(cwd, str(name))
        if entry is None:
            return CacheRebuild("failed", f"remote {name!r} is not registered")
        clone_url = dolt_remote.git_clone_url(entry.url)
        if clone_url is None:
            return CacheRebuild(
                "not-applicable", f"{dolt_remote.classify(entry.url).family.value} remote"
            )
        pulled = self.pull_state(cwd, remote=str(name), extra_env=extra_env)
        if pulled.returncode == 0:
            return CacheRebuild("present")
        text = f"{getattr(pulled, 'stderr', '') or ''}\n{getattr(pulled, 'stdout', '') or ''}"
        found = re.search(r"not a git repository: '([^']+)'", text)
        if not found:
            return CacheRebuild("failed", _stderr_tail(pulled) or f"exit {pulled.returncode}")
        path = Path(found.group(1))
        # The path comes from Dolt's own error (the hash is not derivable, bh-j42f0), but it is
        # only ever a place Dolt keeps its transport cache, and never something that exists.
        if "git-remote-cache" not in path.parts or path.name != "repo.git":
            return CacheRebuild("failed", "refusing an unrecognised cache path")
        if path.exists() and any(path.iterdir()):
            return CacheRebuild("failed", "cache path exists and is not empty; not overwritten")
        path.parent.mkdir(parents=True, exist_ok=True)
        run = importlib.import_module("beadhive.run").run
        cloned = run(
            ["git", "clone", "--bare", "--quiet", clone_url, str(path)],
            check=False,
            capture=True,
            timeout=STATE_TIMEOUT,
            env=_extra_env(None, extra_env),
        )
        if cloned.returncode:
            return CacheRebuild(
                "failed",
                f"git clone of {dolt_remote.redact_url(entry.url)} failed ({cloned.returncode})",
            )
        again = self.pull_state(cwd, remote=str(name), extra_env=extra_env)
        if again.returncode:
            return CacheRebuild("failed", _stderr_tail(again) or f"exit {again.returncode}")
        return CacheRebuild("rebuilt")

    def remote_channel(self, cwd, name):
        entry = self.get_remote(cwd, str(name))
        if entry is None:
            return "unknown"
        return importlib.import_module("beadhive.dolt_remote").channel_for(entry.url)

    def backup(self, cwd, dest, *, actor=""):
        # `bd backup add` + `bd backup sync` — bd's own wrapper around Dolt-native
        # `CALL DOLT_BACKUP(...)` (verified against a real bd binary, bh-areg.1): a full
        # commit-history-and-branches copy taken over the SQL/embedded connection, so it works
        # unchanged regardless of which mode is serving `cwd`. `add` is idempotent for the same
        # destination (bd removes+re-adds under the hood), so repeat calls against the same
        # `dest` are safe. Both legs go through `_state_call` (network/engine-touching, same
        # bound as `push_state`) — `add` first, and `sync` only runs when it succeeds, so a
        # failed `add` is reported as-is rather than masked by a `sync` that had nothing to do.
        added = self._state_call(["backup", "add", str(dest)], cwd, actor=actor)
        if added.returncode:
            return added
        return self._state_call(["backup", "sync"], cwd, actor=actor)

    def backup_restore(self, cwd, source, *, actor=""):
        # `bd backup restore <source> --force` — the connection-oriented counterpart to
        # `backup` above, so restore works regardless of which mode is serving `cwd` too.
        return self._state_call(["backup", "restore", str(source), "--force"], cwd, actor=actor)

    def bootstrap(self, cwd, *, env=None):
        # Extracted from hub.py's `_fetch_cache()` ("bootstrap pulls refs/dolt/data"). Same
        # raw-`cwd=` kwarg shape as import_jsonl's fixed bug above, but NOT a regression and
        # not reachable with cwd=None today (audited bh-r7mq.1): the sole caller
        # (hub.py's `_fetch_cache`) always passes a resolved cache Path, and str() was already
        # here pre-extraction (b089341^:hub.py). Left as-is rather than pre-emptively
        # rewritten — flag it if a future caller ever threads a possibly-None cwd through.
        return self.invoke(
            ["bootstrap", "--non-interactive"],
            cwd=cwd,
            env=env,
            hive_aware=False,
            pin_process_cwd=True,
        )

    def state_channel(self, cwd) -> str:
        return "refs/dolt/data"

    def federation_status(self, cwd, *, timeout=FEDERATION_TIMEOUT):
        # Verified output shape (bd 2026-07): {"peers":[{"ReachError","Reachable",
        # "Status":{"HasConflicts","LocalAhead","LocalBehind","Peer",...},"URL"}],
        # "pendingChanges":N,"schema_version":1}. `Status` may be absent and the counts are
        # -1/unknown when unreachable — parse with .get throughout and never coerce a
        # failure/unreachable result into looking in-sync.
        res = self.invoke(
            ["federation", "status", "--json"], cwd=cwd, capture=True, timeout=timeout
        )
        if res.returncode == 124:
            return FederationStatus(ok=False, error="timeout")
        if res.returncode != 0:
            return FederationStatus(ok=False, error=_stderr_tail(res) or f"exit {res.returncode}")
        try:
            data = json.loads(res.stdout or "")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return FederationStatus(ok=False, error="parse-error")
        peers = []
        for raw in data.get("peers") or []:
            if not isinstance(raw, dict):
                continue
            status = raw.get("Status")
            if not isinstance(status, dict):
                status = {}
            peers.append(
                FederationPeer(
                    peer=str(status.get("Peer") or raw.get("Peer") or ""),
                    url=str(raw.get("URL") or ""),
                    reachable=bool(raw.get("Reachable")),
                    reach_error=str(raw.get("ReachError") or ""),
                    ahead=_int(status.get("LocalAhead")),
                    behind=_int(status.get("LocalBehind")),
                    has_conflicts=bool(status.get("HasConflicts")),
                    remote_observed_at=str(status.get("LastSync") or ""),
                )
            )
        return FederationStatus(
            ok=True, pending_changes=_int(data.get("pendingChanges")), peers=tuple(peers)
        )

    def list_peers(self, cwd) -> tuple[str, ...]:
        # Verified output shape (bd 2026-08, real binary): `[{"Name":…,"URL":…}]`, and `[]` with
        # exit 0 when none are configured. Purely LOCAL state (the peers are dolt remotes in
        # `.dolt/repo_state.json`) — no network fetch, so unlike `federation_status` this costs
        # nothing to ask. A failed/unparseable call reports no peers: the only caller uses this
        # to decide whether registration is NEEDED, and a failed `add_peer` reports itself.
        res = self.invoke(["federation", "list-peers", "--json"], cwd=cwd, capture=True)
        if res.returncode != 0:
            return ()
        try:
            data = json.loads(res.stdout or "")
        except ValueError:
            return ()
        if not isinstance(data, list):
            return ()
        names = (str(r.get("Name") or "") for r in data if isinstance(r, dict))
        return tuple(n for n in names if n)

    def add_peer(self, cwd, name, url):
        # `bd federation add-peer <name> <url>` — bd's own surface for this; bd bootstrap
        # exposes no peer flag (checked `bd bootstrap --help`). NOT idempotent: verified against
        # a real bd binary, a second add of the same name exits 1 with "remote already exists".
        return self.invoke(["federation", "add-peer", str(name), str(url)], cwd=cwd, capture=True)

    def sync_state(self, cwd, *, peer=None, strategy=None, timeout=FEDERATION_TIMEOUT * 2):
        # Verified output shapes (bd 2026-07): success → {"peers":["hub"],"results":[{"Peer",
        # "Conflicts":null|[tables],"Fetched","Merged","Pushed",...}],"schema_version":1};
        # failure → {"error":"...","schema_version":1} with rc=1. On conflicts with no
        # strategy bd pauses ("Run 'bd federation sync --strategy ours|theirs' to resolve
        # conflicts") and lists the conflicted tables per result.
        cmd = ["federation", "sync"]
        if peer:
            cmd += ["--peer", peer]
        if strategy:
            cmd += ["--strategy", strategy]
        cmd += ["--json"]
        res = self.invoke(cmd, cwd=cwd, capture=True, timeout=timeout)
        if res.returncode == 124:
            return SyncOutcome(ok=False, error="timeout")
        try:
            data = json.loads(res.stdout or "")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return SyncOutcome(
                ok=False, error=_cross_line(res) or _stderr_tail(res) or "parse-error"
            )
        conflicts = _conflict_tables(data.get("conflicts"))
        for result in data.get("results") or []:
            if isinstance(result, dict):
                conflicts += _conflict_tables(result.get("Conflicts"))
        if conflicts and strategy is None:
            return SyncOutcome(ok=False, error="conflicts", paused=True, conflicts=tuple(conflicts))
        if res.returncode != 0:
            err = str(data.get("error") or "") or _stderr_tail(res) or f"exit {res.returncode}"
            return SyncOutcome(
                ok=False,
                error=err,
                conflicts=tuple(conflicts),
                no_peers=_NO_PEERS in err.lower(),
            )
        # bd exits 0 yet reports a refused/failed merge in-band (bh-vje85 E9): `Merged: false`,
        # a non-null `Error` (Go serialises an error value as `{}`), or a `✗` line.
        failure = _sync_failure_marker(data, res)
        if failure:
            return SyncOutcome(ok=False, error=failure, conflicts=tuple(conflicts))
        return SyncOutcome(ok=True, conflicts=tuple(conflicts))


_BD_ENGINE = BdEngine()


def get_engine(cfg=None) -> Engine:
    """The configured beads engine (`beads.engine`, default `bd`) for `cfg` (loads config when
    omitted, falling back to `bd` when none is loadable yet — e.g. before `bh config init`).
    `bd` is the only adapter implemented; any other value is a config error until a sibling bead
    (bh-dw3e.8/.9/.10) adds it."""
    if cfg is None:
        try:
            cfg = config.load()
        except FileNotFoundError:
            cfg = None
    name = config.beads_engine(cfg) if cfg is not None else "bd"
    if name == "bd":
        return _BD_ENGINE
    raise ValueError(f"unknown beads engine {name!r} — only 'bd' is implemented today")
