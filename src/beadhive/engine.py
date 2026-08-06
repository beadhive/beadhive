"""Engine seam — the swappable operations `bh` needs from a beads-compatible backend.

Every bead operation `bh` runs today is a literal `bd` subprocess call, scattered inline across
bd.py/hub.py/report.py (docs/design/bead-backend-abstraction.md#the-seam). This module is that
seam: an `Engine` protocol naming exactly the operations `bh` itself needs (not a wrapper for
every tracker verb), and `BdEngine`, a PURE EXTRACTION of the bodies that used to live inline at
each call site — no behavior change, `bd` is still the only implementation. Modeled on dolt.py's
container-backend dispatch: a config key (`beads.engine`) selects a thin implementation, not a
plugin framework. `br`/`bw`/`nodb` adapters land in sibling beads (bh-dw3e.8/.9/.10); wiring
push_state/pull_state into `bh work` verbs is bh-dw3e.6.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Protocol

from . import bd as bd_mod
from . import config

FEDERATION_TIMEOUT = 60.0  # seconds — federation status is a real network fetch per peer
# seconds — dolt push/pull is a real bead-data transfer, so this is deliberately generous
# (2x FEDERATION_TIMEOUT) to avoid tripping on a large but legitimate sync. It exists to bound
# a WEDGED remote, not to police slow ones: past this, `_state_call` degrades to the warning
# path `work._pull_state` already documents rather than hanging the hive (bh-uxew).
STATE_TIMEOUT = 120.0


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
    strategy given and stopped; `conflicts` carries the conflicted table names."""

    ok: bool
    error: str = ""
    paused: bool = False
    conflicts: tuple[str, ...] = ()


def _int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _stderr_tail(res) -> str:
    lines = (getattr(res, "stderr", "") or "").strip().splitlines()
    return lines[-1] if lines else ""


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


class Engine(Protocol):
    """The operations `bh` needs from a beads-compatible backend."""

    name: str

    def passthrough(
        self,
        args: list[str],
        cwd,
        actor: str = "",
        capture: bool = False,
        text_input=None,
        timeout: float | None = None,
    ):
        """Issue management (create/list/dep/close/…) — an arbitrary bd-shaped subcommand
        scoped to `cwd`, attributed to `actor` when given. `timeout` (seconds) bounds the
        child so a wedged backend can't block forever; None keeps the historical
        wait-indefinitely behaviour for local, non-network subcommands."""
        ...

    def export_jsonl(self, cwd, out_path, *, env=None):
        """Export `cwd`'s issues to the interchange JSONL at `out_path` (hub hydration)."""
        ...

    def import_jsonl(self, cwd, args: list[str]):
        """Run a `bd import`-shaped invocation (args carries flags + the JSONL source) in
        `cwd`."""
        ...

    def push_state(self, cwd, actor: str = "", message: str = ""):
        """Publish authoritative bead state (commit + push for `bd`/Dolt)."""
        ...

    def pull_state(self, cwd):
        """Refresh `cwd`'s bead state from the authoritative remote."""
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


class BdEngine:
    """The `bd` (Dolt) adapter — today's only implementation. Every method is a pure
    extraction of a body that used to live inline at its call site (bd.py/hub.py/report.py);
    none of them change what gets run.

    `cwd=None` contract (audited bh-r7mq.1, which fixed one violation): `None` means "inherit
    the caller's process cwd" — the sentinel `route.targets`' default no-`-a`/`-r` mode hands
    down. `import_jsonl` passes `cwd` straight to `_run`'s `cwd=` kwarg, so it must stay
    unstringified (`str(None)` → the literal directory "None"). `passthrough`/`export_jsonl`/
    `federation_status`/`sync_state` instead bake `str(cwd)` into a `bd -C <path>` cmd-line
    flag — a distinct, safer failure mode (a clean `bd`-level "cannot use -C directory None"
    exit, not a Python `FileNotFoundError`) — and none of their current callers ever pass
    `cwd=None`, so left as-is; re-check this note if that ever changes."""

    name = "bd"

    def passthrough(self, args, cwd, actor="", capture=False, text_input=None, timeout=None):
        # Extracted from bd.py's `run()` (the shared bd-invocation helper work/plan/report/
        # triage all call).
        cmd = ["bd", "-C", str(cwd)]
        if actor:
            cmd += ["--actor", actor]
        cmd += list(args)
        kw = {"check": False, "capture": capture, "text_input": text_input}
        # Pass `timeout` only when set, so the call shape stays byte-identical for the many
        # non-network callers — this method's contract is a PURE extraction of bd.py's inline
        # `run()`, and the test doubles pin that shape (bh-uxew).
        if timeout is not None:
            kw["timeout"] = timeout
        return bd_mod._run(cmd, **kw)

    def _state_call(self, args, cwd, actor="", *, timeout=STATE_TIMEOUT):
        """Run a network-touching dolt state verb under a bounded timeout.

        A wedged remote surfaces as a NON-ZERO CompletedProcess (exit 124, the conventional
        timeout code) rather than an exception, so callers' existing returncode checks handle
        it unchanged — `work._pull_state` warns and continues, exactly as its docstring already
        promises ("any other pull failure is a warning, not a hard stop"). Without this a hung
        `bd dolt pull` wedges `bh work claim`/`resume` for the whole hive, because a hang is
        not a failure: the call never returns, so there is no returncode to inspect (bh-uxew).
        """
        try:
            return self.passthrough(args, cwd, actor=actor, capture=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args=["bd", *args],
                returncode=124,
                stdout="",
                stderr=f"bd {' '.join(args)} timed out after {timeout:g}s",
            )

    def export_jsonl(self, cwd, out_path, *, env=None):
        # Extracted from hub.py's `sync()` (per-hive export ahead of hub `repo add`/`sync`).
        cmd = ["bd", "-C", str(cwd), "export", "-o", str(out_path)]
        return bd_mod._run(cmd, env=env, check=False, capture=True)

    def import_jsonl(self, cwd, args):
        # Extracted from bd.py's `import_labeled()` final write. `cwd` here is a real
        # subprocess `cwd=` kwarg (not a `-C <path>` cmd-line flag like the other methods
        # below), so it must be passed through UNSTRINGIFIED: `cwd=None` is subprocess's
        # "inherit the parent's cwd" sentinel (the default no `-a`/`-r` passthrough route,
        # route.targets' "cwd" mode, hits this with cwd=None), and `str(None)` silently
        # becomes the literal directory name "None" — bh-r7mq.1, a regression from b089341's
        # extraction (the original inline body used `cwd=cwd`).
        return bd_mod._run(["bd", "import", *args], check=False, capture=True, cwd=cwd)

    def push_state(self, cwd, actor="", message=""):
        # Extracted from report.py's `file_report()` cache-push tail: commit (result unchecked,
        # matching the original — an empty commit is not itself a failure) then push.
        # Both go through `_state_call`: the push is the network leg, and the commit can itself
        # block on the dolt LOCK a wedged sibling process is holding.
        self._state_call(["dolt", "commit", "-m", message], cwd, actor=actor)
        return self._state_call(["dolt", "push"], cwd, actor=actor)

    def pull_state(self, cwd):
        return self._state_call(["dolt", "pull"], cwd)

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
        cmd = ["bd", "bootstrap", "--non-interactive"]
        return bd_mod._run(cmd, cwd=str(cwd), env=env, check=False)

    def state_channel(self, cwd) -> str:
        return "refs/dolt/data"

    def federation_status(self, cwd, *, timeout=FEDERATION_TIMEOUT):
        # Verified output shape (bd 2026-07): {"peers":[{"ReachError","Reachable",
        # "Status":{"HasConflicts","LocalAhead","LocalBehind","Peer",...},"URL"}],
        # "pendingChanges":N,"schema_version":1}. `Status` may be absent and the counts are
        # -1/unknown when unreachable — parse with .get throughout and never coerce a
        # failure/unreachable result into looking in-sync.
        cmd = ["bd", "-C", str(cwd), "federation", "status", "--json"]
        try:
            res = bd_mod._run(cmd, check=False, capture=True, timeout=timeout)
        except subprocess.TimeoutExpired:
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
        cmd = ["bd", "-C", str(cwd), "federation", "list-peers", "--json"]
        res = bd_mod._run(cmd, check=False, capture=True)
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
        cmd = ["bd", "-C", str(cwd), "federation", "add-peer", str(name), str(url)]
        return bd_mod._run(cmd, check=False, capture=True)

    def sync_state(self, cwd, *, peer=None, strategy=None, timeout=FEDERATION_TIMEOUT * 2):
        # Verified output shapes (bd 2026-07): success → {"peers":["hub"],"results":[{"Peer",
        # "Conflicts":null|[tables],"Fetched","Merged","Pushed",...}],"schema_version":1};
        # failure → {"error":"...","schema_version":1} with rc=1. On conflicts with no
        # strategy bd pauses ("Run 'bd federation sync --strategy ours|theirs' to resolve
        # conflicts") and lists the conflicted tables per result.
        cmd = ["bd", "-C", str(cwd), "federation", "sync"]
        if peer:
            cmd += ["--peer", peer]
        if strategy:
            cmd += ["--strategy", strategy]
        cmd += ["--json"]
        try:
            res = bd_mod._run(cmd, check=False, capture=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return SyncOutcome(ok=False, error="timeout")
        try:
            data = json.loads(res.stdout or "")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return SyncOutcome(ok=False, error=_stderr_tail(res) or "parse-error")
        conflicts = _conflict_tables(data.get("conflicts"))
        for result in data.get("results") or []:
            if isinstance(result, dict):
                conflicts += _conflict_tables(result.get("Conflicts"))
        if conflicts and strategy is None:
            return SyncOutcome(ok=False, error="conflicts", paused=True, conflicts=tuple(conflicts))
        if res.returncode != 0:
            err = str(data.get("error") or "") or _stderr_tail(res) or f"exit {res.returncode}"
            return SyncOutcome(ok=False, error=err, conflicts=tuple(conflicts))
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
