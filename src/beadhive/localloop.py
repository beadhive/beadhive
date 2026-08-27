"""The `local` work-runtime tier (bh-c6dk.5) — a poll loop that supervises seat PROCESSES.

`work.runtime: local` is the harness-agnostic default: no server, no broker, works offline.
The whole tier is one pass, repeated:

    gate check -> reclaim -> renew the host lease -> heartbeat the workers ->
    enforce the caps -> harvest finished seats -> decide -> dispatch

and everything it needs to run that pass is re-derived from `bd` each time. That is what makes
a RESTART a no-op by construction (loop-ownership-and-execution-memory-adr.md Decision 1): the
in-flight map dies with the process, and the beads it referred to come back through
`bd reclaim` on the next pass.

WHAT THIS MODULE OWNS, AND WHAT IT DELIBERATELY DOES NOT
--------------------------------------------------------
It owns **process scheduling only**. Every lifecycle fact — is this bead claimed, is that gate
open, who holds the merge slot — is read from and written to beads through
:mod:`beadhive.coordination` and `bd`. Nothing here is authoritative about any of it
(work-runtime-tiers-adr.md Decision 1), and **nothing here is persisted outside beads**: not
the in-flight map, not a pass ledger, not a retry counter, not a token total. The caps are
in-process and reset on restart *by design*. An implementation that finds it "needs" to persist
something is an ADR amendment, not a local decision.

The decision of *what* to do next is not made here either — :func:`beadhive.work_next.decide`'s
12-row first-match table makes it, and this module executes the closed action vocabulary that
comes back. That split is what keeps R4 ("the dispatcher should not have too much judgement")
true rather than aspirational.

THE HARD REQUIREMENT: PROCESS-GROUP TERMINATION
------------------------------------------------
`asyncio.TaskGroup` supervises the TASK tree, not the PROCESS tree. `proc.terminate()` signals
the direct child only; bh-a7so.2 §3 measured what happens next and it is the single most
important result in the whole spike molecule — the `claude` grandchild is not killed, it
reparents to init, **runs the entire task to completion** (still committing to the worktree
~2.5 minutes after its supervisor was terminated), spends about a full run of tokens, and
writes its final envelope into a pipe nobody is holding. A live, spending, worktree-mutating
agent, orphaned, while the scheduler believes it cancelled. It is signal-independent: the same
PPID-1 reparenting shape reproduced under a group SIGINT (bh-a7so.7 §2).

So every seat run here:

1. spawns with ``start_new_session=True`` — its own process group, so there is a pgid to kill;
2. is reaped via :func:`os.killpg` on that pgid, never ``proc.terminate()`` alone;
3. escalates group SIGTERM -> group SIGKILL across a bounded grace window, **polling until the
   group is actually gone** rather than assuming a signal worked;
4. is NEVER sent SIGINT. A SIGINT-cancelled run exits 0 (bh-a7so.7 §4), which collides head-on
   with the contract's ``0 = done``. :data:`FORBIDDEN_SIGNALS` makes that a raised error rather
   than a code-review convention.

ORDER MATTERS — HOLD THE PIPE, READ THE ENVELOPE, THEN REAP
------------------------------------------------------------
"A killed run emits zero bytes" was an artifact of killing the process holding the READ end of
the pipe at the same instant as the writer (bh-a7so.7 §4 correcting bh-a7so.2). Signal the child
while the reader is still alive and the same CLI emits a priced envelope ~0.63s later carrying
`session_id`, `total_cost_usd`, full `usage` and a machine-readable `terminal_reason`. So the
sequence in :func:`cancel` is **signal the child -> read the envelope -> then killpg as the
reaper**. This module holds the read end from spawn (:func:`spawn_seat` starts a drain task
immediately) precisely so that window always exists.

THE CANCEL LADDER LIVES HERE, NOT IN THE HARNESS
--------------------------------------------------
`baml.sys.exec`'s `ProcessOptions.stdin` is a static string fixed before launch and `exec`
returns a `ShellOutput` for an already-finished process, so `run_resolved_seat` structurally
cannot hold a bidirectional stream-json channel (bh-a7so.7 §13). Rungs 1 and 2 therefore belong
to whatever process spawns the seat and keeps its pipes — in this tier, this module. The
in-flight map (`bead_id -> SeatProcess`, carrying proc/stdin/pgid/session_id) is what makes them
possible at all, and is also why SIBLING NOTIFICATION is the loop's job: the child has no
topology and no outbound channel to a sibling.

WHAT IS NOT HERE
-----------------
* **Token-budget enforcement.** Explicitly out of v1, deferred to bh-3yoh; R3 is knowingly
  half-met (operator decision 2026-08-10). :func:`admit` is the seam a budget governor plugs
  into — see its docstring.
* **The operator surface** (`bh host dispatch`), the supervision backend seam, and the doctor
  section: the follow-on delivery molecule bh-e7r9q, which consumes this seam.
* **Up-chain escalation routing to HQ**: bh-c6dk.10. This loop escalates by writing a closed
  state-dimension value onto the bead (:func:`record_cause`), which is git-synced and visible
  from any host; routing that to a human's inbox is the other bead's job.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import shlex
import signal
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import bd as bd_mod
from . import (
    config,
    coordination,
    log,
    model_routing,
    otel,
    run_journal,
    schedule,
    seatrun,
    state,
    work_next,
)

_LOG = log.get_logger(__name__)

# --------------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------------

#: Signals this tier will never send, and the reason. SIGINT is measured (bh-a7so.7 §4): a
#: SIGINT-cancelled run exits **0**, which collides head-on with the role-binary contract's
#: ``0 = done``. SIGTERM is identical on envelope content, latency, transcript marker and
#: shutdown time, and exits 143 — landing correctly in "anything else = did not complete".
#: Enforced in :func:`send_signal` rather than left as a comment, because a comment cannot fail
#: a test and this is a correctness requirement, not a preference.
FORBIDDEN_SIGNALS = frozenset({signal.SIGINT})


class ForbiddenSignal(RuntimeError):
    """Something tried to send a signal :data:`FORBIDDEN_SIGNALS` rules out (i.e. SIGINT)."""


def send_signal(target: int, sig: signal.Signals, *, group: bool) -> bool:
    """Send *sig* to a pid (``group=False``) or a process group (``group=True``).

    The ONE place this module signals anything, so the never-SIGINT rule has exactly one
    enforcement point. Returns ``False`` when the target was already gone (``ProcessLookupError``
    — the ordinary race between deciding to signal and the process exiting on its own), ``True``
    when the signal was delivered.
    """
    if sig in FORBIDDEN_SIGNALS:
        raise ForbiddenSignal(
            f"refusing to send {sig.name}: {seatrun.NEVER_SIGINT} "
            "(work-runtime-tiers-adr.md Amendment 2 §5)"
        )
    try:
        (os.killpg if group else os.kill)(target, sig)
    except ProcessLookupError:
        return False
    return True


def group_alive(pgid: int) -> bool:
    """Is any process still in process group *pgid*?

    ``killpg(pgid, 0)`` is a delivery probe, not a delivery: it raises ``ProcessLookupError``
    only when the group is empty. This is how the reaper knows whether a SIGTERM actually
    worked instead of assuming it did — and it is how a test detects the orphaned grandchild
    bh-a7so.2 §3 measured, since a reparented grandchild keeps the pgid it inherited.

    Only meaningful once the direct child has been *reaped*: an unreaped zombie still counts as
    a group member. :func:`reap_group` always awaits the child first for that reason.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else — alive as far as we can tell
    return True


# --------------------------------------------------------------------------------------------
# One supervised seat process
# --------------------------------------------------------------------------------------------


@dataclass
class SeatProcess:
    """One in-flight seat run: the value the `{bead_id -> (proc, stdin, pgid, session_id)}` map
    holds, which Amendment 2 §4 names as the reason `--session_id` is required on create.

    Deliberately volatile. It dies with the loop process and SHOULD: the bead it refers to is
    recovered by `bd reclaim` on the next pass of whatever loop comes next.
    """

    bead_id: str
    role: str
    action: str
    # Process-owner identity. Dispatch lifecycle logs join on this value only.
    session_id: str
    # Provider-native continuation passed to the packed seat's ``--session_id``.
    provider_continuation: str
    proc: asyncio.subprocess.Process
    pgid: int
    argv: tuple[str, ...]
    started_at: float
    #: Canonical late-bound model decision. Safe control-plane facts only; launch aliases stay in
    #: argv and secrets never enter this envelope.
    routing: dict | None = None
    #: Rich activity for this immutable outer attempt.  ``None`` preserves the pre-journal
    #: behavior for launch paths that have not yet supplied a validated launch identity.
    journal: run_journal.RunJournal | None = None
    #: Every signal sent to this run, in order, as names ("SIGTERM"). A test asserts SIGINT is
    #: never in here; an operator reads it to see whether the ladder had to escalate.
    signals: list[str] = field(default_factory=list)
    #: Incremental pump tasks (one per output stream) started at spawn. They append CHUNKS as
    #: they arrive rather than buffering to EOF, which matters twice over: a signalled child's
    #: envelope is readable the moment it is written, and a leaked pipe held open by a surviving
    #: grandchild cannot hide the bytes that did arrive.
    _pumps: tuple[asyncio.Task, ...] = ()
    _out: list = field(default_factory=list)
    _err: list = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def finished(self) -> bool:
        """Has the DIRECT CHILD exited?

        `proc.returncode`, deliberately, not `await proc.wait()`. asyncio's subprocess transport
        only considers a process "finished" once every pipe has also closed, so `wait()` blocks
        indefinitely on exactly the case this tier is built for — a grandchild that outlived its
        parent and still holds the inherited stdout. `returncode` is set by the child watcher the
        moment the child is reaped, independent of the pipes, so it is the honest question.
        """
        return self.proc.returncode is not None

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.monotonic()) - self.started_at

    @property
    def stdout(self) -> str:
        return b"".join(self._out).decode("utf-8", "replace")

    @property
    def stderr(self) -> str:
        return b"".join(self._err).decode("utf-8", "replace")

    async def wait_exit(self, timeout: float, poll: float = 0.02) -> bool:
        """Poll until the direct child exits, or *timeout* elapses. Returns whether it exited.
        See :attr:`finished` for why this polls a return code instead of awaiting `wait()`."""
        deadline = time.monotonic() + max(timeout, 0.0)
        while not self.finished and time.monotonic() < deadline:
            await asyncio.sleep(poll)
        return self.finished

    async def collect(self, timeout: float | None = None) -> str:
        """Everything the child has written to stdout so far, optionally waiting up to *timeout*
        for the pumps to reach EOF first.

        Holding the read end from spawn (rather than reading at exit) is the whole point: it is
        what lets a *signalled* child's priced envelope land somewhere instead of into a pipe
        nobody holds.
        """
        pending = [t for t in self._pumps if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        return self.stdout

    def close_stdin(self) -> None:
        """Close the cancellation channel. Called once the run is over so the transport is not
        left half-open for the garbage collector to complain about after the loop is gone."""
        stdin = self.proc.stdin
        if stdin is not None and not stdin.is_closing():
            with contextlib.suppress(Exception):
                stdin.close()

    def write_stdin(self, line: str) -> bool:
        """Write one line to the seat's stdin (the stream-json cancellation channel). Returns
        ``False`` when the pipe is already gone — a closed stdin means the rung is unavailable,
        never an exception the caller has to defend against."""
        stdin = self.proc.stdin
        if stdin is None or stdin.is_closing():
            return False
        try:
            stdin.write((line.rstrip("\n") + "\n").encode())
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            return False
        return True


def seat_argv(
    command: str,
    role: str,
    *,
    workspace: str,
    bead: str,
    instructions: str,
    session_id: str,
    model: str | None = None,
    stream_json: bool = True,
    bundle: str = "",
) -> tuple[str, ...]:
    """Build the settled role-binary argv (work-runtime-tiers-adr.md Amendment 2 §1)::

        bh-<seat> [--bundle <path>] --workspace <path> --bead <id> --instructions <file|->
                  --session_id <uuid> [--model <tier>]

    *command* is the `work.dispatch.seat_command` template (``bh-{role}`` by default),
    shell-split so a hive can point at a wrapper or, in tests and the demo, at the reference
    stub seat. ``--input-format stream-json`` is added by default because rungs 1 and 2 of the
    CANCEL ladder are writes to that channel — a seat spawned without it can only be cancelled
    by rung 3.

    *bundle* is the seat roster + permission mode (bh-xrg1f). It has to travel as `--bundle`
    because that is the ONLY lever the packed seat exposes: `bh-<seat> --help` carries no
    `--permission-mode`, so a bundle is the only way to say anything about a seat's authority
    from out here. Empty means "spawn bare", which resolves the default-closed seat — legitimate
    for the stub seat and for `work.dispatch.seat_bundle: "-"`, and fatal for a real write seat.

    A bundle already named in *command* WINS and suppresses this one. That is the documented
    escape hatch operators were pointed at while this was broken
    (`seat_command: "bh-{role} --bundle /path/to/bundle.json"`), and passing both would hand the
    seat two `--bundle` flags for it to pick between.
    """
    head = shlex.split(command.format(role=role))
    argv = [*head]
    if bundle and "--bundle" not in head:
        argv += ["--bundle", bundle]
    argv += [
        "--workspace",
        workspace,
        "--bead",
        bead,
        "--instructions",
        instructions,
        "--session_id",
        session_id,
    ]
    if model:
        argv += ["--model", model]
    if stream_json:
        argv += ["--input-format", "stream-json"]
    return tuple(argv)


def baml_profile_dir(session_id: str) -> Path:
    """Where THIS seat run's BAML profiler writes — a per-run path bh owns (bh-hum73).

    BAML 0.16.0 profiles by DEFAULT and, with ``BAML_PROFILE_DIR`` unset, writes into
    ``<cwd>/.baml/profiles/``. bh spawns every seat with ``cwd=<bead worktree>``, so an unset
    variable drops a ~276 KB ``.bamlprof`` into the bead's worktree on every run, which nothing
    ever reads back. The seat cannot fix this itself: ``baml.env`` is read-only, the profiler
    reads the variable at runtime start *before* user code runs, and a seat's only ``--add-dir``
    is its workspace. So the parent sets it, and the parent is bh.

    Keyed on ``session_id`` — per seat run (``_spawn_for`` / ``schedule`` mint a fresh uuid4 each
    time) — so two concurrent seats never share a profile dir.

    LANE B / OBSERVALOOP PLACEMENT — NOT WIRED; this is the site it attaches to. The plan is to
    hand the finished ``.bamlprof`` to observaloop's bridge, whose reader looks it up at
    ``otlp.capture_dir()/f"{trace_id}.bamlprof"``, ``trace_id = sha256("trace:"+journal_id)[:16]``.
    Two things block it from bh today, neither fixable here:

    * ``observaloop`` is NOT importable from bh's environment and is not a dependency — bh reaches
      it only as an MCP stdio subprocess (:mod:`beadhive.observaloop`), whose tool surface exposes
      no capture-spooling tool. The supported entry point
      (``observaloop.bridge.client.send(envelope, capture=...)``) is unreleased besides: installed
      observaloop 0.8.2 has neither ``capture=`` nor ``otlp.spool_capture``.
    * bh has NO ``journal_id``. Nothing in bh mints, receives, or parses one — a seat's ``SeatRun``
      envelope carries ``session_id`` and no journal id — so bh cannot compute the name the reader
      looks up. Deriving one here would be exactly the reimplemented, silently-driftable path
      derivation the bead forbids.

    THE ASSUMPTION TO CARRY WHEN IT IS WIRED: the spool is ``Path.home()/".observaloop"``,
    hardcoded with no env override, so placing a capture that way assumes bh and the bridge share
    a host AND a ``$HOME``. Separate them and Lane B fails SILENTLY — a capture written on the
    wrong host is invisible to the reader and aged out by the TTL sweep, with no error anywhere.
    The durable fix is a network route for capture bytes, filed upstream as obs-vuvn; when that
    lands, placement becomes a field on the POST and the assumption disappears. If a local write
    is ever built here, follow ``spool_capture``'s ``.part``-then-``os.replace`` convention — the
    reaper reads a stray ``.part`` as an interrupted write.
    """
    return config.home() / "baml-profiles" / session_id


async def spawn_seat(
    argv: Sequence[str],
    *,
    bead_id: str,
    role: str,
    action: str,
    session_id: str,
    provider_continuation: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
    env: dict | None = None,
    task_group: asyncio.TaskGroup | None = None,
    routing: dict | None = None,
    journal: run_journal.RunJournal | None = None,
    stdout_sink: Callable[[bytes], None] | None = None,
    stderr_sink: Callable[[bytes], None] | None = None,
) -> SeatProcess:
    """Spawn one seat run in **its own process group**, holding all three of its pipes.

    ``start_new_session=True`` is the load-bearing argument and is not negotiable: it calls
    ``setsid`` in the child, so the seat binary AND everything it forks share one pgid that this
    process can later kill as a unit. Without it there is no group to kill and cancellation
    reaches the direct child only — the orphaned-agent failure mode of bh-a7so.2 §3.

    stdin stays open (the CANCEL ladder's rungs 1 and 2 write to it) and stdout is drained by a
    task started here, so the read end is held for the entire life of the run.

    ``BAML_PROFILE_DIR`` is stamped HERE rather than at the call sites because this is the one
    choke point every seat spawn passes through (`LocalLoop._spawn_for`, `RuntimeAdapter.schedule`,
    and any future caller), so no call site can forget it — see :func:`baml_profile_dir`. *env*
    stays "inherit" by default: it is COPIED, never replaced, so the seat still gets bh's
    environment plus this one key.
    """
    env = dict(os.environ if env is None else env)
    provider_continuation = provider_continuation or session_id
    if journal is not None:
        # Reject a conflicting inherited identity before creating the process.  Journal I/O
        # failure itself remains non-fatal; an identity conflict is a launch-contract error.
        journal.bind_provider_continuation(provider_continuation)
        env = journal.child_env(env)
    profile_dir = baml_profile_dir(provider_continuation)
    with contextlib.suppress(OSError):  # unwritable home → BAML's problem, never a failed spawn
        profile_dir.mkdir(parents=True, exist_ok=True)
    env["BAML_PROFILE_DIR"] = str(profile_dir)
    proc = await asyncio.create_subprocess_exec(
        *[str(a) for a in argv],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
        start_new_session=True,  # <- the process GROUP. See this function's docstring.
    )
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:  # exited between spawn and probe; its own pid is still the pgid
        pgid = proc.pid
    seat = SeatProcess(
        bead_id=bead_id,
        role=role,
        action=action,
        session_id=session_id,
        provider_continuation=provider_continuation,
        proc=proc,
        pgid=pgid,
        argv=tuple(str(a) for a in argv),
        started_at=time.monotonic(),
        routing=routing,
        journal=journal,
    )

    async def _pump(stream, sink: list, live_sink: Callable[[bytes], None] | None) -> None:
        # Chunked, not `communicate()`: communicate only returns at EOF on BOTH streams, and a
        # grandchild holding the inherited stdout keeps EOF from ever arriving. Reading chunks
        # means the envelope is in hand as soon as the child writes it.
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            sink.append(chunk)
            if live_sink is not None:
                live_sink(chunk)

    # Under `LocalLoop.run` these are created in the loop's `asyncio.TaskGroup`, so the pump
    # tasks are genuinely supervised: an exception in one propagates up and cancellation
    # propagates down, which is the ONE thing TaskGroup is for here. It is emphatically not
    # process supervision — that is `reap_group`'s job, and conflating the two is the mistake
    # the ADR's own `local`-tier sketch made.
    spawn_task = task_group.create_task if task_group is not None else asyncio.create_task
    seat._pumps = (
        spawn_task(_pump(proc.stdout, seat._out, stdout_sink), name=f"stdout:{bead_id}"),
        spawn_task(_pump(proc.stderr, seat._err, stderr_sink), name=f"stderr:{bead_id}"),
    )
    _LOG.info(
        "seat_spawned",
        bead=bead_id,
        role=role,
        action=action,
        pid=proc.pid,
        pgid=pgid,
        session_id=session_id,
        provider_continuation=provider_continuation,
    )
    if journal is not None:
        activity: dict[str, object] = {
            "kind": "process.spawned",
            "phase": "starting",
            "process": {"pid": proc.pid, "pgid": pgid},
        }
        if journal.degraded:
            activity["journal_degraded"] = True
        journal.append(activity, operation="spawn")
    return seat


# --------------------------------------------------------------------------------------------
# Reaping and the CANCEL ladder
# --------------------------------------------------------------------------------------------

RUNG_COOPERATIVE = "cooperative"
RUNG_HARD = "hard"
RUNG_SIGNAL = "signal"
RUNG_EXITED = "exited"  # it finished on its own before any rung was needed
#: The ladder in order. Every rung returns a priced envelope, so a cancelled run is always
#: attributable and always budgeted (bh-a7so.7 §12).
CANCEL_LADDER: tuple[str, ...] = (RUNG_COOPERATIVE, RUNG_HARD, RUNG_SIGNAL)

#: Rung 1's payload. It must be a TRIGGER for behavior baked into the seat prompt (bh-c6dk.2),
#: never a novel mid-run instruction: bh-a7so.7 §7 recorded a seat correctly flagging an ad-hoc
#: "the scheduler says stop" message as prompt-injection-shaped and complying only because
#: committing is reversible. Rung 2 exists precisely because it is out-of-band and cannot be
#: reasoned about or declined.
WRAP_UP_INSTRUCTION = "INTERRUPT: wrap up now per the interrupt protocol in your seat prompt."

#: Rung 2's payload, verbatim from Amendment 2 §5.
CONTROL_REQUEST_INTERRUPT = json.dumps(
    {"type": "control_request", "request": {"subtype": "interrupt"}}
)


@dataclass(frozen=True)
class ReapResult:
    """What the reaper had to do, and whether it worked.

    ``group_gone`` is the acceptance-critical field: it is ``False`` only when something in the
    seat's process group survived a group SIGTERM *and* a group SIGKILL, which is the orphan
    condition this whole discipline exists to prevent.
    """

    group_gone: bool
    sent_sigterm: bool = False
    sent_sigkill: bool = False
    detail: str = ""


async def reap_group(seat: SeatProcess, *, grace: float, poll: float = 0.05) -> ReapResult:
    """Kill everything left in *seat*'s process group: group SIGTERM, poll, then group SIGKILL.

    Anything still answering to the pgid once the direct child has been reaped is a grandchild
    that outlived its parent: exactly bh-a7so.2 §3's orphan, caught here instead of left
    spending. (The child's own reaping is what the ``returncode`` check below waits for — an
    unreaped zombie is still a group member, and mistaking one for a live grandchild would make
    every reap look like it failed.)

    Polls until the group is actually gone at each stage rather than assuming a signal worked.
    """
    if seat.finished and not group_alive(seat.pgid):
        seat.close_stdin()
        return ReapResult(group_gone=True, detail="group already empty")

    sent_term = send_signal(seat.pgid, signal.SIGTERM, group=True)
    seat.signals.append("SIGTERM(group)")
    if await _wait_group_gone(seat, grace, poll):
        seat.close_stdin()
        return ReapResult(group_gone=True, sent_sigterm=sent_term, detail="group SIGTERM reaped")

    sent_kill = send_signal(seat.pgid, signal.SIGKILL, group=True)
    seat.signals.append("SIGKILL(group)")
    gone = await _wait_group_gone(seat, grace, poll)
    seat.close_stdin()
    if not gone:
        _LOG.error(
            "seat_group_survived_sigkill",
            bead=seat.bead_id,
            pgid=seat.pgid,
            reason="something in the seat's process group survived SIGKILL — this is the "
            "orphaned-agent failure mode; it must be reported, never assumed away",
        )
    return ReapResult(
        group_gone=gone,
        sent_sigterm=sent_term,
        sent_sigkill=sent_kill,
        detail="group SIGKILL reaped" if gone else "group SURVIVED SIGKILL",
    )


async def _wait_group_gone(seat: SeatProcess, grace: float, poll: float) -> bool:
    """Poll until BOTH the direct child has been reaped and its process group is empty.

    Both halves matter: an unreaped child is still a group member (so `group_alive` alone would
    never settle), and an empty-looking group with a live child would be a false all-clear.
    """
    deadline = time.monotonic() + max(grace, 0.0)
    while True:
        if seat.finished and not group_alive(seat.pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll)


@dataclass(frozen=True)
class CancelResult:
    """The outcome of walking the CANCEL ladder against one seat.

    ``classification`` comes from :func:`beadhive.seatrun.classify_run`, so a cancelled run is
    read through the SAME stdout-first rules as a completed one — a cooperative cancel really
    does come back as a `handoff` `SeatRun`, and a signalled one as an `INCOMPLETE` carrying a
    priced :class:`beadhive.seatrun.Envelope`.
    """

    bead_id: str
    rung: str
    stdout: str
    exit_code: int | None
    classification: seatrun.Classification
    reap: ReapResult
    signals: tuple[str, ...]

    @property
    def priced(self) -> bool:
        """Did the cancel come back attributable — a session id and a cost, from either shape?"""
        cls = self.classification
        return bool(cls.seat_run or cls.envelope)

    @property
    def session_id(self) -> str:
        cls = self.classification
        if cls.seat_run:
            return cls.seat_run.session_id
        if cls.envelope:
            return cls.envelope.session_id
        return ""

    @property
    def cost_usd(self) -> float:
        cls = self.classification
        if cls.seat_run:
            return cls.seat_run.cost_usd
        if cls.envelope:
            return cls.envelope.cost_usd
        return 0.0


async def cancel(
    seat: SeatProcess,
    *,
    rungs: Sequence[str] = CANCEL_LADDER,
    cooperative_grace: float = 60.0,
    hard_grace: float = 5.0,
    envelope_grace: float = 3.0,
    terminate_grace: float = 5.0,
) -> CancelResult:
    """Stop *seat*, walking the three-rung ladder and always finishing with the reaper.

    The rungs, measured on one task at one kill point (bh-a7so.7 §12) — each strictly faster and
    strictly less graceful, and **every one of them returns a priced envelope**:

    1. ``cooperative`` — write :data:`WRAP_UP_INSTRUCTION` to the seat's stream-json stdin. The
       seat finishes its in-flight tool call, commits ``wip: interrupted``, emits INTERRUPT_ACK
       and exits 0 with a `subtype:success` envelope. Ack +1.10s, clean exit +38s, clean tree,
       1.32× the cost of a hard kill.
    2. ``hard`` — :data:`CONTROL_REQUEST_INTERRUPT` on the same channel. Correlated ack +0.03s,
       priced envelope +0.09s. Out-of-band, so it cannot be reasoned about or declined; leaves
       the tree dirty.
    3. ``signal`` — SIGTERM **to the direct child only**, then hold the pipe for
       *envelope_grace* while the envelope arrives (+0.63s), and only THEN reap the group. That
       0.63s of patience is the entire difference between a priced, attributed cancel and a
       silent one. Never SIGINT (:data:`FORBIDDEN_SIGNALS`).

    Reaping is not cancellation: :func:`reap_group` always runs at the end as the FLOOR, so a
    grandchild that ignored every rung is still gone when this returns.
    """
    used = RUNG_EXITED
    for rung in rungs:
        if seat.finished:
            break
        if rung == RUNG_COOPERATIVE:
            if not seat.write_stdin(WRAP_UP_INSTRUCTION):
                continue
            used = RUNG_COOPERATIVE
            if await _wait_exit(seat, cooperative_grace):
                break
        elif rung == RUNG_HARD:
            if not seat.write_stdin(CONTROL_REQUEST_INTERRUPT):
                continue
            used = RUNG_HARD
            if await _wait_exit(seat, hard_grace):
                break
        elif rung == RUNG_SIGNAL:
            used = RUNG_SIGNAL
            # DIRECT CHILD ONLY, and the reader stays alive: this is the ordering the whole
            # design turns on. Killing the group here would take out the writer and the reader
            # in the same instant, which is what made bh-a7so.2 believe a killed run emits
            # zero bytes.
            send_signal(seat.pid, signal.SIGTERM, group=False)
            seat.signals.append("SIGTERM(child)")
            await _wait_exit(seat, envelope_grace)
        else:  # pragma: no cover - closed set, guarded by the caller
            raise ValueError(f"unknown cancel rung {rung!r}")

    stdout = await _collect_with_timeout(seat, envelope_grace)
    reap = await reap_group(seat, grace=terminate_grace)
    stdout = stdout or await _collect_with_timeout(seat, envelope_grace)
    exit_code = seat.proc.returncode
    classification = seatrun.classify_run(
        exit_code if exit_code is not None else -1, stdout, bead=seat.bead_id
    )
    result = CancelResult(
        bead_id=seat.bead_id,
        rung=used,
        stdout=stdout,
        exit_code=exit_code,
        classification=classification,
        reap=reap,
        signals=tuple(seat.signals),
    )
    _LOG.info(
        "seat_cancelled",
        bead=seat.bead_id,
        rung=used,
        exit_code=exit_code,
        priced=result.priced,
        session_id=seat.session_id,
        provider_continuation_observed=result.session_id,
        group_gone=reap.group_gone,
        signals=list(result.signals),
    )
    if seat.journal is not None:
        outcome, usage, cost = run_journal.activity_outcome(classification)
        process: dict[str, object] = {
            "exit_code": exit_code,
            # The journal contract spells a direct-child signal as ``SIGTERM`` and a group
            # signal as ``SIGTERM(group)``.  SeatProcess keeps the more explicit internal
            # ``SIGTERM(child)`` marker; normalize only the serialized observation.
            "signals": [value.replace("(child)", "") for value in result.signals],
            "group_gone": reap.group_gone,
        }
        if used != RUNG_EXITED:
            process["cancel_rung"] = used
        activity: dict[str, object] = {
            "kind": "process.cancelled",
            "phase": "finished" if reap.group_gone else "failed",
            "outcome_code": outcome,
            "process": process,
        }
        if usage:
            activity["usage"] = usage
        if cost:
            activity["cost_usd"] = cost
        if seat.journal.degraded:
            activity["journal_degraded"] = True
        seat.journal.append(activity, operation="cancel")
    return result


async def _wait_exit(seat: SeatProcess, timeout: float) -> bool:
    return await seat.wait_exit(timeout)


async def _collect_with_timeout(seat: SeatProcess, timeout: float) -> str:
    return await seat.collect(timeout=timeout)


# --------------------------------------------------------------------------------------------
# In-process caps — the pure decision core (and the seam a budget governor plugs into)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Caps:
    """The v1 caps, both IN-PROCESS and both correctly dying with the loop.

    These are the only two, and they have one spelling each: `work.dispatch.max_concurrency`
    (default 2; below 1 clamps to 1, so 0 can never mean "unlimited") and
    `work.dispatch.max_run_seconds` (default 1800.0; 0 disables). Nothing here reads a clock or
    touches disk — both values describe what is running RIGHT NOW, which is exactly why they are
    correctly volatile (docs/design/loop-ownership-and-execution-memory-adr.md Decision 2).

    A wall-time breach does not itself cancel anything: :func:`over_wall_time` produces the
    verdict and the three-rung CANCEL ladder (:func:`cancel`) acts on it, reading the priced
    envelope BEFORE the process group is reaped.

    A rolling token-budget window is the one thing that cannot live in beads (host/account
    scoped, read on the hot path, needs a TTL, unbounded value) and v1 does not build it:
    enforcement defers to bh-3yoh and R3 is knowingly half-met (operator decision 2026-08-10).
    """

    max_concurrency: int = 2
    max_run_seconds: float = 1800.0


@dataclass(frozen=True)
class SeatLaunchContext:
    """Already-validated launch inputs supplied by the production launch owner.

    LocalLoop owns process lifetime, not artifact discovery or provider provenance.  Qualified
    BAML callers resolve those facts before any claim and pass this closed value; compatibility
    callers leave the seam unset and retain the configured ``bh-{role}`` path.
    """

    command: str
    bundle: str
    hive: str
    driver: str
    provider: str
    manifest_digest: str

    def run_identity(self, bead: str) -> run_journal.RunIdentity:
        return run_journal.RunIdentity(
            hive=self.hive,
            bead=bead,
            driver=self.driver,
            provider=self.provider,
            manifest_digest=self.manifest_digest,
        )


ADMIT_OK = "ok"
ADMIT_AT_CONCURRENCY_CAP = "at_concurrency_cap"
ADMIT_LEASE_LOST = "lease_lost"
ADMIT_HALTED = "halted"
#: Closed set. A deny reason is always SURFACED (reported in the pass and logged), never silent
#: — the bh-h2yc failure mode is a quiet stall where a handoff was owed.
ADMIT_REASONS: tuple[str, ...] = (
    ADMIT_OK,
    ADMIT_AT_CONCURRENCY_CAP,
    ADMIT_LEASE_LOST,
    ADMIT_HALTED,
)


@dataclass(frozen=True)
class Admission:
    """allow/deny + a machine-readable reason. No I/O, fully unit-testable."""

    allowed: bool
    reason: str = ADMIT_OK
    detail: str = ""

    def __post_init__(self) -> None:
        if self.reason not in ADMIT_REASONS:
            raise ValueError(f"unknown admission reason {self.reason!r}")


def admit(
    caps: Caps, *, in_flight: int, lease_held: bool = True, halted: bool = False
) -> Admission:
    """May the loop spawn one more seat right now? Pure — mirrors the `schedule.py`/`molecule.py`
    decision-core pattern: given caps + current in-flight state, return allow/deny plus a reason.

    **This is the ONE admission decision core.** A second one (`dispatch_caps.check_admission` /
    `check_wall_time`, behind `work.dispatch.max_seats_in_flight` /
    `max_run_wall_time_seconds`) shipped alongside it with zero production callers and the
    OPPOSITE zero-sentinel semantics — 0 meaning "unlimited" there, 0 clamping to 1 here — and
    has been deleted. :class:`LocalLoop` still takes an ``admit=`` callable with exactly this
    signature and defaults to this function, so wiring a richer governor in later (the token
    budget, bh-3yoh) stays a constructor argument rather than a change to the loop body.

    Denial order is deliberate: a lost lease beats a full pipeline, because work that cannot be
    landed should not be started no matter how much room there is.
    """
    if not lease_held:
        return Admission(
            False,
            ADMIT_LEASE_LOST,
            "the host lease is no longer held — nothing spawned now could be landed",
        )
    if halted:
        return Admission(False, ADMIT_HALTED, "loop halted — a human owns the next move")
    if in_flight >= max(caps.max_concurrency, 1):
        return Admission(
            False,
            ADMIT_AT_CONCURRENCY_CAP,
            f"{in_flight} in flight, concurrency cap {caps.max_concurrency}",
        )
    return Admission(True, ADMIT_OK, f"{in_flight} in flight, cap {caps.max_concurrency}")


def over_wall_time(caps: Caps, age_seconds: float) -> bool:
    """Has one run exceeded the per-run wall-time cap? ``max_run_seconds <= 0`` disables it.
    Pure, so the cap is testable without waiting out a real half hour."""
    return caps.max_run_seconds > 0 and age_seconds >= caps.max_run_seconds


# --------------------------------------------------------------------------------------------
# The host lease — renewed only while workers are active
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseStatus:
    """Whether this host still holds the hive, and whether this pass renewed it."""

    held: bool
    renewed: bool = False
    detail: str = ""


class NullLeaseKeeper:
    """No host lease to keep — a scratch hive, a test, or a hive that was never adopted.

    Reports ``held=True`` always, which is honest rather than permissive: where no lease exists
    there is no fence to lose and no other host to hand off to. A hive that IS adopted gets
    :class:`HostLeaseKeeper`, and :func:`lease_keeper_for` picks between them.
    """

    def renew(self, *, active: bool) -> LeaseStatus:  # noqa: ARG002 - protocol shape
        return LeaseStatus(held=True, renewed=False, detail="no host lease recorded")


class HostLeaseKeeper:
    """Renew this host's lease on the loop's own tick, for as long as it has seats in flight.

    OBSERVED, NOT THEORISED (2026-08-10): a seat run outlived the 30-minute TTL, `bh work submit`
    then refused on a stale claim-fencing token, and the seat had to re-adopt and re-ack before
    it could land anything. A human was there to notice. An unattended loop has nobody, and seat
    runs in this molecule took 13-35 minutes each — so without renewal the failure mode is quiet
    and expensive: seats keep running and spending, and then every submit refuses.

    The multi-host ADR puts renewal exactly here: *"Renewal is a loop inside the dispatcher
    process that runs only while workers are active — no daemon, no cron"* (Amendment 1 §3).
    Hence ``active``: an IDLE host letting its lease lapse is the intended handoff, not a fault,
    so this renews nothing when nothing is in flight.

    The `held` answer is read back from the lease itself rather than inferred from the renewal
    call, because bh-tfapu leaves the epoch fence inoperable — enforcement is ADVISORY today, so
    the loop cannot rely on being fenced out and has to check and stop on its own.
    """

    #: The lease store: this host's HQ clone, and the same `origin` remote `guard.guard_primary`
    #: renews against. Named here rather than passed in so there is one answer to "where does
    #: the lease live", not two that can drift.
    REMOTE = "origin"

    def __init__(
        self, *, prefix: str, host_id: str, hq_dir: Path, ttl: float, renew_interval: float
    ):
        self.prefix = prefix
        self.host_id = host_id
        self.hq_dir = Path(hq_dir)
        self.ttl = ttl
        self.renew_interval = renew_interval

    def renew(self, *, active: bool) -> LeaseStatus:
        from . import host_lease

        renewed = False
        if active:
            outcome = host_lease.renew_if_due(
                self.REMOTE,
                self.prefix,
                host_id=self.host_id,
                cwd=self.hq_dir,
                ttl=self.ttl,
                renew_interval=self.renew_interval,
            )
            renewed = outcome is not None
        lease = host_lease.read_cached(self.prefix, cwd=self.hq_dir)
        if lease is None:
            # The lease vanished from under us. Not "free to proceed": something rewrote the
            # store, and this loop can no longer prove it may land anything.
            return LeaseStatus(held=False, renewed=renewed, detail="host lease record is gone")
        held = lease.held_by(self.host_id)
        return LeaseStatus(
            held=held,
            renewed=renewed,
            detail=f"held until {lease.expires_at}" if held else lease.describe(),
        )


def lease_keeper_for(hive: str = "", *, cfg=None, hive_dir: Path | None = None):
    """The right keeper for this hive: :class:`HostLeaseKeeper` when a lease is actually recorded
    for it, :class:`NullLeaseKeeper` otherwise.

    Resolution goes through :func:`beadhive.guard.primary_state` — the SAME resolver every gated
    write verb uses — so the loop can never disagree with `guard_primary` about which lease is
    in play or where it lives. A hive that was never adopted (no HQ clone, no lease ref) gets
    the null keeper, which is the single-host default and not a degraded mode.
    """
    from . import guard

    try:
        state = guard.primary_state(hive, cfg=cfg, hive_dir=hive_dir)
    except Exception as exc:  # pragma: no cover - never fail a pass on a lease lookup
        _LOG.warning("lease_keeper_unavailable", error=str(exc))
        return NullLeaseKeeper()
    if state is None:
        return NullLeaseKeeper()
    prefix, this_host, _lease = state
    return HostLeaseKeeper(
        prefix=prefix,
        host_id=this_host,
        hq_dir=config.hq_dir(),
        ttl=config.host_lease_ttl(cfg),
        renew_interval=config.host_lease_renew_interval(cfg),
    )


# --------------------------------------------------------------------------------------------
# Failure causes -> beads (write on FAILURE, never on attempt)
# --------------------------------------------------------------------------------------------

#: The state dimension this loop writes. `bd set-state <id> dispatch=<value> --reason "..."`
#: atomically creates an EVENT BEAD (the source of truth) and refreshes the `dispatch:<value>`
#: label (a fast-lookup cache) — an append-only log plus a materialised projection, built from
#: primitives that already ship.
DISPATCH_DIMENSION = state.DISPATCH_DIM

# The cause VALUES this module writes. They are NOT defined here: `dispatch` is one state
# dimension with one closed set, and that set lives in `state.py` alongside every other closed
# dimension — which is also what `bh label validate` and `work.dispatch_cause_count` read. These
# names are aliases onto it, so a value this loop can write is, by construction, a value the read
# path can count. (Before this, `localloop` kept its OWN seven-value tuple and the intersection
# with `state.STATE_DIMENSIONS["dispatch"]` was empty: every label the loop emitted would have
# failed validation and the loop-breaker could not count a single cause the loop wrote.)
CAUSE_FAILED = state.CAUSE_RUN_FAILED  # the run did not complete and produced no usable outcome
CAUSE_BLOCKED = state.CAUSE_RUN_BLOCKED  # the seat reported blocked — judgment; do not retry
CAUSE_HANDOFF = state.CAUSE_RUN_HANDOFF  # the seat handed off (incl. a cooperative cancel)
CAUSE_CANCELLED = state.CAUSE_RUN_CANCELLED  # the loop cancelled it (wall-time cap, shutdown)
CAUSE_MISMATCH = state.CAUSE_RUN_BEAD_MISMATCH  # the seat reported a different bead
CAUSE_LEASE_LOST = state.CAUSE_RUN_LEASE_LOST  # the host lease went away mid-flight
CAUSE_ESCALATED = state.CAUSE_ESCALATED  # the decision table escalated; a human owns the next move
CAUSE_PROVISIONING_FAILED = state.CAUSE_PROVISIONING_FAILED  # infra failure on the dispatch path
#: CLOSED set — the SAME object `state` registers for the `dispatch` dimension, not a copy.
DISPATCH_CAUSES: frozenset[str] = state.DISPATCH_CAUSES


def record_cause(cwd, bead: str, cause: str, *, reason: str, actor: str = "") -> bool:
    """Write a failure cause onto *bead* as a closed state-dimension value.

    **Write on FAILURE, never on attempt.** Event beads are permanent and this hive has no
    compaction tier (`bd compact` / `bd flatten` are forbidden until bh-3vs6c lands), so
    recording every dispatch attempt would accelerate the fastest-growing bead class for no gain
    a retry count needs. Bounces, stalls and escalations only — which is also everything a
    DERIVED count needs, since :func:`beadhive.work_next.attempt_count` counts these very event
    beads rather than reading a stored counter.

    This is the loop's ONLY durable write outside the ordinary lifecycle verbs, and it goes into
    beads — so v1 still persists nothing outside beads.
    """
    if cause not in state.STATE_DIMENSIONS[state.DISPATCH_DIM]:
        raise ValueError(
            f"unknown dispatch cause {cause!r} — the set is closed "
            f"(state.STATE_DIMENSIONS['dispatch'])"
        )
    res = bd_mod.run(
        ["set-state", bead, f"{DISPATCH_DIMENSION}={cause}", "--reason", reason],
        cwd,
        actor=actor,
        capture=True,
    )
    if res.returncode != 0:
        _LOG.warning(
            "dispatch_cause_write_failed",
            bead=bead,
            cause=cause,
            error=bd_mod.err_line(res),
        )
        return False
    _LOG.info("dispatch_cause_recorded", bead=bead, cause=cause, reason=reason)
    return True


# --------------------------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------------------------

#: The argv marker every seat run carries (the settled contract's required `--session_id`).
#: Combined with a `--bead <id>` match it is what makes orphan discovery specific enough to act
#: on: it can only ever match a process spawned against this hive's own beads through this
#: contract, never an unrelated program that happens to mention a bead id.
SEAT_ARGV_MARKER = "--session_id"


def find_orphan_seats(
    bead_ids, *, scope: str = "", ps_output: str | None = None
) -> tuple[tuple[int, int, str], ...]:
    """Find seat processes still running for *bead_ids* that no live loop owns.

    THE DECISION, RECORDED RATHER THAN LEFT IMPLICIT: an orphaned seat is **REAPED, NOT
    ADOPTED**. A restarted loop cannot adopt one even in principle — the pipes died with the old
    parent, so there is no stdin for CANCEL rungs 1 and 2 and no read end for the priced
    envelope. An "adopted" seat would be an uncancellable, unattributable process that the
    scheduler merely believes it controls, which is precisely the failure mode bh-a7so.2 §3
    measured from the other side. Reaping it and re-dispatching a fresh turn against the same
    worktree costs 0.38-0.42 of a full run (§8); pretending to own it costs correctness.

    Discovery derives from beads plus the OS process table — the bead ids come from `bd`, the
    pgids from `ps` — so **nothing is persisted** to make this work. That matters: the in-flight
    map is deliberately volatile, and a pgid file on disk would be exactly the runtime-only
    durable state the epic's invariant forbids.

    *scope* is a path every seat of THIS hive carries in its argv (the loop passes its hive
    directory, which the workspace and the default instructions file both sit under). It is
    required, and it is the difference between a targeted reap and a host-wide one: bead ids are
    only unique within a hive, so a scan keyed on `--bead <id>` alone would happily kill a live
    seat belonging to a different hive — or to a second loop — that happens to use the same id.
    Measured, not imagined: without it, two test workers on one machine reaped each other's
    seats. A caller that points `instructions` and `workspace` outside the hive narrows itself
    out of discovery; that is a miss, not a mis-kill, and `bd reclaim` remains the backstop.

    Returns `(pid, pgid, argv)` triples. `ps_output` is injectable so the matching logic is
    testable without spawning anything.
    """
    from .run import ps_argv
    from .run import run as run_cmd

    wanted = [str(b) for b in bead_ids if str(b)]
    if not wanted:
        return ()
    if ps_output is None:
        try:
            # `ps_argv`, not a hand-rolled `ps -eo …`: without `-ww` every token this scan
            # matches on — `--session_id`, `--bead <id>`, the scope path — sits past an
            # 80-column cut on a real seat argv, so the scan found NOTHING and said so silently
            # (bh-jwwls). A leaked dolt server costs RSS; a leaked seat costs tokens forever.
            res = run_cmd(ps_argv("pid=,pgid=,args="), check=False, capture=True)
        except FileNotFoundError as exc:
            # NO `ps` ON THIS HOST (bh-x2yy0). Distinct from "`ps` ran and failed" above, which
            # is a degraded scan and warns: this is a missing dependency, and it disables the
            # only mechanism that reaps a seat a killed loop left running and spending. Raised
            # by name rather than warned past, because the loop runs its passes in a TaskGroup
            # and an un-named error there reaches the operator as a bare `ExceptionGroup` —
            # which is exactly how this cost an afternoon to diagnose the first time.
            raise RuntimeError(
                "`ps` not found: the orphan-seat reap needs it (procps). Without it a seat left "
                "running by a killed loop cannot be found or stopped. Install procps, or run "
                "`bh setup check`, which now probes for it."
            ) from exc
        if res.returncode != 0:
            _LOG.warning("orphan_scan_unavailable", error=(res.stderr or "").strip()[:200])
            return ()
        ps_output = res.stdout or ""

    mine = os.getpid()
    found = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        argv = parts[2]
        if pid == mine or SEAT_ARGV_MARKER not in argv:
            continue
        if scope and scope not in argv:
            continue
        for bead in wanted:
            if f"--bead {bead}" in argv:
                found.append((pid, pgid, argv))
                break
    return tuple(found)


#: Which seat runs which action. The decision table names the ACTION; this maps it to the role
#: binary that performs it. A loop that improvised a role here would be re-acquiring exactly the
#: judgement the table exists to remove, so the map is closed and an unmapped action is a
#: non-spawning outcome (`wait` / `done` / `halt` / `escalate`), never a guess.
ROLE_FOR_ACTION: dict[str, str] = {
    "start": "dispatcher",
    "dispatch": "developer",
    "wrap_up": "developer",
    "resume": "developer",
    "review": "reviewer",
    "merge": "merger",
    "finish": "dispatcher",
}


def headless_capable(seat: str) -> bool:
    """Is *seat* ever dispatched headlessly (`--task` / `-d`)?

    Derived from :data:`ROLE_FOR_ACTION`'s own values, deliberately — that closed table is
    already the roster of seats something downstream will pick up an unattended run for, so a
    second hardcoded list here would be a source of truth that can silently disagree with it.
    Seats absent from it (supervisor, director, custodian, controller) are attached-only: a
    headless launch of one would produce a run nothing ever consumes, so callers refuse by name
    rather than starting it (bh-6t49w.6).
    """
    return seat in set(ROLE_FOR_ACTION.values())


def resolve_workspace(hive_dir: Path, bead: str) -> str:
    """The managed worktree *bead* is worked in, or the main clone when it has none yet.

    THE BUG THIS FIXES: only the `dispatch`/`wrap_up` path carried a workspace (it came back on
    the claim envelope). `resume` / `review` / `merge` / `finish` / `start` all fell through to
    the constructor default, which was `hive_dir` — the MAIN CLONE, i.e. the integration branch.
    `work.py::loop` never passes `workspace_for`, so in the only real deployment a `resume`
    handed a **developer seat with Edit/Write the integration branch**. Resolving per bead is
    the fix; :meth:`LocalLoop._spawn_for`'s developer-vs-main-clone refusal is the backstop.

    Read-only: `worktree.locate` is the side-effect-free resolver (no `git worktree add`, no
    `bd` write), so a pass never provisions as a side effect of deciding where to run.
    """
    from . import registry, worktree

    try:
        cfg = config.load()
        entry = registry.entry_for_dir(cfg, Path(hive_dir))
        hive = registry.hive_key(entry) if entry else ""
        _entry, _main, target, _branch = worktree.locate(cfg, hive, bead=bead)
        if Path(target).is_dir():
            return str(target)
        _LOG.warning("workspace_not_provisioned", bead=bead, expected=str(target))
    except Exception as exc:  # noqa: BLE001 - a resolver failure must not crash the pass
        _LOG.warning("workspace_resolve_failed", bead=bead, error=str(exc))
    return str(hive_dir)


@dataclass
class PassReport:
    """One pass, rendered. Volatile like everything else here — this is a return value and a log
    line, not a ledger; nothing writes it anywhere."""

    number: int = 0
    gate_resolved: int = 0
    reclaimed: tuple[str, ...] = ()
    lease: LeaseStatus = field(default_factory=lambda: LeaseStatus(True, False, ""))
    heartbeats: tuple[str, ...] = ()
    decision: work_next.Decision | None = None
    dispatched: tuple[str, ...] = ()
    #: Canonical per-bead model decisions made or reused this pass. This is the runtime envelope
    #: counterpart of `work schedule --json`; it records what actually reached launch argv.
    routing: tuple[tuple[str, dict], ...] = ()
    harvested: tuple[tuple[str, str], ...] = ()  # (bead, outcome)
    cancelled: tuple[tuple[str, str], ...] = ()  # (bead, rung)
    denied: tuple[Admission, ...] = ()
    declined: tuple[str, ...] = ()  # `bh work next` decline codes this pass
    orphans_reaped: tuple[str, ...] = ()  # seats a previous, killed loop left running
    causes: tuple[tuple[str, str], ...] = ()  # (bead, cause)
    #: The beads whose SEATS are still running at the end of this pass. `bh host dispatch
    #: status` advertises "seats in flight?" and reads exactly this key off the sink, so it is
    #: part of the report's contract, not debug colour — without it that column was
    #: structurally always 0.
    in_flight: tuple[str, ...] = ()
    halted: bool = False
    done: bool = False
    #: THE LOUD FLAG (bh-3xl60). Stamped on every `dispatch_pass` record a decide-only loop
    #: emits, not left to a banner alone, so a log read a week later can never mistake a dry
    #: pass for a real one. `dispatched` / `harvested` / `cancelled` / `causes` all being empty
    #: on a dry pass is what "nothing acted" looks like; `decision` still carries what WOULD
    #: have happened (row/action/beads/reason/detail) — the same record shape, no new fields
    #: beyond this one.
    dry_run: bool = False
    #: DRY-RUN ONLY: the beads this loop would actually be ALLOWED to claim this pass (bh-sh6yt).
    #: `decision.beads` is NOT that list and never was — it is the count bound the decision table
    #: produced from in-molecule dependencies alone, so it legitimately names beads `bd` reports
    #: as BLOCKED by a dependency outside the molecule (see `work_next._ready`, which defers to
    #: `bd ready` rather than inventing a deadlock). Read as a dispatch plan it lied twice over:
    #: it over-listed blocked beads, and — before `--epic` scoping — the real dispatch could take
    #: beads that appeared on no list at all. This field is the intersection that answers the
    #: question an operator is actually asking of `--dry-run`. Empty on a live pass, where the
    #: claim verb answers it authoritatively and one more `bd ready` read per pass buys nothing.
    claimable: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "pass": self.number,
            "dry_run": self.dry_run,
            "claimable": list(self.claimable),
            "gate_resolved": self.gate_resolved,
            "reclaimed": list(self.reclaimed),
            "lease": {"held": self.lease.held, "renewed": self.lease.renewed},
            "heartbeats": list(self.heartbeats),
            "decision": self.decision.as_dict() if self.decision else None,
            "dispatched": list(self.dispatched),
            "routing": {bead: decision for bead, decision in self.routing},
            "harvested": [list(h) for h in self.harvested],
            "cancelled": [list(c) for c in self.cancelled],
            "denied": [{"reason": d.reason, "detail": d.detail} for d in self.denied],
            "declined": list(self.declined),
            "orphans_reaped": list(self.orphans_reaped),
            "causes": [list(c) for c in self.causes],
            "in_flight": list(self.in_flight),
            "halted": self.halted,
            "done": self.done,
        }


@dataclass(frozen=True)
class ClaimResult:
    """What the atomic pick-claim-provision verb came back with. `bh work next` (bh-qczj) is the
    default implementation: exit 0 claimed / 3 declined / 4 refused, with the resolved worktree
    on the JSON envelope."""

    claimed: str = ""
    worktree: str = ""
    actor: str = ""
    reason: str = ""


def json_tail(stdout: str):
    """Parse the trailing JSON object off *stdout*, tolerating the human-readable progress lines
    that precede it.

    Same shape and same reason as `coordination._parse_json_tail`: `--json` changes the shape of
    the SUMMARY, not whether the verbs underneath print their own confirmations — `bh work next
    --json` really does emit `Updated issue: ...` and git's `HEAD is now at ...` ahead of its
    envelope. A caller that json.loads the whole stream sees a parse error and concludes the
    claim failed while the bead is in fact claimed: an ORPHANED CLAIM caused by a parser, which
    is exactly the class of bug an unattended loop cannot afford. (Observed while building the
    demo, not imagined.)
    """
    text = stdout or ""
    try:
        return json.loads(text)
    except ValueError:
        pass
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == "{"]
    if not starts:
        return None
    try:
        return json.loads("\n".join(lines[starts[-1] :]))
    except ValueError:
        return None


def bh_work_next(
    hive_dir: Path, actor: str, *, hive: str = "", bh: str = "bh", epic: str = ""
) -> ClaimResult:
    """Take the next ready bead through `bh work next --json` — the atomic pick-claim-provision
    verb, run as a subprocess so the loop gets the SAME race-free claim every other driver gets.

    Deliberately not re-implemented in-process: `bd update --claim` is not a compare-and-swap,
    and re-deriving the pick-then-claim race is exactly what an unattended loop must not do
    (bh-qczj is a recorded dependency of this bead for that reason).

    `epic` is what keeps that delegation honest (bh-sh6yt). Handing the claim to a hive-wide verb
    also handed it the whole hive: a loop pointed at one molecule spawned live seats for beads
    from other molecules, because the decision table bounded the COUNT and nothing bounded the
    IDENTITY. `--epic` is the identity bound, applied inside the same atomic verb rather than as
    a filter the loop re-derives around it.

    Exit codes are not consulted: the verb reports `claimed` / `declined` / `refused` on the
    envelope precisely so a driver never has to parse stderr or memorise exit numbers, and the
    envelope is also what carries the worktree it provisioned.
    """
    from .run import run as run_cmd

    argv = [bh, "work", "next", "--json"]
    if epic:
        argv += ["--epic", epic]
    if actor:
        argv += ["--as", actor]
    if hive:
        argv += ["--hive", hive]
    res = run_cmd(argv, cwd=str(hive_dir), check=False, capture=True)
    payload = json_tail(res.stdout or "")
    if not isinstance(payload, dict):
        _LOG.warning(
            "work_next_unparseable",
            exit_code=res.returncode,
            stderr=(res.stderr or "").strip()[:400],
        )
        return ClaimResult(reason=f"unparseable `bh work next` output (exit {res.returncode})")
    if str(payload.get("status") or "") != "claimed":
        return ClaimResult(reason=str(payload.get("reason") or payload.get("status") or "declined"))
    return ClaimResult(
        claimed=str(payload.get("bead") or ""),
        worktree=str(payload.get("worktree") or ""),
        actor=str(payload.get("actor") or actor),
    )


class LocalLoop:
    """The `local` tier's per-epic dispatcher: one pass, repeated, over one molecule.

    Scope is the epic container (dispatcher @ epic-container · fanout): it drives one molecule's
    ready set to landing and exits. There is no director loop in v1 and with a single hive it
    does not need one.

    Collaborators are injected rather than imported at the call site — ``claim``, ``admit``,
    ``lease``, ``instructions`` and ``spawn`` — so the pass is testable without a real seat
    binary, a real host lease, or a real claim race, and so bh-e7r9q's caps module plugs into
    ``admit`` without touching this body.

    ``dry_run=True`` (bh-3xl60) is the DECIDE-ONLY mode: :meth:`run_pass` still resolves gates
    (through `bd gate check --dry-run`, itself a read), still reads the ready set and consults
    the caps, still checks (never renews) the host lease, and still emits the SAME
    ``dispatch_pass`` event — with ``dry_run: true`` stamped on it so a log read a week later
    can never mistake it for a real pass — but :meth:`_act` is never called, so nothing is
    claimed, no worktree is provisioned, no seat is spawned, and no bead is written. `bd reclaim`
    and the once-per-startup orphan reap are SKIPPED rather than run "for real", because both are
    writes (`bd reclaim` reverts stale claims; the orphan reaper sends real signals) and `bd
    reclaim` has no read-only mode to fall back to the way `bd gate check` does — a decide-only
    pass would rather under-report a stale lease than write anything. :func:`_act` itself raises
    if it is ever invoked while ``dry_run`` is set, so the guard is enforced, not merely
    unexercised (bh-bwcxx's lesson): see the module's tests for the deliberate-write-attempt
    that proves it.
    """

    def __init__(
        self,
        *,
        hive_dir: Path,
        epic: str,
        actor: str,
        caps: Caps | None = None,
        seat_command: str = "bh-{role}",
        seat_bundle: str = "",
        harness: str = "claude",
        poll_interval: float = 5.0,
        envelope_grace: float = 3.0,
        terminate_grace: float = 5.0,
        max_action_retries: int = work_next.DEFAULT_MAX_ACTION_RETRIES,
        claim: Callable[[], ClaimResult] | None = None,
        admit: Callable[..., Admission] = admit,
        lease=None,
        instructions: Callable[[str, str, str], str] | None = None,
        env: dict | None = None,
        workspace_for: Callable[[str], str] | None = None,
        routing: Callable[[str, str], model_routing.ModelDecision | None] | None = None,
        run_identity: Callable[..., run_journal.RunIdentity | None] | None = None,
        launch_context: Callable[[str], SeatLaunchContext] | None = None,
        journal_base: Path | None = None,
        dry_run: bool = False,
    ):
        self.hive_dir = Path(hive_dir)
        self.epic = epic
        self.actor = actor
        self.caps = caps or Caps()
        self.seat_command = seat_command
        self.seat_bundle = seat_bundle
        self.harness = harness
        self.poll_interval = poll_interval
        self.envelope_grace = envelope_grace
        self.terminate_grace = terminate_grace
        self.max_action_retries = max_action_retries
        self._claim = claim or (lambda: bh_work_next(self.hive_dir, self.actor, epic=self.epic))
        self._admit = admit
        self.lease = lease or NullLeaseKeeper()
        self._instructions = instructions or self._default_instructions
        self.env = env
        self._workspace_for = workspace_for or (lambda bead: resolve_workspace(self.hive_dir, bead))
        self._gateway_availability = model_routing.GatewayAvailabilityAdapter()
        self._harness_availability = model_routing.HarnessAvailabilityAdapter()
        self._routing = routing or self._default_routing
        # Launch identity is supplied by the component that validates the role artifact and
        # manifest.  LocalLoop propagates it unchanged and never guesses provider provenance.
        self._run_identity = run_identity
        self._launch_context = launch_context
        self._journal_base = journal_base
        # Volatile by the same execution-memory rule as in_flight. Reuse within this loop keeps a
        # resume on the launch identity already chosen for the bead; restart re-derives from beads.
        self._resolved_routing: dict[str, model_routing.ModelSelection] = {}
        #: DECIDE-ONLY mode (bh-3xl60) — see the class docstring. Read once at construction and
        #: never flipped mid-run, so a dry loop cannot accidentally start acting partway through.
        self.dry_run = dry_run
        #: The in-flight map. DELIBERATELY VOLATILE — see the module docstring.
        self.in_flight: dict[str, SeatProcess] = {}
        self.halted = False
        self.done = False
        #: Set by the SIGTERM handler — "wind up at the next safe point", not "die now".
        self.stopping = False
        #: The wake-up channel that handler uses; only live for the duration of :meth:`run`.
        self._stop: asyncio.Event | None = None
        self.passes = 0
        self._instruction_dir: Path | None = None
        #: Set for the lifetime of :meth:`run` so spawned pumps join the supervised task tree.
        #: `None` when a caller drives `run_pass` directly (tests, the demo), which is fine —
        #: the pumps then stand alone and `shutdown` still reaps every process group.
        self._tg: asyncio.TaskGroup | None = None

    # ---- instructions ---------------------------------------------------------------------

    def _default_instructions(self, action: str, bead: str, role: str) -> str:
        """Write a minimal brief for one seat run and return its path.

        Scratch, not state: the file lives under the hive's `.beads/dispatch/` scratch dir and
        carries only what the contract needs (`--instructions <file>`). Everything the seat
        actually reasons about it reads from beads and git itself — RECOVERY is re-dispatch of a
        fresh turn against the same worktree, so an instructions file is never a checkpoint.
        """
        if self._instruction_dir is None:
            self._instruction_dir = self.hive_dir / ".beads" / "dispatch"
            self._instruction_dir.mkdir(parents=True, exist_ok=True)
        path = self._instruction_dir / f"{bead}.{action}.md"
        path.write_text(
            f"# {role} — {action} {bead}\n\n"
            f"Molecule: {self.epic}\n"
            f"Action: {action}\n"
            f"Bead: {bead}\n\n"
            "Drive this bead through `bh work` per your seat prompt. Commit after every step —\n"
            "the branch is the checkpoint, and a restart re-dispatches a fresh turn against\n"
            "this same worktree rather than resuming a dead session.\n",
            encoding="utf-8",
        )
        return str(path)

    def _default_routing(self, bead: str, role: str) -> model_routing.ModelDecision | None:
        """Read current bead/config facts and call the same pure resolver as schedule advice."""
        from . import registry

        cfg = config.load()
        entry = registry.entry_for_dir(cfg, self.hive_dir) or {}
        data = bd_mod.show(bead, self.hive_dir, strict=True)
        routes = config.routing_tiers(cfg, entry)
        # Empty routing config means the feature is not enabled, preserving the established
        # harness-default launch. An explicit model preference still asks routing to decide and
        # therefore gets an honest blocked verdict when no route can satisfy it.
        if not routes and not schedule.model_preference(data):
            return None
        harness = config.harness_name(cfg, entry)
        availability = model_routing.discover_availability(
            routes,
            role=role,
            harness=harness,
            gateway=self._gateway_availability,
            harness_defaults=self._harness_availability,
        )
        return schedule.resolve_launch_decision(
            [data],
            policy=config.routing_policy(cfg, entry),
            role=role,
            harness=harness,
            routes=routes,
            availability=availability,
        )

    @staticmethod
    def _routing_attributes(decision: model_routing.ModelSelection) -> dict:
        """Secret-safe low-cardinality routing facts for GenAI telemetry."""
        attrs = {
            "bh.routing.required_complexity": decision.required_tier.name,
            "bh.routing.selected_model": decision.selected_model,
            "bh.routing.availability_source": decision.availability_source,
            "bh.routing.selection_reason": decision.selection_reason,
            "bh.routing.endpoint_source": "gateway" if decision.endpoint else "harness_default",
        }
        if decision.preferred_model:
            attrs["bh.routing.preferred_model"] = decision.preferred_model
        return attrs

    # ---- molecule ------------------------------------------------------------------------

    def claimable_now(self, decision: work_next.Decision) -> tuple[str, ...]:
        """Of the beads *decision* names, the ones `bd` agrees are ready RIGHT NOW (bh-sh6yt).

        This is the honest answer to "what would a dispatch pass take", and it is narrower than
        `decision.beads` for a reason the decision table is right about: `work_next._ready` only
        knows in-molecule dependencies and deliberately defers to `bd ready` on everything else,
        so a bead blocked by an out-of-molecule dependency still appears on the decision. That is
        correct for sizing a budget and wrong for reading as a plan.

        Only meaningful for a `dispatch` action — every other action names beads the loop already
        holds, where there is no claim to be allowed or refused.
        """
        if decision.action != "dispatch" or not decision.beads:
            return ()
        rows = bd_mod.json(["ready", "--limit", "0"], self.hive_dir) or []
        ready = {str(r.get("id") or "") for r in rows if isinstance(r, dict)}
        return tuple(b for b in decision.beads if b in ready)

    def load_molecule(self, budget: int) -> work_next.Molecule:
        """Re-derive the whole decision input from `bd`, every pass.

        Nothing is cached between passes on purpose: this is the property that makes a restart a
        no-op. The molecule a fresh process sees on its first pass is byte-for-byte the molecule
        the dead process would have seen on its next one.

        `--all` on both reads is load-bearing, not defensive (measured while building the demo):

        * `bd list` hides CLOSED issues by default, and every state-change **event bead is
          created closed**. Without `--all` the event list comes back empty forever, so
          :func:`beadhive.work_next.attempt_count` derives zero and the loop-breaker can never
          fire — a dispatcher that never gives up, which the ADR names as the worse failure.
        * The decision table derives "which beads are ready" from which of the molecule's
          dependencies are CLOSED, so dropping closed children would make finished work look
          unfinished and hold the ready set back.
        """
        epic_row = bd_mod.show(self.epic, self.hive_dir) or {}
        rows = bd_mod.json(
            ["list", "--parent", self.epic, "--include-infra", "--all"], self.hive_dir
        )
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        children = [r for r in rows if str(r.get("issue_type") or "") not in work_next.INFRA_TYPES]
        events: dict[str, list[dict]] = {}
        for child in children:
            bead = str(child.get("id") or "")
            child_rows = bd_mod.json(
                ["list", "--parent", bead, "--include-infra", "--all"], self.hive_dir
            )
            events[bead] = [
                r
                for r in (child_rows or [])
                if isinstance(r, dict) and str(r.get("issue_type") or "") == "event"
            ]
        return work_next.Molecule(
            epic=self.epic,
            epic_status=str(epic_row.get("status") or "open"),
            dispatchable=True,
            beads=tuple(children),
            events=events,
            escalations=(),
            budget=max(budget, 0),
            max_action_retries=self.max_action_retries,
        )

    # ---- the pass ---------------------------------------------------------------------------

    async def run_pass(self) -> PassReport:
        """One full pass. Every step is re-derived; nothing carries over but the in-flight map
        (and that is allowed to vanish).

        ``self.dry_run`` (bh-3xl60) takes the SAME steps 1/3/7 (gate resolution done through
        `bd gate check`'s own read-only mode, the lease CHECKED but never renewed, the ready set
        + caps read to produce a real :class:`work_next.Decision`) and skips every step that
        writes: reclaim (2), the once-per-startup orphan reap (2b, which sends real signals),
        worker heartbeats and wall-time cancellation and harvest (4-6, all no-ops anyway since a
        dry loop never has anything in flight), and — the one that matters most — step 8's
        :meth:`_act`, never called at all. See the class docstring for why reclaim has no
        read-only fallback and is skipped outright rather than "run for real"."""
        self.passes += 1
        report = PassReport(number=self.passes, dry_run=self.dry_run)

        # 1. Gates self-resolve (timer / gh:run / gh:pr / bead). A human gate is NEVER touched
        #    here — which is exactly why a ready bead behind an open type:human gate is not
        #    dispatched: it never appears as ready until a person resolves it. `dry_run=True`
        #    forwards straight to `bd gate check --dry-run`: a real evaluation, zero writes.
        gate = coordination.gate_check(self.hive_dir, actor=self.actor, dry_run=self.dry_run)
        report.gate_resolved = gate.resolved

        if not self.dry_run:
            # 2. Dead-worker recovery. THE backstop, not the normal path: a cancelled seat
            #    releases its own claim, and this catches the holder that simply died (lease
            #    TTL 5 min). SKIPPED in dry_run: `bd reclaim` reverts stale claims — a write —
            #    and has no `--dry-run` of its own to fall back to.
            rec = coordination.reclaim(self.hive_dir, actor=self.actor)
            report.reclaimed = rec.reclaimed_ids

            # 2b. STARTUP RECONCILIATION, once: a seat left running by a loop that was killed -9
            #     is still spending. It cannot be adopted (its pipes died with its parent), so
            #     it is reaped and its bead released — see `find_orphan_seats`. SKIPPED in
            #     dry_run: this sends real signals to real processes and releases real claims,
            #     which is exactly the opposite of "nothing claimed, no seat spawned".
            if self.passes == 1:
                report.orphans_reaped = await self.reap_orphan_seats()

        # 3. The host lease — renewed only while workers are active. A dry pass never has any
        #    (see below), so `active` is unconditionally False and this call is a pure READ:
        #    `HostLeaseKeeper.renew(active=False)` skips `host_lease.renew_if_due` and only
        #    answers "is it held", which is the "must still hold the lease check" requirement —
        #    a dry pass that skipped this would report what a loop WOULD do in a state it could
        #    not legally be in.
        report.lease = self.lease.renew(active=bool(self.in_flight) and not self.dry_run)
        if not report.lease.held:
            if self.dry_run:
                # Mirrors _handle_lease_loss's VERDICT (halted, no more dispatching) without its
                # WRITES (record_cause onto the epic, cancelling in-flight seats — there are
                # none to cancel here regardless). A plain attribute set, no I/O.
                self.halted = True
                _LOG.warning(
                    "host_lease_lost_mid_flight",
                    hive=str(self.hive_dir),
                    epic=self.epic,
                    detail=report.lease.detail,
                    dry_run=True,
                )
            else:
                await self._handle_lease_loss(report)

        if not self.dry_run:
            # 4. The WORKER lease for each in-flight bead. Nothing releases a claim on its own;
            #    a worker that stops heartbeating is exactly what makes its lease reclaimable.
            #    A dry loop never has anything in `in_flight` (nothing here ever spawns one), so
            #    this is skipped rather than iterated over nothing.
            beats = []
            for bead in list(self.in_flight):
                if coordination.heartbeat(self.hive_dir, bead, actor=self.actor).ok:
                    beats.append(bead)
            report.heartbeats = tuple(beats)

            # 5. The per-run wall-time cap, enforced through the ladder rather than a bare kill
            #    so even a capped-out run comes back priced.
            await self._enforce_wall_time(report)

            # 6. Harvest anything that finished, classifying stdout-first.
            await self._harvest(report)

        # 7. Decide — pure, no I/O beyond the `bd` READS `load_molecule` already does to
        #    resolve the ready set and the caps this budget represents.
        room = max(self.caps.max_concurrency - len(self.in_flight), 0)
        decision = work_next.decide(self.load_molecule(self.caps.max_concurrency))
        report.decision = decision

        # 7b. DRY-RUN ONLY: resolve the count bound into the set that could really be claimed
        #     (bh-sh6yt). Costs one extra `bd ready` READ on a pass that is already read-only,
        #     and it is the only way `--dry-run` can answer "which beads" without running the
        #     claim verb — which claims.
        if self.dry_run:
            report.claimable = self.claimable_now(decision)

        # 8. Execute the closed action vocabulary — UNLESS this is a decide-only pass, in which
        #    case `_act` is never called at all. `decision` above already carries everything an
        #    operator needs to see what WOULD happen (row/action/beads/reason/detail); `_act`
        #    itself refuses to run under `dry_run` (see its docstring) as the enforced backstop,
        #    not just a control-flow convention.
        if not self.dry_run:
            await self._act(decision, report, room)

        report.in_flight = tuple(self.in_flight)
        report.halted = self.halted
        report.done = self.done
        _LOG.info("dispatch_pass", **report.as_dict())
        return report

    def _install_sigterm_handler(self) -> bool:
        """Install an asyncio SIGTERM handler so graceful shutdown actually runs.

        THIS IS THE ONLY SIGNAL THIS PROCESS WILL EVER GET. The systemd unit is
        `Restart=always` with `KillMode=control-group`, so `systemctl stop` / a restart / a
        reboot all arrive as SIGTERM. Python's DEFAULT disposition for SIGTERM terminates the
        interpreter outright — no exception, no unwinding — so `run`'s `finally: await
        self.shutdown()` never executes. Everything shutdown exists to do is lost: the priced
        envelope is never read, the cause is never written to the bead, and the claim is left to
        age out over the 5-minute lease TTL while its seat may still be alive and spending.

        Returns whether the handler was installed. It cannot be on a non-main thread (a caller
        driving `run` from :class:`LocalRuntime`'s private loop thread, or from a test) or on a
        platform without `add_signal_handler` — in both cases the loop still runs, it simply
        keeps the pre-existing disposition, so this degrades rather than refusing to start.
        """
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self._on_sigterm)
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            _LOG.debug("sigterm_handler_unavailable", error=str(exc))
            return False
        return True

    def _remove_sigterm_handler(self) -> None:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)

    def _on_sigterm(self) -> None:
        """Ask the loop to stop at the next safe point. Deliberately does NOT kill anything
        here: the reaping is `shutdown`'s job and it must run the CANCEL ladder (envelope read
        BEFORE the group is reaped), which cannot happen inside a signal handler."""
        _LOG.warning("sigterm_received", epic=self.epic, in_flight=list(self.in_flight))
        self.stopping = True
        if self._stop is not None:
            self._stop.set()

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep between passes, but wake immediately on SIGTERM — a loop that only noticed the
        signal after a full `poll_interval` would still be the wrong shutdown latency."""
        if self._stop is None:
            await asyncio.sleep(seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def run(
        self,
        *,
        max_passes: int | None = None,
        on_pass: Callable[[PassReport], None] | None = None,
    ) -> list[PassReport]:
        """Poll until the molecule is done, the loop halts, SIGTERM arrives, or *max_passes* is
        reached.

        `asyncio.TaskGroup` is the supervision tree for the TASK layer — cancellation propagates
        down, exceptions propagate up, a failing sibling cancels the group. It is NOT process
        supervision; that is :func:`reap_group`'s job, and :meth:`shutdown` is what connects the
        two so a cancelled task tree never leaves a live process tree behind.

        `on_pass` is called with each report AS IT COMPLETES, before the next pass starts. That
        is what makes `bh work loop --json` a stream rather than a batch: with `--passes 0` the
        return value does not exist until the molecule lands (possibly hours), and the list
        grows without bound in the meantime.
        """
        reports: list[PassReport] = []
        self._stop = asyncio.Event()
        installed = self._install_sigterm_handler()
        async with asyncio.TaskGroup() as tg:
            self._tg = tg
            try:
                while True:
                    report = await self.run_pass()
                    reports.append(report)
                    if on_pass is not None:
                        on_pass(report)
                    if self.done or self.halted or self.stopping:
                        break
                    if max_passes is not None and len(reports) >= max_passes:
                        break
                    await self._sleep_or_stop(self.poll_interval)
            finally:
                if installed:
                    self._remove_sigterm_handler()
                self._stop = None
                # INSIDE the group, deliberately: a TaskGroup does not exit until every child
                # task finishes, and a pump task only finishes when its pipe closes — which
                # only happens once the seat's process group is reaped. Shutting down after the
                # `async with` would therefore deadlock on exactly the live-seat case shutdown
                # exists to handle.
                await self.shutdown()
                self._tg = None
        return reports

    async def shutdown(self, *, cause: str = CAUSE_CANCELLED, reason: str = "loop shutting down"):
        """Stop every in-flight seat through the ladder and confirm each process GROUP is gone.

        SIGTERM to the loop itself lands here — really, not aspirationally: :meth:`run` installs
        :meth:`_on_sigterm` via `add_signal_handler`, which flips `stopping` and wakes the poll
        sleep so the `finally` below runs. Children are then terminated through their groups,
        each one's cause is written to its bead, and the claim is released rather than left to
        age out over the 5-minute TTL. `bd reclaim` stays the backstop for the case this path
        never runs (kill -9 of the loop), which is the restart story, not this one.
        """
        for bead, seat in list(self.in_flight.items()):
            result = await cancel(
                seat,
                envelope_grace=self.envelope_grace,
                terminate_grace=self.terminate_grace,
            )
            self.in_flight.pop(bead, None)
            record_cause(
                self.hive_dir,
                bead,
                cause,
                reason=f"{reason} (rung {result.rung}, session {result.session_id or 'unknown'})",
                actor=self.actor,
            )
            self._release(bead)

    # ---- pass steps --------------------------------------------------------------------------

    async def reap_orphan_seats(self) -> tuple[str, ...]:
        """Reap seat processes a previous loop left behind, and release the beads they held.

        Called once at startup. This is the OTHER side of the orphan problem: bh-a7so.2 §3
        watched a supervisor kill its child badly; here the supervisor itself disappeared. The
        answer is the same in both directions — kill the process GROUP, then let `bd` be the
        record of what happened — and the decision (reap, never adopt) is argued in
        :func:`find_orphan_seats`.
        """
        molecule = self.load_molecule(self.caps.max_concurrency)
        bead_ids = [str(b.get("id") or "") for b in molecule.beads]
        reaped = []
        for pid, pgid, argv in find_orphan_seats(bead_ids, scope=str(self.hive_dir)):
            bead = next((b for b in bead_ids if f"--bead {b}" in argv), "")
            _LOG.warning("orphan_seat_found", bead=bead, pid=pid, pgid=pgid)
            send_signal(pgid, signal.SIGTERM, group=True)
            deadline = time.monotonic() + self.terminate_grace
            while group_alive(pgid) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if group_alive(pgid):
                send_signal(pgid, signal.SIGKILL, group=True)
            reaped.append(bead or str(pid))
            if bead:
                record_cause(
                    self.hive_dir,
                    bead,
                    CAUSE_CANCELLED,
                    reason=(
                        f"orphaned seat (pid {pid}, pgid {pgid}) reaped on loop restart — not "
                        "adopted: its pipes died with the loop that spawned it, so it could "
                        "neither be cancelled nor priced. Re-dispatch is a fresh turn."
                    ),
                    actor=self.actor,
                )
                self._release(bead)
        return tuple(reaped)

    async def _handle_lease_loss(self, report: PassReport) -> None:
        """Lost the host lease mid-flight: STOP DISPATCHING and escalate — never keep spawning
        seats whose work cannot be landed.

        Handled explicitly rather than left to surface as a submit refusal, because bh-tfapu
        leaves the epoch fence inoperable: enforcement is advisory, so nothing else will stop
        this loop. In-flight seats are wrapped up COOPERATIVELY rather than hard-killed — their
        commits are still worth having on the branch even though this host can no longer land
        them (the branch is the checkpoint), which is also what makes the eventual re-dispatch
        cheap.
        """
        self.halted = True
        _LOG.error(
            "host_lease_lost_mid_flight",
            hive=str(self.hive_dir),
            epic=self.epic,
            detail=report.lease.detail,
        )
        record_cause(
            self.hive_dir,
            self.epic,
            CAUSE_LEASE_LOST,
            reason=f"host lease lost mid-flight; dispatch stopped ({report.lease.detail})",
            actor=self.actor,
        )
        report.causes += ((self.epic, CAUSE_LEASE_LOST),)
        await self.shutdown(
            cause=CAUSE_LEASE_LOST, reason="host lease lost mid-flight — wrapping up"
        )

    async def _enforce_wall_time(self, report: PassReport) -> None:
        now = time.monotonic()
        for bead, seat in list(self.in_flight.items()):
            if seat.finished or not over_wall_time(self.caps, seat.age(now)):
                continue
            result = await cancel(
                seat,
                envelope_grace=self.envelope_grace,
                terminate_grace=self.terminate_grace,
            )
            self.in_flight.pop(bead, None)
            report.cancelled += ((bead, result.rung),)
            record_cause(
                self.hive_dir,
                bead,
                CAUSE_CANCELLED,
                reason=(
                    f"per-run wall-time cap {self.caps.max_run_seconds}s exceeded; cancelled at "
                    f"rung {result.rung}, session {result.session_id or 'unknown'}, "
                    f"cost {result.cost_usd}"
                ),
                actor=self.actor,
            )
            report.causes += ((bead, CAUSE_CANCELLED),)
            await self.notify_siblings(bead, f"sibling {bead} was cancelled (wall-time cap)")
            self._release(bead)

    async def _harvest(self, report: PassReport) -> None:
        for bead, seat in list(self.in_flight.items()):
            if not seat.finished:
                continue
            stdout = await _collect_with_timeout(seat, self.envelope_grace)
            reap = await reap_group(seat, grace=self.terminate_grace)
            if not reap.group_gone:
                _LOG.error("seat_group_orphaned", bead=bead, pgid=seat.pgid)
            cls = seatrun.classify_run(seat.proc.returncode or 0, stdout, bead=bead)
            self.in_flight.pop(bead, None)
            report.harvested += ((bead, str(cls.outcome)),)
            cause = self._cause_for(cls)
            if cause:
                record_cause(
                    self.hive_dir,
                    bead,
                    cause,
                    reason=self._cause_reason(cls, seat),
                    actor=self.actor,
                )
                report.causes += ((bead, cause),)
            _LOG.info(
                "seat_harvested",
                bead=bead,
                outcome=str(cls.outcome),
                exit_code=seat.proc.returncode,
                session_id=seat.session_id,
                provider_continuation_observed=(
                    cls.seat_run.session_id
                    if cls.seat_run
                    else cls.envelope.session_id
                    if cls.envelope
                    else ""
                ),
            )
            if seat.journal is not None:
                outcome, usage, cost = run_journal.activity_outcome(cls)
                activity: dict[str, object] = {
                    "kind": "process.harvested",
                    "phase": "finished" if reap.group_gone else "failed",
                    "outcome_code": outcome,
                    "process": {
                        "exit_code": seat.proc.returncode,
                        "group_gone": reap.group_gone,
                    },
                }
                if usage:
                    activity["usage"] = usage
                if cost:
                    activity["cost_usd"] = cost
                if seat.journal.degraded:
                    activity["journal_degraded"] = True
                seat.journal.append(activity, operation="harvest")

    @staticmethod
    def _cause_for(cls: seatrun.Classification) -> str:
        """Map a classification to a cause — or to nothing.

        A `done` run writes NOTHING: write on failure, not on attempt. A bead-id mismatch is a
        failure even when the status says done, because the seat advanced something other than
        what it was handed.
        """
        if cls.bead_id_mismatch:
            return CAUSE_MISMATCH
        return {
            seatrun.RunOutcome.DONE: "",
            seatrun.RunOutcome.BLOCKED: CAUSE_BLOCKED,
            seatrun.RunOutcome.HANDOFF: CAUSE_HANDOFF,
            seatrun.RunOutcome.INCOMPLETE: CAUSE_FAILED,
        }[cls.outcome]

    @staticmethod
    def _cause_reason(cls: seatrun.Classification, seat: SeatProcess) -> str:
        parts = [f"exit {seat.proc.returncode}", f"action {seat.action}"]
        if cls.seat_run:
            parts.append(f"session {cls.seat_run.session_id}")
            parts.append(f"cost {cls.seat_run.cost_usd}")
            if cls.seat_run.outcome.summary:
                parts.append(cls.seat_run.outcome.summary)
        elif cls.envelope:
            parts.append(f"session {cls.envelope.session_id}")
            parts.append(f"terminal_reason {cls.envelope.terminal_reason}")
        else:
            parts.append(cls.detail)
        return "; ".join(p for p in parts if p)

    def _escalation_open(self, bead: str) -> bool:
        """Whether *bead* already carries an UNRESOLVED `escalation:raised`."""
        row = bd_mod.show(bead, self.hive_dir) or {}
        return state.is_escalation_raised(row.get("labels") or [])

    def _escalate(self, bead: str, decision: work_next.Decision, report: PassReport) -> None:
        """Raise ONE escalation per bead and then stay quiet until a human resolves it.

        WHY THE DEDUPE IS LOAD-BEARING, not tidiness. `dispatch_hive_run` re-picks a halted epic
        and `Restart=always` restarts a halted loop, so the escalate row fires again on every
        cycle. Each firing wrote a `dispatch=escalated` EVENT BEAD, and event beads are
        PERMANENT in this hive (`bd compact` / `bd flatten` are forbidden until bh-3vs6c lands).
        At the default `poll_interval: 10.0` a single bead awaiting human triage would mint on
        the order of 8,640 permanent beads a day. So: `escalation:raised` is the latch. Raising
        it once is the whole point of a fire-and-forget escalation — the SECOND identical
        escalation carries no information a human does not already have.
        """
        if self._escalation_open(bead):
            _LOG.info(
                "escalation_already_open",
                bead=bead,
                row=decision.row,
                reason=decision.reason,
            )
            return
        reason = f"{decision.row}: {decision.reason} — {decision.detail}"
        record_cause(self.hive_dir, bead, CAUSE_ESCALATED, reason=reason, actor=self.actor)
        # The latch itself. Written AFTER the cause so a crash between the two re-escalates
        # (one duplicate event) rather than silently swallowing the escalation entirely.
        res = bd_mod.run(
            ["set-state", bead, f"{state.ESCALATION_DIM}=raised", "--reason", reason],
            self.hive_dir,
            actor=self.actor,
            capture=True,
        )
        if res.returncode != 0:
            _LOG.warning("escalation_latch_failed", bead=bead, error=bd_mod.err_line(res))
        report.causes += ((bead, CAUSE_ESCALATED),)

    async def _act(self, decision: work_next.Decision, report: PassReport, room: int) -> None:
        """Execute the closed action vocabulary `work_next.decide` returned — claim, provision,
        spawn, escalate, or halt, as the row named.

        THE ENFORCED GUARD (bh-3xl60), not merely an unexercised convention: :meth:`run_pass`
        already never calls this under ``dry_run``, but a guard nobody has watched fail is not
        evidence of anything (bh-bwcxx) — so this raises outright if it is ever reached while
        ``dry_run`` is set, rather than trusting the one call site upstream to stay correct
        forever. A test calls this directly with ``dry_run=True`` to prove the raise fires.
        """
        if self.dry_run:
            raise RuntimeError(
                "LocalLoop._act invoked during a --dry-run pass — a decide-only loop must "
                "never claim, provision, spawn, or write a bead (bh-3xl60)"
            )
        action = decision.action
        if action == "done":
            self.done = True
            return
        if action in ("halt", "wait"):
            self.halted = self.halted or action == "halt"
            return
        if action == "escalate":
            for bead in decision.beads or (self.epic,):
                self._escalate(bead, decision, report)
            self.halted = True
            return

        role = ROLE_FOR_ACTION.get(action)
        if role is None:  # pragma: no cover - ACTIONS is closed and fully mapped above
            raise ValueError(f"no seat role for action {action!r}")

        if action in ("dispatch", "wrap_up"):
            await self._dispatch_ready(decision, report, room, role)
            return
        # Every other action names a bead that already exists and is already this loop's to
        # drive — no claim race to run, so spawn the seat for it directly.
        for bead in decision.beads[: max(room, 1)]:
            await self._spawn_for(bead, action=action, role=role, report=report)

    async def _dispatch_ready(
        self, decision: work_next.Decision, report: PassReport, room: int, role: str
    ) -> None:
        """Take up to *room* ready beads through the atomic claim verb, then spawn a seat each.

        The decision table said "there is dispatchable room"; `bh work next` says WHICH bead this
        loop actually holds. Those are deliberately different questions — re-deriving the
        pick-then-claim race here is exactly what an unattended loop must not do — so the beads
        named on the decision bound the COUNT, not the identity.

        The IDENTITY is bounded too, but one layer down: `self._claim` passes `--epic self.epic`,
        so the claim verb's candidate set is this molecule (bh-sh6yt). Before that flag existed
        this method's "count, not identity" split was a hive-wide claim wearing a molecule's name
        — a loop pointed at a two-bead epic took live seats for beads in other molecules. Read
        the two sentences above as "the decision does not pick the bead", NOT as "any bead will
        do".
        """
        # Qualified launch validation is launch-owner work and must finish before the atomic
        # claim below.  A bad/missing BAML artifact can therefore never strand a claimed bead.
        if self._launch_context is not None:
            self._launch_context(role)
        wanted = min(room, len(decision.beads))
        for _ in range(wanted):
            verdict = self._admit(
                self.caps,
                in_flight=len(self.in_flight),
                lease_held=True,
                halted=self.halted,
            )
            if not verdict.allowed:
                report.denied += (verdict,)
                _LOG.info("dispatch_denied", reason=verdict.reason, detail=verdict.detail)
                return
            claimed = self._claim()
            if not claimed.claimed:
                # A decline is not a cap denial: nothing was refused, there was simply nothing
                # takeable right now (empty_queue / none_eligible / all_lost). Reported so the
                # pass is legible, then the loop backs off — retrying inside one pass would just
                # re-lose the same race.
                report.declined += (claimed.reason or "declined",)
                return
            await self._spawn_for(
                claimed.claimed,
                action=decision.action,
                role=role,
                report=report,
                workspace=claimed.worktree or None,
            )

    def _is_main_clone(self, ws: str) -> bool:
        """Whether *ws* IS the hive's main clone — i.e. the integration branch."""
        try:
            return Path(ws).resolve() == self.hive_dir.resolve()
        except OSError:  # pragma: no cover - a path that cannot be resolved is not the clone
            return False

    def _provision(self, bead: str) -> str:
        """Provision (or re-attach) *bead*'s managed worktree, returning "" if that is not possible.

        THE GAP THIS CLOSES (bh-4kq1b). :func:`resolve_workspace` is deliberately read-only, and
        only `dispatch`/`wrap_up` arrive with a workspace on the claim envelope. So `resume` — the
        one other DEVELOPER action — depends on the bead's worktree DIRECTORY already existing.
        It usually does (`submit` leaves it intact), but "usually" is not a guarantee: a second
        host that picked up the epic under `bh host lease` never provisioned it, and neither did a
        clone where the operator reclaimed `$BH_WORKTREES` or ran `bh work abandon --rm`. In every
        one of those the seat is LEGITIMATE and the refusal below still fired, so the pass minted
        a permanent `provisioning_failed` event and re-decided `resume-changes-requested` on the
        next tick — forever, because `work_next.attempt_count` counts changes-requested markers
        and those events carry none, so the loop-breaker never trips.

        Provisioning is confined to the SPAWN path on purpose: the resolver stays side-effect-free
        so a pass never provisions merely by deciding where to run. This runs only when the
        alternative is refusing a seat that has every right to run — and `worktree.ensure` is
        idempotent and is exactly what `bh work claim` / `next` / `resume` call.
        """
        from . import registry, worktree

        try:
            cfg = config.load()
            entry = registry.entry_for_dir(cfg, self.hive_dir)
            hive = registry.hive_key(entry) if entry else ""
            _entry, target, _branch = worktree.ensure(cfg, hive, bead)
        except Exception as exc:  # noqa: BLE001 - provisioning is best-effort; refusal is the backstop
            _LOG.warning("workspace_provision_failed", bead=bead, error=str(exc))
            return ""
        if Path(target).is_dir() and not self._is_main_clone(str(target)):
            _LOG.info("workspace_provisioned", bead=bead, ws=str(target))
            return str(target)
        return ""

    def _workspace_permitted(
        self, bead: str, ws: str, *, action: str, role: str, report: PassReport
    ) -> bool:
        """Refuse to spawn an Edit/Write seat in the MAIN CLONE.

        A `developer` seat commits. Handing it `hive_dir` points it at the integration branch —
        the one branch this whole tier exists to keep clean — and it would not even be a visible
        failure: the seat would work, commit, and land its work in the wrong place. `start` is
        the sole exception and it is a `dispatcher` seat, not a developer, precisely because it
        is the call that CREATES the container worktree the rest of the molecule runs in.
        """
        if role != "developer" or not self._is_main_clone(ws):
            return True
        reason = (
            f"refusing to spawn a developer seat for action {action!r} in the MAIN CLONE "
            f"({self.hive_dir}) — that is the integration branch, not a bead worktree. The "
            f"bead has no provisioned worktree; re-claim it (`bh work claim {bead}`) or let "
            f"`bh work next` provision one."
        )
        _LOG.error("developer_seat_in_main_clone_refused", bead=bead, action=action, ws=ws)
        record_cause(
            self.hive_dir, bead, CAUSE_PROVISIONING_FAILED, reason=reason, actor=self.actor
        )
        report.causes += ((bead, CAUSE_PROVISIONING_FAILED),)
        return False

    async def _spawn_for(
        self,
        bead: str,
        *,
        action: str,
        role: str,
        report: PassReport,
        workspace: str | None = None,
    ) -> None:
        """Spawn one seat for *bead*, unless one is already in flight for it.

        The in-flight guard is what makes "the loop never spawns two processes for one bead"
        true: it is a membership test on the map, checked before every spawn, and it is also why
        a restart cannot double-dispatch — a restarted loop has an empty map and re-derives the
        world from `bd`, where the bead is still `in_progress` (and so not ready) until
        `bd reclaim` frees it.
        """
        if bead in self.in_flight:
            return
        launch = self._launch_context(role) if self._launch_context is not None else None
        verdict = self._admit(
            self.caps, in_flight=len(self.in_flight), lease_held=True, halted=self.halted
        )
        if not verdict.allowed:
            report.denied += (verdict,)
            return
        routing = self._resolved_routing.get(bead)
        if routing is None:
            resolved = self._routing(bead, role)
            if isinstance(resolved, model_routing.ModelBlockedVerdict):
                payload = {"bead": bead, **resolved.as_dict()}
                report.routing += ((bead, payload),)
                reason = (
                    f"model routing blocked {bead}: {resolved.reason}; "
                    f"remediation: {resolved.remediation}"
                )
                _LOG.error("model_routing_blocked", **payload)
                record_cause(self.hive_dir, bead, CAUSE_BLOCKED, reason=reason, actor=self.actor)
                report.causes += ((bead, CAUSE_BLOCKED),)
                return
            routing = resolved
            if isinstance(routing, model_routing.ModelSelection):
                self._resolved_routing[bead] = routing
        translated_model = (
            model_routing.translate_for_harness(routing.selected_model, routing.harness)
            if isinstance(routing, model_routing.ModelSelection)
            else None
        )
        routing_payload = None
        if isinstance(routing, model_routing.ModelSelection):
            routing_payload = {
                "bead": bead,
                **routing.as_dict(),
            }
            report.routing += ((bead, routing_payload),)
            for warning in routing.warnings:
                _LOG.warning("model_routing_fallback", bead=bead, warning=warning)
        seat_process_id = f"seat-{uuid.uuid4()}"
        provider_continuation = f"provider-{uuid.uuid4()}"
        ws = workspace or self._workspace_for(bead)
        # A developer seat pointed at the main clone means "this bead has no worktree here yet",
        # not "this bead may not run" — provision one rather than dead-end (bh-4kq1b). The
        # refusal below stays the backstop for when even that fails.
        if role == "developer" and self._is_main_clone(ws):
            ws = self._provision(bead) or ws
        if not self._workspace_permitted(bead, ws, action=action, role=role, report=report):
            return
        validation = seatrun.validate_workspace(ws)
        if not validation.ok:
            record_cause(
                self.hive_dir,
                bead,
                CAUSE_FAILED,
                reason=f"workspace invalid before spawn: {validation.reason}",
                actor=self.actor,
            )
            report.causes += ((bead, CAUSE_FAILED),)
            return
        argv = seat_argv(
            launch.command if launch is not None else self.seat_command,
            role,
            workspace=ws,
            bead=bead,
            instructions=self._instructions(action, bead, role),
            session_id=provider_continuation,
            model=translated_model,
            bundle=launch.bundle if launch is not None else self.seat_bundle,
        )
        attrs = self._routing_attributes(routing) if routing else None
        journal = None
        if launch is not None:
            journal = run_journal.RunJournal.create(
                launch.run_identity(bead), base=self._journal_base
            )
        elif self._run_identity is not None:
            identity = self._run_identity(bead, action, role, routing)
            if identity is not None:
                # Mint + run.created happen before create_subprocess_exec.  Calling this method
                # again after retry creates a new object and therefore a new outer run_id.
                journal = run_journal.RunJournal.create(identity, base=self._journal_base)
        with otel.record_agent_dispatch(
            agent=role,
            model=routing.selected_model if routing else "",
            system=routing.harness if routing else "",
            attributes=attrs,
        ):
            seat = await spawn_seat(
                argv,
                bead_id=bead,
                role=role,
                action=action,
                session_id=seat_process_id,
                provider_continuation=provider_continuation,
                cwd=ws,
                env=self.env,
                task_group=self._tg,
                routing=routing_payload,
                journal=journal,
            )
        self.in_flight[bead] = seat
        report.dispatched += (bead,)

    # ---- sibling notification ------------------------------------------------------------

    async def notify_siblings(self, bead: str, message: str) -> tuple[str, ...]:
        """Tell the molecule's OTHER in-flight seats that something happened to *bead*.

        This is the loop's job and cannot be the child's (bh-a7so.7 §14): the child has no
        topology — the contract hands it one bead and one worktree by design — and no outbound
        channel to a sibling, since stream-json input is inbound only and its stdout goes to
        whoever spawned it. The loop already holds the in-flight map and already knows the
        molecule, so it writes to their stdin pipes directly.
        """
        notified = []
        for other, seat in self.in_flight.items():
            if other == bead or seat.finished:
                continue
            if seat.write_stdin(message):
                notified.append(other)
        if notified:
            _LOG.info("siblings_notified", about=bead, notified=notified, message=message)
        return tuple(notified)

    # ---- claim release ---------------------------------------------------------------------

    def _release(self, bead: str) -> None:
        """Release the claim on *bead* NOW rather than waiting out the 5-minute lease TTL.

        `bd reclaim` is the BACKSTOP for a holder that died, not the cancellation path: a run
        this loop stopped on purpose should be re-dispatchable on the very next pass. Note the
        failure mode the group kill exists to prevent — under a direct-child-only kill the
        "dead" holder is still alive and still committing while its lease ages out.
        """
        res = bd_mod.run(
            ["update", bead, "--status", "open", "--assignee", ""],
            self.hive_dir,
            actor=self.actor,
            capture=True,
        )
        if res.returncode != 0:
            _LOG.warning("claim_release_failed", bead=bead, error=bd_mod.err_line(res))


# --------------------------------------------------------------------------------------------
# The Runtime protocol implementation
# --------------------------------------------------------------------------------------------


class LocalRuntime:
    """`work.runtime: local` as :class:`beadhive.runtime.Runtime` — schedule / observe /
    on_gate_resolved over the same supervised-process machinery :class:`LocalLoop` uses.

    The protocol is SYNCHRONOUS and poll-shaped (``observe`` may legitimately answer
    ``running``), while the supervision discipline this tier is built on is asyncio. Rather than
    grow a second spawn path — which is how the `start_new_session` / `killpg` discipline would
    eventually drift out of one of them — this class owns a private event loop on a daemon
    thread and marshals onto it. One spawn path, one reaper, two calling conventions.

    The thread starts lazily on the first :meth:`schedule` so merely *resolving* the runtime
    (``get_runtime()``) costs nothing.
    """

    name = "local"

    def __init__(
        self,
        *,
        seat_command: str = "bh-{role}",
        seat_bundle: str = "",
        harness: str = "claude",
        terminate_grace: float = 5.0,
        envelope_grace: float = 3.0,
        run_identity: Callable[..., run_journal.RunIdentity | None] | None = None,
        journal_base: Path | None = None,
    ):
        self.seat_command = seat_command
        self.seat_bundle = seat_bundle
        self.harness = harness
        self.terminate_grace = terminate_grace
        self.envelope_grace = envelope_grace
        self._run_identity = run_identity
        self._journal_base = journal_base
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = None
        self._runs: dict[str, SeatProcess] = {}

    # ---- the private loop thread -----------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        import threading

        if self._loop is not None:
            return self._loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="bh-local-runtime", daemon=True)
        thread.start()
        self._loop, self._thread = loop, thread
        return loop

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result()

    async def _shutdown_runs(self) -> None:
        """Reap every process before the private loop that owns its pipes is stopped."""
        failures: list[str] = []
        for seat in tuple(self._runs.values()):
            reap = None
            try:
                if seat.finished:
                    await _collect_with_timeout(seat, self.envelope_grace)
                    reap = await reap_group(seat, grace=self.terminate_grace)
                else:
                    result = await cancel(
                        seat,
                        rungs=(RUNG_SIGNAL,),
                        cooperative_grace=0.0,
                        hard_grace=0.0,
                        envelope_grace=self.envelope_grace,
                        terminate_grace=self.terminate_grace,
                    )
                    reap = result.reap
            except Exception as exc:
                failures.append(f"{seat.bead_id}: cleanup failed: {exc}")
                try:
                    reap = await reap_group(seat, grace=self.terminate_grace)
                except Exception as reap_exc:
                    failures.append(f"{seat.bead_id}: fallback reap failed: {reap_exc}")
            if reap is not None and not reap.group_gone:
                failures.append(f"{seat.bead_id}: process group {seat.pgid} survived cleanup")
        self._runs.clear()
        if failures:
            raise RuntimeError("; ".join(failures))

    def _shutdown_timeout(self) -> float:
        # cancel may consume three envelope windows and reap_group has separate SIGTERM and
        # SIGKILL grace windows. If ordinary cleanup raises late, its fallback reap can consume
        # both termination windows again.
        per_seat = 3 * max(self.envelope_grace, 0.0) + 4 * max(self.terminate_grace, 0.0) + 1.0
        return max(per_seat * max(len(self._runs), 1), 1.0)

    async def _fallback_reap_runs(self) -> list[str]:
        failures: list[str] = []
        for seat in tuple(self._runs.values()):
            try:
                reap = await reap_group(seat, grace=self.terminate_grace)
            except BaseException as exc:
                failures.append(f"{seat.bead_id}: last-resort reap failed: {exc}")
                continue
            if not reap.group_gone:
                failures.append(
                    f"{seat.bead_id}: process group {seat.pgid} survived last-resort reap"
                )
        self._runs.clear()
        return failures

    def close(self) -> None:
        """Stop owned processes and join the private event-loop thread.

        A daemon thread is not lifecycle management: leaving these loops alive makes later
        subprocess launches fork from a multithreaded process on platforms where Python cannot
        use ``posix_spawn`` for that invocation. ``close`` is explicit and idempotent so callers
        and tests can make the process boundary deterministic.
        """
        loop, thread = self._loop, self._thread
        if loop is None:
            return
        failure: BaseException | None = None
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown_runs(), loop)
            try:
                future.result(timeout=self._shutdown_timeout())
            except concurrent.futures.TimeoutError:
                future.cancel()
                try:
                    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=1.0)
                except (concurrent.futures.TimeoutError, RuntimeError):
                    pass
                fallback = asyncio.run_coroutine_threadsafe(self._fallback_reap_runs(), loop)
                fallback_timeout = max(
                    (2 * max(self.terminate_grace, 0.0) + 1.0) * max(len(self._runs), 1),
                    1.0,
                )
                try:
                    fallback_failures = fallback.result(timeout=fallback_timeout)
                except concurrent.futures.TimeoutError:
                    fallback.cancel()
                    fallback_failures = ["last-resort process-group reap timed out"]
                detail = f"; {'; '.join(fallback_failures)}" if fallback_failures else ""
                failure = RuntimeError(f"local runtime process cleanup timed out{detail}")
            except BaseException as exc:
                failure = exc
        finally:
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=max(self.terminate_grace + self.envelope_grace, 1.0))
            if thread is not None and thread.is_alive():
                failure = RuntimeError("local runtime event loop did not stop")
            else:
                try:
                    loop.close()
                except BaseException as exc:
                    failure = failure or exc
                self._loop = None
                self._thread = None
        if failure is not None:
            raise RuntimeError(f"local runtime close failed: {failure}") from failure

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    # ---- Runtime ---------------------------------------------------------------------------

    def schedule(
        self,
        bead_id: str,
        role: str,
        *,
        workspace,
        instructions,
        session_id: str,
        model: str | None = None,
        decision: model_routing.ModelDecision | None = None,
    ):
        """Spawn the role binary for *bead_id* and return a handle to observe it by.

        Idempotent on an already-scheduled bead: a second call for a still-running bead returns
        the SAME handle rather than a second process. "The loop never spawns two processes for
        one bead" is a property of the map, so it has to hold on this path too — a caller
        driving the protocol directly gets the same guarantee the poll loop gets.
        """
        from .runtime import RoleHandle

        live = self._runs.get(bead_id)
        if live is not None and not live.finished:
            return RoleHandle(bead_id=bead_id, session_id=live.provider_continuation)

        if isinstance(decision, model_routing.ModelBlockedVerdict):
            raise ValueError(
                f"cannot schedule {bead_id}: {decision.reason}; required "
                f"{decision.required_tier.name}, preferred {decision.preferred_model or '(none)'}, "
                f"availability {decision.availability_source}; {decision.remediation}"
            )
        canonical_model = decision.selected_model if decision else model
        harness = decision.harness if decision else self.harness
        translated_model = (
            model_routing.translate_for_harness(canonical_model, harness)
            if canonical_model
            else None
        )
        routing_payload = None
        if isinstance(decision, model_routing.ModelSelection):
            routing_payload = {
                "bead": bead_id,
                **decision.as_dict(),
            }

        validation = seatrun.validate_workspace(str(workspace))
        if not validation.ok:
            raise ValueError(f"cannot schedule {bead_id}: {validation.reason}")
        provider_continuation = session_id
        seat_process_id = f"seat-{uuid.uuid4()}"
        argv = seat_argv(
            self.seat_command,
            role,
            workspace=str(workspace),
            bead=bead_id,
            instructions=str(instructions),
            session_id=provider_continuation,
            model=translated_model,
            bundle=self.seat_bundle,
        )
        attributes = LocalLoop._routing_attributes(decision) if decision else None
        journal = None
        if self._run_identity is not None:
            identity = self._run_identity(bead_id, "schedule", role, decision)
            if identity is not None:
                journal = run_journal.RunJournal.create(identity, base=self._journal_base)
        with otel.record_agent_dispatch(
            agent=role,
            model=canonical_model or "",
            system=harness,
            attributes=attributes,
        ):
            seat = self._submit(
                spawn_seat(
                    argv,
                    bead_id=bead_id,
                    role=role,
                    action="schedule",
                    session_id=seat_process_id,
                    provider_continuation=provider_continuation,
                    cwd=str(workspace),
                    routing=routing_payload,
                    journal=journal,
                )
            )
        self._runs[bead_id] = seat
        return RoleHandle(bead_id=bead_id, session_id=provider_continuation)

    def observe(self, handle):
        """A status READ, never a blocking wait. ``running`` while the process is alive; once it
        exits, the verdict comes from :func:`beadhive.seatrun.classify_run` — stdout-first, never
        trusting a bare exit 0 — and the group is reaped so a finished run can never leave an
        orphan behind."""
        from .runtime import RoleOutcome

        seat = self._runs.get(handle.bead_id)
        if seat is None:
            return RoleOutcome(status="failed", summary=f"no run scheduled for {handle.bead_id}")
        if not seat.finished:
            return RoleOutcome(
                status="running", summary=f"pid {seat.pid}, pgid {seat.pgid}", routing=seat.routing
            )
        stdout = self._submit(_collect_with_timeout(seat, self.envelope_grace))
        self._submit(reap_group(seat, grace=self.terminate_grace))
        cls = seatrun.classify_run(seat.proc.returncode or 0, stdout, bead=handle.bead_id)
        if cls.outcome is seatrun.RunOutcome.INCOMPLETE:
            return RoleOutcome(status="failed", summary=cls.detail, routing=seat.routing)
        summary = cls.seat_run.outcome.summary if cls.seat_run else ""
        return RoleOutcome(status=str(cls.outcome), summary=summary, routing=seat.routing)

    def on_gate_resolved(self, gate_id: str) -> None:
        """A no-op with a reason, not an oversight. This tier notices a resolved gate on its next
        poll regardless — `bd gate check` runs at the head of every pass — and the ADR rejects a
        push doorbell outright (Decision 1's rejected alternative: `bd gate` is already a durable
        addressable wait, and a broker would only reduce latency). Gate latency bounded by
        `work.dispatch.poll_interval` is the documented trade (Limitation 1)."""
        _LOG.debug("gate_resolved_noop", gate=gate_id, runtime=self.name)

    def cancel(self, bead_id: str) -> CancelResult | None:
        """Walk the CANCEL ladder against a scheduled run. Not part of the `Runtime` protocol —
        the protocol has no cancel verb — but the capability belongs to whatever holds the pipes,
        so it is exposed here rather than left reachable only through :class:`LocalLoop`."""
        seat = self._runs.get(bead_id)
        if seat is None:
            return None
        return self._submit(
            cancel(
                seat,
                envelope_grace=self.envelope_grace,
                terminate_grace=self.terminate_grace,
            )
        )


def runtime_from_config(cfg=None, entry=None) -> LocalRuntime:
    """A :class:`LocalRuntime` wired from `work.dispatch.*`. The factory `runtime.get_runtime`
    calls for `work.runtime: local`.

    A `None` cfg becomes `{}` rather than triggering a `config.load()` deep inside the accessor
    chain: `get_runtime` already resolved (or failed to resolve) config once, and re-entering
    the loader from here would turn "no config yet" — a perfectly ordinary pre-`bh config init`
    state that must still yield the default tier — into a `FileNotFoundError`.
    """
    cfg = {} if cfg is None else cfg
    return LocalRuntime(
        seat_command=config.dispatch_seat_command(cfg, entry),
        seat_bundle=config.dispatch_seat_bundle(cfg, entry),
        harness=config.harness_name(cfg, entry),
        terminate_grace=config.dispatch_terminate_grace(cfg, entry),
        envelope_grace=config.dispatch_envelope_grace(cfg, entry),
    )
