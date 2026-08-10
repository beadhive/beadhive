#!/usr/bin/env python3
"""Runnable demo of the `local` work-runtime tier (bh-c6dk.5) against a THROWAWAY hive.

Stands up a scratch hive in a temp directory (`git init` + `bd init`), seeds a small synthetic
molecule, and drives it to completion with the reference stub seat — printing every step of the
pass so an operator can watch the thing the bead is about:

    gate check -> reclaim -> host lease -> heartbeat -> caps -> harvest -> decide -> pick-claim
    -> spawn -> envelope -> advance

It exercises, on purpose:

* an **open `type:human` gate** holding a bead back, and a **`timer` gate** that self-resolves on
  `bd gate check` — the difference between "a runtime can advance this" and "only a person can";
* the **atomic pick-claim-provision verb** (`bh work next`), so the demo runs the same race-free
  claim any other driver does rather than a bespoke one;
* a **failure path**: a seat that reports `blocked`, whose cause is written into beads with
  `bd set-state` (an event bead + a `dispatch:blocked` label) and then read back and printed;
* a **cancellation**: a hung seat that trips the per-run wall-time cap and is stopped through the
  three-rung CANCEL ladder, coming back with a priced envelope, with its whole process GROUP
  reaped — then re-dispatched as a FRESH turn (recovery is re-dispatch, never `--resume_session`).

ISOLATION IS THE FIRST THING IT DOES, AND IT IS ASSERTED, NOT ASSUMED
---------------------------------------------------------------------
This demo must never touch `~/.beadhive`, any registered hive's `.beads/`, or this repo's own
beads. That discipline is what made the `bh-00cq` spike evidence trustworthy, so it is checked
rather than promised: :func:`verify_isolation` re-points every root bh reads (`BH_HOME`,
`BH_CONFIG`, `BH_WORKTREES`, `GIT_WORKSPACE`, `GIT_CONFIG_GLOBAL`, the bd shared-server dir),
proves each resolved path lands inside the scratch root, and takes a **tripwire snapshot** of the
real `~/.beadhive` and of this repo's `.beads/` which is re-checked at the end. Run

    uv run scripts/demo_local_loop.py --check-isolation-only

to see just that part (it needs no `bd` and no `bh`); `tests/test_localloop.py` runs exactly that
so the guarantee is covered by the suite.

USAGE
------
    uv run scripts/demo_local_loop.py                # scratch root under the system temp dir
    uv run scripts/demo_local_loop.py --root /tmp/x  # keep the scratch hive for inspection
    uv run scripts/demo_local_loop.py --check-isolation-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STUB_SEAT = REPO / "tests" / "fixtures" / "stub_seat.py"

ORG, REPO_NAME, PREFIX = "demo-org", "demo-hive", "dm"


# --------------------------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------------------------


class Tripwire:
    """A before/after fingerprint of a directory that MUST NOT change during the demo.

    Cheap and blunt on purpose: relative path + size + mtime for every file. It cannot be argued
    with, and it fails loudly at the end rather than leaving "we're pretty sure it was isolated"
    as the evidence.
    """

    #: Subtrees skipped when fingerprinting. `worktrees/` under a real `~/.beadhive` holds every
    #: hive's checked-out trees — 1.2M files on the machine this was written on — and the demo
    #: cannot reach it anyway, because `$BH_WORKTREES` is re-pointed into the scratch root and
    #: that is asserted separately. Walking it would make the guard so slow nobody runs it, which
    #: is a worse outcome than a narrower guard that always runs.
    SKIP = frozenset({"worktrees", ".git"})

    def __init__(self, label: str, path: Path):
        self.label = label
        self.path = path
        self.before = self._snapshot()

    def _snapshot(self) -> dict:
        out: dict[str, tuple[int, int]] = {}
        self._walk(self.path, out)
        return out

    def _walk(self, directory: Path, out: dict) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name in self.SKIP:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if entry.is_dir():
                self._walk(entry, out)
            else:
                out[str(entry.relative_to(self.path))] = (st.st_size, int(st.st_mtime))

    def assert_untouched(self) -> None:
        after = self._snapshot()
        if after != self.before:
            changed = sorted(set(after) ^ set(self.before)) or sorted(
                k for k in after if self.before.get(k) != after[k]
            )
            raise SystemExit(
                f"ISOLATION VIOLATION: {self.label} ({self.path}) changed during the demo: "
                f"{changed[:10]}"
            )


def isolate(root: Path) -> dict:
    """Re-point every root bh/bd/git reads at *root*, and return the resulting env overrides.

    Set in `os.environ` (not just passed to children) because the demo drives some of the loop
    in-process and shells out for the rest — both halves must see the same scratch world.
    """
    env = {
        "BH_HOME": str(root / "bh-home"),
        "BH_CONFIG": str(root / "bh-home" / "config.yaml"),
        "BH_WORKTREES": str(root / "worktrees"),
        "GIT_WORKSPACE": str(root / "workspace"),
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
        # bd resolves its shared-server dir/port from the ambient environment when unset, which
        # defaults to the operator's REAL fleet server. Point it somewhere disposable.
        "BEADS_SHARED_SERVER_DIR": str(root / "beads-shared"),
        "BH_SKIP_SETUP_CHECK": "1",
        "GIT_PAGER": "cat",
        "NO_COLOR": "1",
    }
    for key, value in env.items():
        os.environ[key] = value
    (root / "bh-home").mkdir(parents=True, exist_ok=True)
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / "gitconfig").write_text("[core]\n\texcludesFile = /dev/null\n")
    return env


def verify_isolation(root: Path) -> list[Tripwire]:
    """Prove the scratch world is the only world this demo can reach. Raises on any doubt."""
    from beadhive import config

    root = root.resolve()
    checks = {
        "config.home()": Path(config.home()).resolve(),
        "config.config_path()": Path(config.config_path()).resolve(),
        "$GIT_WORKSPACE": Path(os.environ["GIT_WORKSPACE"]).resolve(),
        "$BH_WORKTREES": Path(os.environ["BH_WORKTREES"]).resolve(),
    }
    for label, path in checks.items():
        if root not in path.parents and path != root:
            raise SystemExit(f"ISOLATION FAILURE: {label} resolves to {path}, outside {root}")
        print(f"  ✓ {label:<22} → {path}")

    real_home = Path.home() / ".beadhive"
    repo_beads = REPO / ".beads"
    for label, path in (("~/.beadhive", real_home), ("this repo's .beads/", repo_beads)):
        if path.exists() and (path == root or path in root.parents):
            raise SystemExit(f"ISOLATION FAILURE: scratch root {root} sits inside {label}")
        print(f"  ✓ {label:<22} is outside the scratch root (tripwire armed)")

    print("  ✓ isolation verified")
    return [Tripwire("~/.beadhive", real_home), Tripwire("repo .beads/", repo_beads)]


# --------------------------------------------------------------------------------------------
# The scratch hive
# --------------------------------------------------------------------------------------------


def sh(argv, cwd=None, check=True):
    res = subprocess.run(
        [str(a) for a in argv], cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )
    if check and res.returncode != 0:
        raise SystemExit(f"$ {' '.join(map(str, argv))}\n{res.stdout}\n{res.stderr}")
    return res


#: The seat identity the demo claims and closes beads as. `bd close` refuses to close a bead
#: held by someone else (correctly — that is claim fencing doing its job), so every write the
#: demo makes on a claimed bead is attributed to the holder rather than forced through.
DEMO_ACTOR = "dev/demo"


def bd(*args, cwd: Path, check=True, actor: str = ""):
    head = ["bd", "-C", str(cwd)]
    if actor:
        head += ["--actor", actor]
    return sh([*head, *args], check=check)


def bd_json(*args, cwd: Path):
    res = bd(*args, "--json", cwd=cwd, check=False)
    try:
        data = json.loads(res.stdout or "null")
    except json.JSONDecodeError:
        return None
    return data


def bd_row(*args, cwd: Path) -> dict:
    """One bead row. `bd show --json` answers with either an object or a 1-element list."""
    data = bd_json(*args, cwd=cwd)
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else {}


def build_hive(root: Path) -> Path:
    """A real git repo with a real embedded-Dolt bd store, registered in the scratch config."""
    from beadhive import config

    main = Path(os.environ["GIT_WORKSPACE"]) / "github" / ORG / REPO_NAME
    main.mkdir(parents=True, exist_ok=True)
    sh(["git", "init", "-q", "-b", "main", str(main)])
    for key, value in {
        "user.name": "demo human",
        "user.email": "demo@example.invalid",
        "commit.gpgsign": "false",
    }.items():
        sh(["git", "-C", str(main), "config", key, value])
    (main / "README.md").write_text("# demo hive\n")
    (main / ".gitignore").write_text(".beads/\nAGENTS.md\nCLAUDE.md\n")
    sh(["git", "-C", str(main), "add", "-A"])
    sh(["git", "-C", str(main), "commit", "-qm", "chore: init"])
    sh(["bd", "init", "--prefix", PREFIX, "--quiet"], cwd=main)

    config.save(
        {
            "schema_version": 1,
            "providers": ["github"],
            "otel": {"enabled": False},
            "work": {
                "validate_cmd": "true",
                "review_gate": "human",
                "identity": {"mode": "agent", "name": "dev/demo", "email": "demo@example.invalid"},
            },
            "managed_repos": [
                {
                    "provider": "github",
                    "org": ORG,
                    "repo": REPO_NAME,
                    "prefix": PREFIX,
                    "kind": "personal",
                }
            ],
        }
    )
    return main


def _created_id(res) -> str:
    """Pull the new bead id out of `bd create`'s human line (`✓ Created issue: dm-abc — title`).

    Not `--json`, deliberately: the demo shows the same output an operator would see if they
    typed these commands themselves, so the seeding step is copy-pasteable.
    """
    for line in (res.stdout or "").splitlines():
        if "Created issue:" in line:
            return line.split("Created issue:", 1)[1].strip().split()[0]
    raise SystemExit(f"could not parse a bead id out of: {res.stdout!r}")


def seed_molecule(main: Path) -> dict:
    """One epic, five children, and two gates — the smallest shape that shows every branch."""
    epic = _created_id(bd("create", "demo molecule", "-t", "epic", "-p", "1", cwd=main))
    children = {}
    for key, title in (
        ("happy", "a bead the seat completes"),
        ("blocked", "a bead the seat reports blocked"),
        ("hang", "a bead whose seat hangs and gets cancelled"),
        ("timed", "a bead behind a timer gate the runtime may resolve"),
        ("human", "a bead behind a human gate only a person may resolve"),
    ):
        children[key] = _created_id(
            bd("create", title, "-t", "task", "-p", "2", "--parent", epic, cwd=main)
        )
    # in_progress: what `bh work start <epic>` would have done. The demo drives the CHILD
    # lifecycle, not the epic-container provisioning, so the container is pre-opened.
    bd("update", epic, "--status", "in_progress", cwd=main)

    # A TIMER gate that `bd gate check` can resolve on its own, and a HUMAN gate it never will.
    bd(
        "gate",
        "create",
        "--blocks",
        children["timed"],
        "--type",
        "timer",
        "--timeout",
        "3s",
        "--reason",
        "demo: a gate the runtime may resolve",
        cwd=main,
    )
    human = bd_json(
        "gate",
        "create",
        "--blocks",
        children["human"],
        "--type",
        "human",
        "--reason",
        "demo: only a person may resolve this",
        cwd=main,
    )
    return {"epic": epic, **children, "human_gate": str((human or {}).get("id") or "")}


# --------------------------------------------------------------------------------------------
# The demo run
# --------------------------------------------------------------------------------------------


def directives_for(root: Path, bead: str, text: str) -> str:
    """Write the instructions file for one seat run and return its path.

    The stub's directive mini-language stands in for a real brief. Scratch, not state: nothing
    downstream reads it back, because the branch — not an instructions file — is the checkpoint.
    """
    path = root / "instructions" / f"{bead}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")
    return str(path)


async def run_demo(root: Path, main: Path, ids: dict) -> None:
    from beadhive import localloop

    plan = {
        ids["happy"]: "STUB_STATUS=done\nSTUB_SUMMARY=implemented and submitted",
        ids["timed"]: "STUB_STATUS=done\nSTUB_SUMMARY=implemented after the timer gate cleared",
        ids["human"]: "STUB_STATUS=done\nSTUB_SUMMARY=implemented after a person approved",
        ids["blocked"]: "STUB_STATUS=blocked\nSTUB_SUMMARY=acceptance criteria contradict",
        ids["hang"]: "STUB_HANG=true",
    }
    attempts: dict[str, int] = {}

    def brief(_action: str, bead: str, _role: str) -> str:
        """Instructions for one seat run — and the demo's stand-in for RECOVERY IS RE-DISPATCH.

        A re-dispatched bead gets a FRESH turn (a new session id, new instructions, the same
        worktree), never `--resume_session`: resuming replays the dead conversation including
        its dead ends and measured 1.30x the cost of a fresh turn (bh-a7so.2 §7). Here the
        second turn on the hung bead simply completes.
        """
        attempts[bead] = attempts.get(bead, 0) + 1
        text = plan[bead]
        if bead == ids["hang"] and attempts[bead] > 1:
            text = "STUB_STATUS=done\nSTUB_SUMMARY=completed on the re-dispatched fresh turn"
        return directives_for(root, bead, text)

    loop = localloop.LocalLoop(
        hive_dir=main,
        epic=ids["epic"],
        actor=DEMO_ACTOR,
        caps=localloop.Caps(max_concurrency=2, max_run_seconds=2.0),
        seat_command=f"{sys.executable} {STUB_SEAT}",
        poll_interval=0.4,
        envelope_grace=3.0,
        terminate_grace=3.0,
        instructions=brief,
    )

    for n in range(1, 20):
        report = await loop.run_pass()
        decision = report.decision
        # Printed in the order the pass actually ran them, so the trace reads as the loop body:
        # coordination -> caps -> harvest -> decide -> dispatch.
        print(
            f"\npass {n}: gate check resolved={report.gate_resolved} "
            f"reclaimed={len(report.reclaimed)} heartbeats={list(report.heartbeats)} "
            f"lease={'held' if report.lease.held else 'LOST'}"
        )
        for bead, rung in report.cancelled:
            print(
                f"  CANCEL → {bead} hit the wall-time cap; stopped at ladder rung {rung!r}, "
                "priced envelope read, process group reaped"
            )
        for bead, outcome in report.harvested:
            print(f"  harvest→ {bead} outcome={outcome}")
            if outcome == "done":
                # Stand-in for what a REAL seat does itself (`bh work submit` → review → merge).
                # The stub is a contract double, not an agent; the demo closes the bead so the
                # molecule keeps moving and the decision table has something to advance on.
                bd(
                    "close",
                    bead,
                    "--reason",
                    "demo: seat reported done",
                    cwd=main,
                    actor=DEMO_ACTOR,
                )
                print(f"           (a real seat would have submitted; demo closed {bead})")
        for bead, cause in report.causes:
            labels = bd_row("show", bead, cwd=main).get("labels") or []
            print(
                f"  CAUSE  → {bead} dispatch={cause} written to beads "
                f"(event bead + label cache); labels now {sorted(labels)}"
            )
            if cause == localloop.CAUSE_BLOCKED:
                # `blocked` is JUDGMENT, not failure — blind-retrying it would spend the same
                # tokens for the same answer. A person triages it; that needs no runtime.
                print(f"  (a human triages {bead}'s dispatch:blocked cause — no runtime involved)")
                plan[bead] = "STUB_STATUS=done\nSTUB_SUMMARY=re-dispatched after a human ruled"
                bd("update", bead, "--status", "open", "--assignee", "", cwd=main, actor=DEMO_ACTOR)
        for denied in report.denied:
            print(f"  denied → {denied.reason}: {denied.detail}")
        for reason in report.declined:
            print(f"  claim  → nothing takeable ({reason})")
        print(f"  decide → row={decision.row!r} action={decision.action!r} — {decision.detail}")
        for bead in report.dispatched:
            seat = loop.in_flight.get(bead)
            print(
                f"  spawn  → {bead} pid={seat.pid if seat else '?'} "
                f"pgid={seat.pgid if seat else '?'} session={seat.session_id if seat else '?'} "
                f"(claimed through `bh work next`)"
            )

        if n == 4 and ids["human_gate"]:
            # THE ACCEPTANCE CRITERION, shown live: `bd gate check` ran at the head of every pass
            # above and left this gate alone every time, so its bead was never dispatched. Only a
            # person clears it, and doing so needs no runtime running at all (Decision 1's first
            # consequence) — this is `bh work approve` in its rawest form.
            print(f"  (human resolves gate {ids['human_gate']} — no runtime was involved)")
            bd(
                "gate",
                "resolve",
                ids["human_gate"],
                "--reason",
                "demo: approved by a person",
                cwd=main,
            )
        if loop.done:
            print("\nmolecule complete.")
            break
        if loop.halted:
            print("\nloop halted (a human owns the next move).")
            break
        if decision.row == "finish":
            bd(
                "close",
                ids["epic"],
                "--reason",
                "demo: molecule landed",
                cwd=main,
                actor=DEMO_ACTOR,
            )
        await asyncio.sleep(loop.poll_interval)

    await loop.shutdown()

    print("\n--- what the beads remember (the ONLY durable record) ---")
    for key in ("happy", "blocked", "hang", "timed", "human"):
        row = bd_row("show", ids[key], cwd=main)
        labels = sorted(row.get("labels") or [])
        print(f"  {ids[key]} {key:<8} status={row.get('status')} labels={labels}")
    print("  (retry counts are DERIVED by counting these event beads — never stored)")
    for key in ("blocked", "hang"):
        # `--all`: event beads are created CLOSED, and `bd list` hides closed issues by default.
        events = bd_json("list", "--parent", ids[key], "--include-infra", "--all", cwd=main) or []
        for ev in events:
            if isinstance(ev, dict) and ev.get("issue_type") == "event":
                print(f"    {ids[key]} event {ev.get('id')}: {ev.get('title')}")


# --------------------------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="", help="scratch root (default: a fresh temp dir)")
    parser.add_argument(
        "--check-isolation-only",
        action="store_true",
        help="set up + verify the sandbox and exit (needs neither bd nor bh)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(tempfile.mkdtemp(prefix="bh-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    print(f"scratch root: {root}")
    print("verifying isolation before anything is written:")
    isolate(root)
    tripwires = verify_isolation(root)
    if args.check_isolation_only:
        return 0

    for tool in ("bd", "bh"):
        if shutil.which(tool) is None:
            print(
                f"✗ `{tool}` is not on PATH — the demo drives real binaries on purpose "
                "(the claim goes through `bh work next`, the atomic pick-claim-provision verb)",
                file=sys.stderr,
            )
            return 2

    probe = subprocess.run(["bh", "work", "next", "--help"], capture_output=True, text=True)
    if probe.returncode != 0:
        print(
            "✗ the `bh` on PATH has no `work next` verb — the demo needs THIS tree's bh.\n"
            f"  found: {shutil.which('bh')}\n"
            "  run the demo through the project venv: `uv run scripts/demo_local_loop.py`",
            file=sys.stderr,
        )
        return 2

    started = time.monotonic()
    main_dir = build_hive(root)
    ids = seed_molecule(main_dir)
    print(f"\nseeded {ids['epic']} with 5 children and 2 gates in {main_dir}")
    print(
        f"  human gate {ids['human_gate']} blocks {ids['human']} — the runtime may not resolve it"
    )

    try:
        asyncio.run(run_demo(root, main_dir, ids))
    finally:
        for tripwire in tripwires:
            tripwire.assert_untouched()
        print(
            f"\n✓ tripwires clean: nothing outside {root} was touched "
            f"({time.monotonic() - started:.1f}s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
