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
  `bd set-state` (an event bead + a `dispatch:run_blocked` label) and then read back and printed;
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
real `~/.beadhive` which is re-checked at the end. This repo's own `.beads/` is guarded the
stronger way when the run is fenced — the fence binds it READ-ONLY, so :class:`WriteBarrier`
proves the demo *cannot* write it rather than diffing a directory the host still shares and
blaming the demo for someone else's write (bh-zq5is). Run

    uv run scripts/demo_local_loop.py --check-isolation-only

to see just that part (it needs no `bd` and no `bh`); `tests/test_localloop.py` runs exactly that
so the guarantee is covered by the suite.

THE EXIT CODE IS THE VERDICT
----------------------------
The run ends by re-reading every bead and asserting the molecule actually reached its terminal
state (epic closed, every child closed, the cancelled bead re-dispatched and its cause recorded);
anything that does not hold is listed and the script exits **non-zero**. A crash and a success
must not be distinguishable only by reading the tail of ninety seconds of output.

Timing is stated rather than assumed: :func:`print_timing_contract` prints, before the first
pass, which parts of the output vary between runs (pass numbers, pids, elapsed seconds) and
which are fixed (the sequence of outcomes). Nothing in the script keys off a pass number — the
timer gate is waited out rather than raced, and the human gate is resolved on an observed
condition.

USAGE
------
    uv run scripts/demo_local_loop.py                # scratch root under the system temp dir
    uv run scripts/demo_local_loop.py --root /tmp/x  # keep the scratch hive for inspection
    uv run scripts/demo_local_loop.py --check-isolation-only
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from beadhive.state import CAUSE_RUN_CANCELLED, DISPATCH_DIM

#: The label a cancelled run carries, spelled from the vocabulary CONSTANT (not a literal) so a
#: future rename of the dispatch vocabulary cannot desync this demo from the loop the way the
#: `run_` prefix rename under bh-e7r9q did (bh-bwcxx).
DISPATCH_CANCELLED_LABEL = f"{DISPATCH_DIM}:{CAUSE_RUN_CANCELLED}"

REPO = Path(__file__).resolve().parents[1]
STUB_SEAT = REPO / "tests" / "fixtures" / "stub_seat.py"

ORG, REPO_NAME, PREFIX = "demo-org", "demo-hive", "dm"

#: How long the self-resolving `timer` gate holds its bead. The demo WAITS this out rather than
#: racing it (see :func:`await_timer_gate`), so it is a duration, never a scheduling assumption.
TIMER_GATE_SECONDS = 3.0

#: Per-run wall-time cap. The hung seat trips it; nothing else comes near it.
MAX_RUN_SECONDS = 2.0

#: Hard bound on passes. Reaching it is a FAILURE (reported, non-zero exit), not a quiet stop:
#: the molecule above settles in well under a dozen passes on any machine.
MAX_PASSES = 60


# --------------------------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------------------------


def _fenced() -> bool:
    """True iff this run is inside `scripts/hermetic.sh`'s bubblewrap fence.

    The fence exports `BH_HERMETIC_FENCE=1` (scripts/hermetic.sh) and, critically for the
    tripwires below, gives the run a **fresh tmpfs `$HOME`** — so `Path.home()/".beadhive"` is a
    directory private to this process tree rather than the operator's real hive root. That one
    bit changes what a tripwire violation MEANS, which is why it is read rather than assumed.
    """
    return os.environ.get("BH_HERMETIC_FENCE") == "1"


#: Ambient writers already MEASURED rewriting paths under a real `~/.beadhive` while this demo
#: ran — recorded so an UNFENCED violation names a suspect instead of pointing at the demo.
#:
#: `hq/hives/*.yaml` is not a guess. Run 3 of the 0.11.2 push gate (bh-ik08j) tripped on ten
#: paths — `cache/metadata.json` plus nine `hq/hives/**.yaml` rewritten SEQUENTIALLY ~0.7s
#: apart — with no `bh` command typed by the operator, and the bead recorded the cause as
#: UNIDENTIFIED with a TTL hypothesis. It was identified by measurement on 2026-08-13: a single
#: `bh doctor` reproduces that signature exactly, because `doctor._bd_schema_skew_warnings`
#: calls `hive_schema.refresh()` UNCONDITIONALLY for every registered hive with a local
#: checkout, and each refresh rewrites that hive's manifest (`observed_at` always moves, so the
#: content differs on every run — a content diff would not have spared it either). Nine of the
#: twenty-one registered hives were rewritten in both the incident and the reproduction: the
#: rest are `bd`-schema-blocked, their probe fails, and `refresh` correctly writes nothing.
#: The TTL hypothesis is therefore DISPROVEN — nothing about the refresh is time-gated — and
#: "no human typed a bh command" does not mean no `bh` ran: `bh mcp serve` exposes
#: `doctor.doctor_payload()` as the `beadhive://doctor` resource, and seven long-lived
#: `bh mcp serve` processes were live on the box during the incident.
_AMBIENT_WRITERS: tuple[tuple[str, str], ...] = (
    (
        "hq/hives/",
        "`bh doctor`'s per-hive schema refresh (hive_schema.refresh — one YAML rewrite per "
        "registered hive, ~0.7s apart), reachable with no human typing anything via the "
        "`beadhive://doctor` MCP resource",
    ),
    (
        "cache/metadata.json",
        "a fleet metadata refresh (metadata.read_fleet with ttl=0) — `bh doctor` and "
        "`bh worktree status` both force one; it is a repo-scan CACHE, not hive state",
    ),
    (
        "hq/hosts/",
        "a host-lease heartbeat (`bh host adopt`, and every write-shaped `bh work` verb)",
    ),
    ("hq/", "any `bh` verb that writes Factory HQ — `bh bd create`, `bh escalate`, `bh work …`"),
)


def _candidate_writers() -> list[str]:
    """`pid  elapsed  cmd` for every live `bh`/`bd` process on this box, newest first.

    CANDIDATES, NOT PROOF, and the caller says so. The point is to stop the operator having to
    guess: 'ISOLATION VIOLATION' sent a whole session chasing the demo when the cause was
    elsewhere, twice (bh-ik08j). Best-effort — a missing/odd `ps` returns nothing rather than
    turning a diagnostic into a second failure.

    `ps_argv` for the `-ww` (bh-jwwls): matching here keys on argv[0], which survives an
    80-column cut, so this was never mis-MATCHING — but the suspect it prints is the whole
    point, and a truncated command line names a suspect the operator cannot act on.
    """
    from beadhive.run import ps_argv

    try:
        res = subprocess.run(
            ps_argv("pid=,etimes=,args="), capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[tuple[int, str]] = []
    for line in (res.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, etimes, cmd = parts
        # Match the BINARY, not the whole command line: every `ps` row of this very demo
        # mentions "bh" somewhere, and a probe that always names itself names nothing.
        argv0 = Path(cmd.split()[0]).name if cmd.split() else ""
        if argv0 not in ("bh", "bd", "bh-mcp"):
            continue
        try:
            age = int(etimes)
        except ValueError:
            continue
        rows.append((age, f"pid {pid:>8}  {age:>6}s  {cmd[:100]}"))
    return [row for _, row in sorted(rows)]


class Tripwire:
    """A before/after fingerprint of a directory that MUST NOT change during the demo.

    Cheap and blunt on purpose: relative path + size + mtime for every file. It cannot be argued
    with, and it fails loudly at the end rather than leaving "we're pretty sure it was isolated"
    as the evidence.

    WHAT A VIOLATION MEANS DEPENDS ON THE FENCE, and :meth:`assert_untouched` says which case it
    is rather than printing paths and leaving the operator to guess:

    * FENCED (`just demo-local-loop`, and every `check-all` phase since bh-yndxi) — `$HOME` is a
      fresh tmpfs, so the watched path is private to this process tree and no other process on
      the box can reach it. A violation is therefore THIS DEMO or a child of it: a real escape,
      which is exactly what the tripwire is for.
    * UNFENCED (`BH_HERMETIC=0`, a bare `uv run scripts/demo_local_loop.py`, macOS with no
      `bwrap`) — the watched path is the operator's REAL hive root, shared by every `bh` process
      on the box, so the writer may be nothing to do with the demo. That ambiguity is the whole
      of bh-ik08j: two of three 0.11.2 push-gate runs failed here, and the error named neither
      the concurrent process nor the fact that something else caused it.
    """

    #: Subtrees skipped when fingerprinting. `wt/` (and the retired `worktrees/`) under a real
    #: `~/.beadhive` holds every hive's checked-out trees — 1.2M files on the machine this was
    #: written on — and the demo cannot reach it anyway, because `$BH_WORKTREES` is re-pointed
    #: into the scratch root and that is asserted separately. Walking it would make the guard so
    #: slow nobody runs it, which is a worse outcome than a narrower guard that always runs.
    #: Both names are listed: the managed-worktree root moved to `wt/` with the unified
    #: `wt/bead/<type>/<id>` namespace, and a skip list naming only the old one silently walked
    #: 400k+ files and hung `--check-isolation-only` past its caller's timeout (bh-ysnds).
    SKIP = frozenset({"wt", "worktrees", ".git"})

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

    #: Changed paths listed in full before the tail is elided. Ten was the old cap and the
    #: incident that named this bead listed exactly ten — enough to see the SHAPE of a wave.
    MAX_LISTED = 10

    def _changes(self, after: dict) -> list[str]:
        """One `<sign> <path>  (<what changed>)` line per differing entry, added/removed first.

        Says WHAT changed, not just which path: an idempotent rewrite that moved only the mtime
        reads very differently from a file that grew, and the old message could not tell them
        apart because it printed a bare list of names.
        """
        lines = []
        for key in sorted(set(self.before) - set(after)):
            lines.append(f"    - {key}  (removed)")
        for key in sorted(set(after) - set(self.before)):
            lines.append(f"    + {key}  (created, {after[key][0]}B)")
        for key in sorted(set(after) & set(self.before)):
            was, now = self.before[key], after[key]
            if was == now:
                continue
            if was[0] != now[0]:
                lines.append(f"    ~ {key}  ({was[0]}B -> {now[0]}B, rewritten)")
            else:
                lines.append(f"    ~ {key}  (same {now[0]}B, mtime moved — idempotent rewrite)")
        return lines

    def _cause(self, changed: list[str]) -> list[str]:
        """The CAUSE block — who wrote, or (unfenced) who the candidates are.

        bh-ik08j's fifth acceptance criterion: the error must name what caused the violation,
        not only which paths changed.
        """
        if _fenced():
            return [
                "  CAUSE: this run is FENCED (BH_HERMETIC_FENCE=1), so that path is a tmpfs "
                "PRIVATE to",
                "  this process tree — no other process on this box can reach it. The writer is "
                "therefore",
                "  THIS DEMO or a child of it. That is a REAL ESCAPE (an absolute path baked "
                "into code,",
                "  a subprocess that resolves $HOME before the redirect), which is precisely "
                "what this",
                "  tripwire exists to catch. Do not weaken the assertion — find the writer.",
            ]

        out = [
            "  CAUSE: this run is UNFENCED (BH_HERMETIC_FENCE is unset — BH_HERMETIC=0, a bare "
            "`uv run`,",
            "  or no `bwrap` on this platform), so the watched path is the OPERATOR'S REAL hive "
            "root,",
            "  shared by every bh process on this box. The writer may have nothing to do with "
            "the demo:",
            "  this is bh-ik08j, which failed two of three 0.11.2 push-gate runs on ambient "
            "writes.",
            "  Re-run through the fence — `just demo-local-loop` — and the ambiguity does not "
            "exist.",
        ]
        blob = "\n".join(changed)
        suspects = [f"    {path} <- {who}" for path, who in _AMBIENT_WRITERS if path in blob]
        if suspects:
            out.append("  KNOWN ambient writers for the paths above (measured, not guessed):")
            out.extend(suspects)
        live = _candidate_writers()
        if live:
            out.append("  bh/bd processes alive right now (CANDIDATES, not proof):")
            out.extend(f"    {row}" for row in live[: self.MAX_LISTED])
        return out

    def assert_untouched(self) -> None:
        after = self._snapshot()
        if after == self.before:
            return
        changed = self._changes(after)
        shown = changed[: self.MAX_LISTED]
        if len(changed) > self.MAX_LISTED:
            shown.append(f"    … and {len(changed) - self.MAX_LISTED} more")
        raise SystemExit(
            "\n".join(
                [
                    f"ISOLATION VIOLATION: {self.label} ({self.path}) changed during the demo "
                    f"— {len(changed)} path(s):",
                    *shown,
                    *self._cause(changed),
                ]
            )
        )


class WriteBarrier:
    """Proof that the demo *cannot* write a path, for the one watched path the fence SHARES.

    WHY THIS EXISTS AND WHAT IT REPLACES (bh-zq5is). :class:`Tripwire`'s fenced verdict — "this
    path is a tmpfs private to this process tree, so the writer is THIS DEMO" — is true of
    `~/.beadhive` (the fence gives the run a fresh tmpfs `$HOME`) and FALSE of this repo's
    `.beads/`. `scripts/hermetic.sh` does not hide that one; it `--ro-bind`s the REAL directory
    back in, so the fenced run looks at the same inodes as the host and sees every write any
    other `bh`/`bd` process on the box makes to it. A before/after diff of a directory the host
    still shares cannot say who wrote it, and it blamed the demo for writes the demo could not
    physically have made: `just check-all` (and with it the pre-push gate) failed on
    `~ last-touched (same 8B, mtime moved)`, written by an ambient `bh doctor` / `bh hq bd list`
    in ANOTHER repo's session — the same measured writer as bh-ik08j, arriving through a
    different door. MEASURED, not reasoned: polling both `.beads` trees at 20ms through a fenced
    demo caught the mtime moving three times while every `bd` the demo ran carried
    `-C <scratch>`, and `touch .beads/PROBE` inside the fence answers `Read-only file system`.

    So the guarantee is asserted where it actually lives — in the mount. A read-only bind is a
    kernel-enforced "no writes from this process tree", which is STRICTER than the diff it
    replaces (the diff could only observe an escape after the fact; this one makes it
    impossible) and it does not race the host. If the barrier is ever gone — someone drops the
    `--ro-bind` from the fence — this fails loudly rather than falling back to the diff, because
    a demo that CAN write the operator's bead store is exactly the escape being guarded against.

    Unfenced there is no barrier to assert, so the ordinary :class:`Tripwire` diff still runs on
    this path; that is the bh-ik08j world, where the message already says the writer may be any
    `bh` process on the box.
    """

    PROBE = ".bh-demo-isolation-probe"

    def __init__(self, label: str, path: Path):
        self.label = label
        self.path = path
        self.assert_untouched()

    def assert_untouched(self) -> None:
        probe = self.path / self.PROBE
        try:
            probe.touch()
        except OSError as exc:
            if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
                return
            raise
        probe.unlink(missing_ok=True)
        raise SystemExit(
            "\n".join(
                [
                    f"ISOLATION FAILURE: {self.label} ({self.path}) is WRITABLE inside the fence.",
                    "  The fence is supposed to --ro-bind it (scripts/hermetic.sh), so this demo "
                    "— and every",
                    "  `bd` it spawns — physically cannot write the operator's bead store. That "
                    "barrier is gone,",
                    "  which is the escape the tripwire exists to prevent. Fix the fence; do not "
                    "relax this.",
                ]
            )
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


def verify_isolation(root: Path) -> list[Tripwire | WriteBarrier]:
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

    # The repo's own `.beads/` is the one watched path the fence does NOT hide — it is bound back
    # in READ-ONLY, so a diff of it races every other bh/bd process on the box. Assert the barrier
    # instead of diffing a shared directory; see :class:`WriteBarrier`.
    if _fenced() and repo_beads.exists():
        beads_wire: Tripwire | WriteBarrier = WriteBarrier("repo .beads/", repo_beads)
        print("  ✓ repo .beads/ is read-only inside the fence (write barrier proven)")
    else:
        beads_wire = Tripwire("repo .beads/", repo_beads)

    print("  ✓ isolation verified")
    return [Tripwire("~/.beadhive", real_home), beads_wire]


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


def seed_molecule(main: Path) -> tuple[dict, float]:
    """One epic, five children, and two gates — the smallest shape that shows every branch.

    Returns the ids plus the monotonic deadline the `timer` gate self-resolves at, so the run
    can wait the gate out instead of racing it.
    """
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
        f"{TIMER_GATE_SECONDS:g}s",
        "--reason",
        "demo: a gate the runtime may resolve",
        cwd=main,
    )
    timer_deadline = time.monotonic() + TIMER_GATE_SECONDS
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
    return {"epic": epic, **children, "human_gate": str((human or {}).get("id") or "")}, (
        timer_deadline
    )


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


def is_closed(main: Path, bead: str) -> bool:
    return str(bd_row("show", bead, cwd=main).get("status") or "") == "closed"


async def await_timer_gate(main: Path, ids: dict, loop, deadline: float) -> None:
    """Wait the `timer` gate out when it is the ONLY thing that could still move the molecule.

    Without this, the run has a genuine race: if every other child settles before the gate's
    timeout elapses, the next pass sees open work, nothing ready and nothing in flight — decision
    row 12, `deadlock-escalate` — and the loop halts, correctly, on a molecule that was one second
    away from being fine. The loop is right; racing it is what would be wrong. So the demo blocks
    until the gate is resolvable and then lets `bd gate check` resolve it, which is what a real
    driver's poll interval does for free over a gate measured in minutes rather than seconds.

    The clock test comes first and is free, so after the timeout has elapsed this costs nothing.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0 or loop.in_flight:
        return
    if not all(is_closed(main, ids[key]) for key in ("happy", "blocked", "hang", "human")):
        return
    print(
        f"  (waiting {remaining:.1f}s for the timer gate on {ids['timed']} to become resolvable — "
        "the demo waits it out rather than racing `bd gate check`)"
    )
    await asyncio.sleep(remaining)


async def run_demo(root: Path, main: Path, ids: dict, timer_deadline: float) -> list[str]:
    """Drive the molecule to its terminal state. Returns a list of assertion failures (empty
    means the run is good) — the caller turns a non-empty list into a non-zero exit."""
    from beadhive import localloop

    plan = {
        ids["happy"]: "STUB_STATUS=done\nSTUB_SUMMARY=implemented and submitted",
        ids["timed"]: "STUB_STATUS=done\nSTUB_SUMMARY=implemented after the timer gate cleared",
        ids["human"]: "STUB_STATUS=done\nSTUB_SUMMARY=implemented after a person approved",
        ids["blocked"]: "STUB_STATUS=blocked\nSTUB_SUMMARY=acceptance criteria contradict",
        ids["hang"]: "STUB_HANG=true",
    }
    attempts: dict[str, int] = {}
    violations: list[str] = []
    human_gate_open = bool(ids["human_gate"])
    epic_closed = False

    def brief(action: str, bead: str, role: str) -> str:
        """Instructions for one seat run — and the demo's stand-in for RECOVERY IS RE-DISPATCH.

        A re-dispatched bead gets a FRESH turn (a new session id, new instructions, the same
        worktree), never `--resume_session`: resuming replays the dead conversation including
        its dead ends and measured 1.30x the cost of a fresh turn (bh-a7so.2 §7). Here the
        second turn on the hung bead simply completes.

        The EPIC gets its own branch, not a plan entry. Under the recursive dispatch rule the
        container actions (`start` / `finish`) act on the epic itself and resolve to a DISPATCHER
        seat, so `plan` — which is keyed by child — has nothing to say about it. An unbriefed
        seat must never be spawnable: no `plan.get(bead, "")` default here, because an empty
        brief hands a seat no instructions and then *looks* like it worked.
        """
        attempts[bead] = attempts.get(bead, 0) + 1
        if bead == ids["epic"]:
            if role != "dispatcher":
                raise SystemExit(
                    f"BUG: the epic {bead} was routed to a {role!r} seat for action {action!r}; "
                    "container actions are the dispatcher's"
                )
            text = (
                f"STUB_STATUS=done\n"
                f"STUB_SUMMARY=container {action}: molecule {bead} assembled and landed"
            )
            return directives_for(root, bead, text)
        if bead not in plan:
            raise SystemExit(
                f"BUG: the demo has no brief for {bead} (action={action!r} role={role!r}). "
                "Every spawnable bead needs an explicit brief — refusing to spawn a seat with "
                "empty instructions."
            )
        text = plan[bead]
        if bead == ids["hang"] and attempts[bead] > 1:
            text = "STUB_STATUS=done\nSTUB_SUMMARY=completed on the re-dispatched fresh turn"
        return directives_for(root, bead, text)

    loop = localloop.LocalLoop(
        hive_dir=main,
        epic=ids["epic"],
        actor=DEMO_ACTOR,
        caps=localloop.Caps(max_concurrency=2, max_run_seconds=MAX_RUN_SECONDS),
        seat_command=f"{sys.executable} {STUB_SEAT}",
        poll_interval=0.4,
        envelope_grace=3.0,
        terminate_grace=3.0,
        instructions=brief,
    )

    n = 0
    while n < MAX_PASSES:
        n += 1
        await await_timer_gate(main, ids, loop, timer_deadline)
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
            if outcome != "done":
                continue
            if bead == ids["epic"]:
                # The dispatcher seat's `bh work finish` is what closes the epic in production;
                # the stub can't, so the demo does. Guarded because the loop keeps re-issuing
                # `finish` until the epic is ACTUALLY closed (see the spawn note below) — closing
                # a closed bead is an error, and swallowing that error would hide a real one.
                if not epic_closed:
                    bd(
                        "close",
                        bead,
                        "--reason",
                        "demo: molecule landed",
                        cwd=main,
                        actor=DEMO_ACTOR,
                    )
                    epic_closed = True
                    print(
                        f"           (a real dispatcher would `bh work finish`; demo closed {bead})"
                    )
                continue
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
                print(
                    f"  (a human triages {bead}'s {DISPATCH_DIM}:{cause} cause — "
                    "no runtime involved)"
                )
                plan[bead] = "STUB_STATUS=done\nSTUB_SUMMARY=re-dispatched after a human ruled"
                bd("update", bead, "--status", "open", "--assignee", "", cwd=main, actor=DEMO_ACTOR)
        for denied in report.denied:
            print(f"  denied → {denied.reason}: {denied.detail}")
        for reason in report.declined:
            print(f"  claim  → nothing takeable ({reason})")
        print(f"  decide → row={decision.row!r} action={decision.action!r} — {decision.detail}")
        for bead in report.dispatched:
            seat = loop.in_flight.get(bead)
            how = (
                "container action — the epic resolves to a DISPATCHER seat"
                if bead == ids["epic"]
                else "claimed through `bh work next`"
            )
            print(
                f"  spawn  → {bead} pid={seat.pid if seat else '?'} "
                f"pgid={seat.pgid if seat else '?'} session={seat.session_id if seat else '?'} "
                f"({how})"
            )
            if bead == ids["human"] and human_gate_open:
                violations.append(
                    f"{bead} was dispatched on pass {n} while human gate "
                    f"{ids['human_gate']} was still open"
                )
        if ids["epic"] in report.dispatched and attempts.get(ids["epic"], 0) > 1:
            print(
                "           (second `finish` turn: a pass harvests, decides and spawns before it "
                "returns, so the demo's stand-in close lands after this spawn. The loop re-issues "
                "`finish` until the epic is actually closed — correct: the SEAT, not the loop, "
                "owns that close)"
            )

        if human_gate_open and report.dispatched:
            # THE ACCEPTANCE CRITERION, resolved on an OBSERVED condition rather than a pass
            # number: this pass dispatched work and did NOT dispatch the human-gated bead, so
            # `bd gate check` — which ran at the head of every pass — has demonstrably left the
            # gate alone while moving everything else. Only a person clears it, and doing so
            # needs no runtime running at all (Decision 1's first consequence); this is
            # `bh work approve` in its rawest form.
            held = bd_row("show", ids["human"], cwd=main).get("status")
            print(
                f"  (evidence: the loop dispatched {list(report.dispatched)} and left "
                f"{ids['human']} at status={held!r} — gate {ids['human_gate']} is still open)"
            )
            print(f"  (human resolves gate {ids['human_gate']} — no runtime was involved)")
            bd(
                "gate",
                "resolve",
                ids["human_gate"],
                "--reason",
                "demo: approved by a person",
                cwd=main,
            )
            human_gate_open = False
        if loop.done:
            print("\nmolecule complete.")
            break
        if loop.halted:
            print("\nloop halted (a human owns the next move).")
            break
        await asyncio.sleep(loop.poll_interval)
    else:
        violations.append(f"the molecule did not settle within {MAX_PASSES} passes")

    await loop.shutdown()

    print("\n--- what the beads remember (the ONLY durable record) ---")
    for key in ("happy", "blocked", "hang", "timed", "human"):
        row = bd_row("show", ids[key], cwd=main)
        labels = sorted(row.get("labels") or [])
        print(f"  {ids[key]} {key:<8} status={row.get('status')} labels={labels}")
    print("  (retry counts are DERIVED by counting these event beads — never stored)")
    events_by_bead: dict[str, list[str]] = {}
    for key in ("blocked", "hang"):
        # `--all`: event beads are created CLOSED, and `bd list` hides closed issues by default.
        events = bd_json("list", "--parent", ids[key], "--include-infra", "--all", cwd=main) or []
        for ev in events:
            if isinstance(ev, dict) and ev.get("issue_type") == "event":
                events_by_bead.setdefault(ids[key], []).append(str(ev.get("id")))
                print(f"    {ids[key]} event {ev.get('id')}: {ev.get('title')}")

    return violations + check_terminal_state(main, ids, loop, attempts, events_by_bead)


def check_terminal_state(main: Path, ids: dict, loop, attempts: dict, events: dict) -> list[str]:
    """Assert the molecule actually LANDED, and say precisely what didn't if it didn't.

    Without this the demo's exit code says only "the script reached the end", which a crash and a
    success are distinguishable by only if someone reads the tail of 90 seconds of output. Every
    check below is a claim the bead's acceptance criteria make, re-read from beads at the end:
    a future regression that quietly stops dispatching, stops cancelling, or stops recording
    causes fails here instead of scrolling past.
    """
    failures: list[str] = []

    def want(ok: bool, message: str) -> None:
        print(f"  {'✓' if ok else '✗'} {message}")
        if not ok:
            failures.append(message)

    print("\n--- completion assertions (the demo fails loudly, not quietly) ---")
    want(loop.done, "the loop reached its terminal `done` decision")
    want(not loop.halted, "the loop never halted for a human")
    want(is_closed(main, ids["epic"]), f"the epic {ids['epic']} is closed")
    for key in ("happy", "blocked", "hang", "timed", "human"):
        want(is_closed(main, ids[key]), f"child {ids[key]} ({key}) is closed")

    hang_labels = set(bd_row("show", ids["hang"], cwd=main).get("labels") or [])
    hang_turns = attempts.get(ids["hang"], 0)
    want(
        DISPATCH_CANCELLED_LABEL in hang_labels,
        f"{ids['hang']} carries {DISPATCH_CANCELLED_LABEL}",
    )
    want(
        hang_turns >= 2,
        f"{ids['hang']} was RE-DISPATCHED as a fresh turn (turns={hang_turns})",
    )
    want(bool(events.get(ids["hang"])), f"{ids['hang']} has a cause event bead")
    want(bool(events.get(ids["blocked"])), f"{ids['blocked']} has a cause event bead")
    want(
        attempts.get(ids["blocked"], 0) >= 2,
        f"{ids['blocked']} ran again only after a human ruled "
        f"(attempts={attempts.get(ids['blocked'], 0)})",
    )
    want(
        attempts.get(ids["epic"], 0) >= 1,
        f"a dispatcher seat ran the container action on the epic {ids['epic']}",
    )
    return failures


def print_timing_contract() -> None:
    """State plainly what varies run to run, and what may not. A demo whose whole job is to be
    reviewable by someone who wasn't here has to say which parts of its own output are stable."""
    print(
        "\nwhat varies between runs, and what does not:\n"
        "  VARIES  the wall-clock seconds, and therefore WHICH PASS NUMBER each event lands on.\n"
        "          A pass is dominated by real `bd` (Dolt) calls, so pass duration is a property\n"
        "          of the machine. Pids, pgids and session ids are fresh every run too.\n"
        f"  FIXED   the SEQUENCE of outcomes: the {TIMER_GATE_SECONDS:g}s timer gate is waited\n"
        "          out rather than raced, the human gate is resolved on an OBSERVED condition\n"
        "          (a pass that dispatched other work and left the gated bead alone) and never\n"
        "          on a pass number, and the run ends by re-reading every bead and asserting\n"
        "          the molecule landed. Nothing in this script keys off timing.\n"
        f"  BOUNDED at most {MAX_PASSES} passes; the per-run wall-time cap is "
        f"{MAX_RUN_SECONDS:g}s (only the hung seat reaches it).\n"
        "  Exit code is the verdict: 0 only if the molecule reached its terminal state."
    )


# --------------------------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="", help="scratch root (default: a fresh temp dir)")
    parser.add_argument(
        "--check-isolation-only",
        action="store_true",
        help="set up + verify the sandbox and exit (needs neither bd nor bh)",
    )
    parser.add_argument(
        "--events",
        default="",
        help=(
            "path to additionally tee the structured event stream to, as flushed JSONL "
            "(an ADDITION to stderr, not a replacement) — so `tail -f PATH | jq` follows the "
            "loop's own telemetry (seat_spawned, seat_harvested, seat_cancelled, "
            "dispatch_cause_recorded, dispatch_pass, ...) live, from another terminal, while "
            "this process runs (bh-29r28)"
        ),
    )
    args = parser.parse_args(argv)

    if args.events:
        from beadhive import log

        events_path = Path(args.events).resolve()
        events_path.parent.mkdir(parents=True, exist_ok=True)
        log.add_file_sink(str(events_path))
        print(f"events: {events_path}")
        print(f"  tail -f {events_path} | jq -c '{{event,bead,pass}}'")

    root = Path(args.root).resolve() if args.root else Path(tempfile.mkdtemp(prefix="bh-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    print(f"scratch root: {root}")
    print("verifying isolation before anything is written:")
    isolate(root)
    tripwires = verify_isolation(root)
    if args.check_isolation_only:
        # Re-check the wires before returning, rather than only arming them. Setting the sandbox
        # up is itself work that could write outside the scratch root, and this is the only mode
        # cheap enough to run as a fast probe of the GUARD — `tests/test_localloop.py` drives it
        # to prove the tripwire still catches a real escape, which stops the fence (bh-yndxi)
        # from silently turning a working assertion into a permanently-green one (bh-ik08j).
        for tripwire in tripwires:
            tripwire.assert_untouched()
        print("  ✓ tripwires clean")
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
    ids, timer_deadline = seed_molecule(main_dir)
    print(f"\nseeded {ids['epic']} with 5 children and 2 gates in {main_dir}")
    print(
        f"  human gate {ids['human_gate']} blocks {ids['human']} — the runtime may not resolve it"
    )
    print_timing_contract()

    failures: list[str] = []
    try:
        failures = asyncio.run(run_demo(root, main_dir, ids, timer_deadline))
    finally:
        for tripwire in tripwires:
            tripwire.assert_untouched()
        print(
            f"\n✓ tripwires clean: nothing outside {root} was touched "
            f"({time.monotonic() - started:.1f}s)"
        )
    if failures:
        print(f"\n✗ DEMO FAILED — {len(failures)} check(s) did not hold:", file=sys.stderr)
        for failure in failures:
            print(f"    - {failure}", file=sys.stderr)
        return 1
    print("✓ demo complete: the molecule reached its terminal state and every check held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
