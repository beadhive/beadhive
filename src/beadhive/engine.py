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
        self, args: list[str], cwd, actor: str = "", capture: bool = False, text_input=None
    ):
        """Issue management (create/list/dep/close/…) — an arbitrary bd-shaped subcommand
        scoped to `cwd`, attributed to `actor` when given."""
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

    def state_channel(self, cwd) -> str:
        """The channel authoritative state rides — e.g. `refs/dolt/data` for `bd`/Dolt."""
        ...

    def federation_status(self, cwd, *, timeout: float = FEDERATION_TIMEOUT) -> FederationStatus:
        """Read-only peer sync status (`bd federation status`). Does a real network fetch
        per peer — callers own when to pay it."""
        ...

    def sync_state(
        self, cwd, *, peer: str | None = None, strategy: str | None = None,
        timeout: float = FEDERATION_TIMEOUT * 2,
    ) -> SyncOutcome:
        """Bidirectional peer sync (`bd federation sync`). With conflicts and no `strategy`
        (`ours`|`theirs`), bd pauses and reports the conflicted tables."""
        ...


class BdEngine:
    """The `bd` (Dolt) adapter — today's only implementation. Every method is a pure
    extraction of a body that used to live inline at its call site (bd.py/hub.py/report.py);
    none of them change what gets run."""

    name = "bd"

    def passthrough(self, args, cwd, actor="", capture=False, text_input=None):
        # Extracted from bd.py's `run()` (the shared bd-invocation helper work/plan/report/
        # triage all call).
        cmd = ["bd", "-C", str(cwd)]
        if actor:
            cmd += ["--actor", actor]
        cmd += list(args)
        return bd_mod._run(cmd, check=False, capture=capture, text_input=text_input)

    def export_jsonl(self, cwd, out_path, *, env=None):
        # Extracted from hub.py's `sync()` (per-hive export ahead of hub `repo add`/`sync`).
        cmd = ["bd", "-C", str(cwd), "export", "-o", str(out_path)]
        return bd_mod._run(cmd, env=env, check=False, capture=True)

    def import_jsonl(self, cwd, args):
        # Extracted from bd.py's `import_labeled()` final write.
        return bd_mod._run(["bd", "import", *args], check=False, capture=True, cwd=str(cwd))

    def push_state(self, cwd, actor="", message=""):
        # Extracted from report.py's `file_report()` cache-push tail: commit (result unchecked,
        # matching the original — an empty commit is not itself a failure) then push.
        self.passthrough(["dolt", "commit", "-m", message], cwd, actor=actor, capture=True)
        return self.passthrough(["dolt", "push"], cwd, actor=actor, capture=True)

    def pull_state(self, cwd):
        return self.passthrough(["dolt", "pull"], cwd, capture=True)

    def bootstrap(self, cwd, *, env=None):
        # Extracted from hub.py's `_fetch_cache()` ("bootstrap pulls refs/dolt/data").
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
