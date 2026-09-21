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
FSCK_TIMEOUT = 300
PUSH_STATE_TIMEOUT = FSCK_TIMEOUT + 60.0


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


def _int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _stderr_tail(res) -> str:
    lines = (getattr(res, "stderr", "") or "").strip().splitlines()
    return lines[-1] if lines else ""


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
        self, cwd, actor: str = "", message: str = "", *, remote: str = "", force: bool = False
    ):
        """Publish authoritative bead state (commit + push for `bd`/Dolt). `remote` targets a
        named remote instead of the default; `force` overwrites remote changes (`bd dolt push
        --remote/--force`)."""
        ...

    def pull_state(self, cwd, *, remote: str = ""):
        """Refresh `cwd`'s bead state from the authoritative remote. `remote` pulls from a
        named remote instead of the default (`bd dolt pull --remote`)."""
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
    ):
        """Run one bounded bd process while preserving its real invocation variants."""
        cmd = ["bd"]
        if hive_aware and cwd is not None:
            cmd += ["-C", str(cwd)]
        if actor:
            cmd += ["--actor", actor]
        cmd += list(args)
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
                stderr=f"bd {' '.join(str(arg) for arg in args)} timed out after {timeout:g}s",
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

    def push_state(self, cwd, actor="", message="", *, remote="", force=False):
        # Extracted from report.py's `file_report()` cache-push tail: commit (result unchecked,
        # matching the original — an empty commit is not itself a failure) then push.
        # Both go through `_state_call`: the push is the network leg, and the commit can itself
        # block on the dolt LOCK a wedged sibling process is holding.
        self._state_call(["dolt", "commit", "-m", message], cwd, actor=actor)
        args = ["dolt", "push"]
        if remote:
            args += ["--remote", remote]
        if force:
            args.append("--force")
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
        fence_remote = remote or "origin"
        try:
            reservation = host_fence.reserve_managed_push(fence_remote, cwd=cwd, cfg=config.load())
        except (
            host_fence.FenceError,
            host_fence.RemoteUnreachable,
            RuntimeError,
            ValueError,
        ) as exc:
            return subprocess.CompletedProcess(
                args=["bd", *args],
                returncode=1,
                stdout="",
                stderr=f"epoch-fence preflight refused state push: {exc}",
            )

        push_env = dict(os.environ)
        push_env.setdefault("BEADS_FSCK_TIMEOUT", str(FSCK_TIMEOUT))
        pushed = self._state_call(args, cwd, actor=actor, timeout=PUSH_STATE_TIMEOUT, env=push_env)
        stderr = (getattr(pushed, "stderr", "") or "").lower()
        if pushed.returncode and "fsck" in stderr and "timed out" in stderr:
            prior = (getattr(pushed, "stderr", "") or "").rstrip()
            pushed = subprocess.CompletedProcess(
                args=getattr(pushed, "args", ["bd", *args]),
                returncode=pushed.returncode,
                stdout=getattr(pushed, "stdout", "") or "",
                stderr=(
                    f"{prior}\nRetry safely with BEADS_FSCK_TIMEOUT={FSCK_TIMEOUT * 2} "
                    "bh hive sync --push; the timeout alone does not indicate corruption."
                ),
            )
        if pushed.returncode or reservation is None:
            return pushed
        try:
            host_fence.verify_managed_push(fence_remote, cwd=cwd, reservation=reservation)
        except (
            host_fence.FenceError,
            host_fence.RemoteUnreachable,
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

    def pull_state(self, cwd, *, remote=""):
        args = ["dolt", "pull"]
        if remote:
            args += ["--remote", remote]
        return self._state_call(args, cwd)

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
            return SyncOutcome(ok=False, error=_stderr_tail(res) or "parse-error")
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
