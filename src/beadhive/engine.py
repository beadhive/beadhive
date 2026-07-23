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

from typing import Protocol

from . import bd as bd_mod
from . import config


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
