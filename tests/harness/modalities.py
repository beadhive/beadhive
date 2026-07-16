"""Developer modalities + the scripted coordinator→developer→merger flow.

- Supervised / AgentLocal drive the REAL `ws work` verbs (linked worktree).
- RemoteSandbox is harness-driven (clone + isolated config + injected key + dolt bootstrap),
  the executable spec for the not-yet-built `ws work` remote path — it exercises real
  BEADS-SYNC state transfer over the file:// dolt remote.

The "change" a developer makes is deterministic and content-irrelevant; the validation is the
resulting git history (authors, verified signatures, branch names, merge structure).
"""

from __future__ import annotations

from pathlib import Path

from beadhive import config, work, worktree
from beadhive.run import run

from . import beads
from .hive import Hive
from .world import Identity, git, progress


def branch_for(hive: Hive, bead: str) -> tuple[Path, str]:
    _entry, _main, target, branch = worktree.locate(config.load(), hive.prefix, bead)
    return target, branch


class Modality:
    name = "base"

    def work_block(self) -> dict:
        return {}

    def developer(self) -> Identity:
        raise NotImplementedError

    def assign(self, hive: Hive, bead: str):
        # assign is orchestrator-only (bead .38): dispatch as a dispatcher seat, not the
        # developer/config identity the modality otherwise runs work under.
        work.assign(bead=bead, to=self.developer().name, as_="disp/coordinator", hive=hive.prefix)

    def develop(self, hive: Hive, bead: str):
        raise NotImplementedError


class _LocalDev(Modality):
    """Same-filesystem developer: real `ws work` + one signed worktree commit."""

    def develop(self, hive: Hive, bead: str):
        work.claim(bead=bead, as_="", hive=hive.prefix)
        target, _branch = branch_for(hive, bead)
        (Path(target) / f"{bead}.txt").write_text("change")
        git("-C", str(target), "add", "-A")
        git("-C", str(target), "commit", "-qm", f"feat: implement {bead}")
        work.submit(bead=bead, hive=hive.prefix)


class SupervisedModality(_LocalDev):
    name = "supervised"

    def __init__(self, world):
        self._dev = world.human  # inherit the human identity + signing (no stamp)

    def developer(self) -> Identity:
        return self._dev


class AgentLocalModality(_LocalDev):
    name = "agent-local"

    def __init__(self, world):
        self._dev = world.identity("crew/agent-local", "agent-local@fixture", sign=True)

    def work_block(self) -> dict:
        d = self._dev
        return {
            "identity": {
                "mode": "agent",
                "name": d.name,
                "email": d.email,
                "signing_key": str(d.key),
                "sign": True,
            }
        }

    def developer(self) -> Identity:
        return self._dev


class RemoteSandboxModality(Modality):
    name = "remote-sandbox"

    def __init__(self, world):
        self.world = world
        self._dev = world.identity("crew/remote", "remote@fixture", sign=True)
        self.last_sandbox: Path | None = None

    def work_block(self) -> dict:
        d = self._dev
        return {
            "identity": {
                "mode": "agent",
                "name": d.name,
                "email": d.email,
                "signing_key": str(d.key),
                "sign": True,
            }
        }

    def developer(self) -> Identity:
        return self._dev

    def assign(self, hive: Hive, bead: str):
        # Stamp the assignee in the hive's bd, then PUBLISH to the dolt remote so a remote
        # sandbox's own `bd dolt pull` (during bootstrap) sees the assignment.
        beads.bd("update", bead, "--assignee", self._dev.name, cwd=hive.main, capture=True)
        beads.push(hive.main)

    def develop(self, hive: Hive, bead: str):
        d = self._dev
        sb = self.world.sandboxes / bead
        # fresh clone — separate object store, isolated config (empty global, /dev/null system).
        # -b main is explicit so the bead branch always descends from the shared base history.
        git("clone", "-q", "-b", "main", str(hive.git_remote), str(sb))
        # the sandbox's OWN embedded bd, cloned from the file:// dolt remote (sees assignment)
        (sb / ".beads").mkdir(parents=True, exist_ok=True)
        (sb / ".beads" / "config.yaml").write_text(f'sync.remote: "file://{hive.dolt_remote}"\n')
        run(["bd", "bootstrap", "--yes"], cwd=str(sb), check=True, capture=True, timeout=180)
        self.last_sandbox = sb
        # inject identity + signing (a real machine has no local key path — the orchestrator
        # provides it); fresh config means nothing leaks from the human.
        for k, v in {
            "user.name": d.name,
            "user.email": d.email,
            "gpg.format": "ssh",
            "user.signingkey": str(d.key),
            "commit.gpgsign": "true",
            "gpg.ssh.allowedSignersFile": str(self.world.allowed),
        }.items():
            git("-C", str(sb), "config", k, v)
        _target, branch = branch_for(hive, bead)
        git("-C", str(sb), "checkout", "-q", "-b", branch)
        (sb / f"{bead}.txt").write_text("change")
        git("-C", str(sb), "add", "-A")
        git("-C", str(sb), "commit", "-qm", f"feat: implement {bead}")
        git("-C", str(sb), "push", "-q", "origin", f"{branch}:{branch}")


# ---- coordinator + merger drivers -------------------------------------------


def _merge(hive: Hive, bead: str):
    """Refiner merges the approved bead branch into the integration branch with --no-ff."""
    _target, branch = branch_for(hive, bead)
    main = hive.main
    have = (
        git(
            "-C", str(main), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        ).returncode
        == 0
    )
    if not have:  # remote-sandbox pushed the branch to the bare git remote
        git("-C", str(main), "fetch", "-q", "origin", f"{branch}:{branch}")
    r = hive.world.refiner
    git(
        "-C",
        str(main),
        "-c",
        f"user.name={r.name}",
        "-c",
        f"user.email={r.email}",
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={r.key}",
        "-c",
        "commit.gpgsign=true",
        "merge",
        "--no-ff",
        "-m",
        f"chore(merge): bead {bead}",
        branch,
    )


def run_flow(hive: Hive, bead_ids: list[str], modality: Modality, label: str = "") -> list[str]:
    """Drive coordinator → developer → merger over the work graph using real `bd ready`
    ordering. Returns the merged order (parents before children)."""
    progress(f"▶ {label or hive.prefix} ({modality.name}): {len(bead_ids)} beads")
    remaining = list(bead_ids)
    order: list[str] = []
    while remaining:
        ready = [b for b in beads.ready_ids(hive.main) if b in remaining]
        assert ready, f"deadlock — none ready of {remaining}"
        for bead in ready:
            progress(f"  · {bead}: assign")
            modality.assign(hive, bead)
            progress(f"  · {bead}: develop")
            modality.develop(hive, bead)
            beads.resolve_gates(hive.main, bead)  # review approves
            progress(f"  · {bead}: merge")
            _merge(hive, bead)
            beads.close(hive.main, bead, actor=hive.world.refiner.name)
            progress(f"  ✓ {bead} landed")
            order.append(bead)
            remaining.remove(bead)
    return order
