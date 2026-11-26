"""The loop contract, end to end on a REAL scratch bd store (bh-lxv3v, epic bh-vjzki).

Proves that ``bh work loop``'s driver drives a REGISTERED, non-child runtime — Mode 1 of
docs/design/gas-city-runtime-seam-adr.md — rather than spawning seats itself:

- the runtime is a Gas-City-shaped stand-in, ``FakeSessionRuntime``, registered through an
  injected ``beadhive.runtimes`` entry point and resolved the way ``bh work loop`` resolves it
  (``config.load`` of a real config file selecting it per hive, then ``get_runtime(cfg, entry)``);
- ``schedule()`` starts a DETACHED session — ``bh role <role> --outcome-sink bead --session-id``
  in its own process group, held by a session host that outlives every runtime instance and every
  loop — and ``observe()`` is the outcome-sink reader, ``outcome.observe``;
- ``localloop.spawn_seat``, ``asyncio.create_subprocess_exec`` and the orphan scan raise for the
  whole module (the direct B1 guard: the loop has no spawn path of its own left to fall back on).

Time: the loop's monotonic and wall clocks are a ``FakeClock``; a seat holds its turn on a stdin
readline, released by the test, and a turn is complete when its ``bh role`` process has exited
(a blocking ``wait``). Nothing sleeps. Leases are real bd leases: lease expiry is produced by
backdating ``lease_expires_at`` through ``bd sql`` (server-mode stores only), and ``bd reclaim``
still makes the call.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shlex
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from beadhive import (
    coordination,
    localloop,
    outcome,
    runtime,
    runtime_handle,
    work_dispatch,
)
from beadhive.config_consumer_ports import work_settings as config
from beadhive.gate_resolution import GateResolution
from beadhive.modules.config.contracts import SCHEMA_VERSION
from beadhive.modules.config.domain import runtime_names
from beadhive.runtime import RoleHandle
from harness.beads import skip_if_no_bd
from harness.clock import FakeClock
from harness.world import reap_dolt_server

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server, skip_if_no_bd]

RUNTIME_NAME = "gascity-standin"
#: The loop's actor; it also takes the claims, as `bh work next` does for the loop.
ACTOR = "disp/loop-contract"
#: The identity `bh role --outcome-sink bead` writes the seat's record as.
SEAT_ACTOR = "dev/gc-seat"
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"
SEAT = FIXTURES / "loop_contract_seat.py"
DRIVER = FIXTURES / "role_sink_driver.py"
_BD_TIMEOUT = 120
_SESSION_TIMEOUT = 180
_SHARED_SERVER_ENV = (
    "BEADS_DOLT_SHARED_SERVER",
    "BEADS_SHARED_SERVER_DIR",
    "BEADS_DOLT_SERVER_PORT",
)


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ---- the scratch store --------------------------------------------------------------------------


class Store:
    """A real `bd init --server` store under a test's tmp dir (server mode: `bd sql` exists)."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["bd", "init", "--server", "--prefix", "lc", "--non-interactive"],
            cwd=str(path),
            check=True,
            capture_output=True,
            text=True,
            timeout=_BD_TIMEOUT,
        )

    def bd(self, *args: str, actor: str = ACTOR) -> str:
        done = subprocess.run(
            ["bd", "-C", str(self.path), "--actor", actor, *args],
            capture_output=True,
            text=True,
            timeout=_BD_TIMEOUT,
        )
        assert done.returncode == 0, f"bd {' '.join(args)} -> {done.returncode}: {done.stderr}"
        return done.stdout

    def json(self, *args: str):
        return json.loads(self.bd(*args, "--json") or "null")

    def create(self, title: str, kind: str = "task", *, parent: str = "") -> str:
        extra = ["--parent", parent] if parent else []
        return str(self.json("create", title, "-t", kind, *extra)["id"])

    def molecule(self, *titles: str) -> tuple[str, list[str]]:
        epic = self.create("loop contract", "epic")
        children = [self.create(title, parent=epic) for title in titles]
        self.bd("update", epic, "--status", "in_progress")
        return epic, children

    def show(self, bead: str) -> dict:
        data = self.json("show", bead)
        return data[0] if isinstance(data, list) else data

    def labels(self, bead: str) -> set[str]:
        return set(self.show(bead).get("labels") or ())

    def dispatch_events(self, bead: str) -> list[dict]:
        rows = self.json("list", "--parent", bead, "--include-infra", "--all", "--limit", "0")
        return [
            r
            for r in rows or ()
            if r.get("issue_type") == "event" and "dispatch" in str(r.get("title") or "")
        ]

    def backdate_lease(self, bead: str, when: datetime) -> None:
        stamp = when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        self.bd("sql", f"UPDATE leases SET lease_expires_at = '{stamp}' WHERE issue_id = '{bead}'")


@pytest.fixture
def store(tmp_path, monkeypatch):
    # The loop's own bd calls inherit this process's environment: without the sandbox's
    # shared-server endpoint, they reach this store's own server rather than another one.
    for name in _SHARED_SERVER_ENV:
        monkeypatch.delenv(name, raising=False)
    scratch = Store(tmp_path / "hive")
    yield scratch
    reap_dolt_server(scratch.path / ".beads")


# ---- the Gas-City-shaped runtime ----------------------------------------------------------------


class SessionHost:
    """The process host, as far as the loop can tell: each session is a ``bh role ...
    --outcome-sink bead`` process in its own process group, held HERE — not by the loop and not
    by any runtime instance — so it outlives both, exactly as a Gas City session does."""

    def __init__(self, root: Path, store: Store):
        self.store = store
        self.seat_log = root / "seat-log"
        self.err_dir = root / "session-err"
        self.err_dir.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self.runtimes: list[FakeSessionRuntime] = []

    def start(self, *, bead: str, role: str, workspace: str, session_id: str) -> None:
        argv = [
            sys.executable,
            str(DRIVER),
            "--hive",
            str(self.store.path),
            "--workspace",
            workspace,
            "--seat-actor",
            SEAT_ACTOR,
            "--",
            "role",
            role,
            "--bead",
            bead,
            "--outcome-sink",
            "bead",
            "--session-id",
            session_id,
        ]
        with (self.err_dir / f"{session_id}.err").open("wb") as err:
            self.procs[session_id] = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,  # a fire-and-forget host keeps no stdout
                stderr=err,
                start_new_session=True,
            )

    def finish(self, session_id: str, status: str, summary: str = "") -> None:
        """Release the session's turn and wait for ``bh role`` to have written its record."""
        proc = self.procs[session_id]
        assert proc.stdin is not None
        proc.stdin.write(f"{status} {summary or status + ' turn'}\n".encode())
        proc.stdin.close()
        code = proc.wait(timeout=_SESSION_TIMEOUT)
        assert code == 0, (self.err_dir / f"{session_id}.err").read_text()

    def alive(self, session_id: str) -> bool:
        return self.procs[session_id].poll() is None

    def launched(self, session_id: str) -> dict:
        """What the seat itself was launched with (written before it took its turn)."""
        return json.loads((self.seat_log / f"{session_id}.json").read_text())

    def stop_all(self) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=_SESSION_TIMEOUT)


class FakeSessionRuntime:
    """A Gas-City-shaped :class:`beadhive.runtime.Runtime`. Sessions are not its children and
    outlive it (``adoptable``); ``observe()`` reads the outcome sink; it never writes a bead;
    ``close()`` releases the runtime and leaves every session running."""

    name = RUNTIME_NAME
    adoptable = True

    def __init__(self, host: SessionHost, cfg, entry):
        self.host = host
        self.cfg, self.entry = cfg, entry
        self.scheduled: list[dict] = []
        self.gates: list[tuple[GateResolution, int]] = []
        self.closed = 0
        host.runtimes.append(self)

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None):
        self.scheduled.append(
            {"bead": bead_id, "role": role, "workspace": workspace, "session_id": session_id}
        )
        self.host.start(bead=bead_id, role=role, workspace=workspace, session_id=session_id)
        return RoleHandle(bead_id=bead_id, session_id=session_id)

    def observe(self, handle):
        return outcome.observe(handle, cwd=self.host.store.path)

    def on_gate_resolved(self, resolution):
        # Recorded with how many schedules had happened: a re-dispatch must be the LOOP's.
        self.gates.append((resolution, len(self.scheduled)))

    def close(self):
        self.closed += 1


class _EntryPoint:
    """A stand-in `beadhive.runtimes` entry point (what a Gas City package would declare)."""

    def __init__(self, name, obj):
        self.name, self._obj = name, obj
        self.dist = type("Dist", (), {"name": "gascity-standin"})()
        self.loads = 0

    def load(self):
        self.loads += 1
        return self._obj


@pytest.fixture
def host(tmp_path, store):
    session_host = SessionHost(tmp_path, store)
    yield session_host
    session_host.stop_all()


@pytest.fixture
def contract_config(tmp_path, monkeypatch, host):
    """A real config file: this hive selects the stand-in runtime per hive (the host default
    stays `local`), reads beads over the CLI route, and points `bh role` at the stand-in seat."""
    seat_command = " ".join(
        shlex.quote(part)
        for part in (sys.executable, str(SEAT), "--log", str(host.seat_log), "--role")
    )
    path = tmp_path / "config.yaml"
    path.write_text(
        f"schema_version: {SCHEMA_VERSION}\n"
        "providers: [github]\n"
        "managed_repos:\n"
        "  - provider: github\n"
        "    org: loop-contract\n"
        "    repo: scratch\n"
        "    prefix: lc\n"
        "    kind: external\n"
        "    work:\n"
        f"      runtime: {RUNTIME_NAME}\n"
        "work:\n"
        "  beads:\n"
        "    route: api+cli-fallback\n"
        "  dispatch:\n"
        f"    seat_command: {json.dumps(seat_command + ' {role}')}\n"
    )
    monkeypatch.setenv("BH_CONFIG", str(path))
    cfg = config.load()
    return cfg, cfg["managed_repos"][0]


@pytest.fixture
def resolve(monkeypatch, host, contract_config):
    """Resolve the hive's runtime the way `bh work loop` does, from the entry point."""
    saved = dict(runtime_names.RUNTIMES)
    runtime_names.RUNTIMES.pop(RUNTIME_NAME, None)
    cfg, entry = contract_config
    ep = _EntryPoint(RUNTIME_NAME, lambda cfg, entry: FakeSessionRuntime(host, cfg, entry))

    def entry_points(group=None):
        return [ep] if group == runtime_names.ENTRY_POINT_GROUP else []

    def get():
        return runtime.get_runtime(cfg, entry, entry_points=entry_points)

    get.entry_point = ep
    yield get
    runtime_names.RUNTIMES.clear()
    runtime_names.RUNTIMES.update(saved)


@pytest.fixture(autouse=True)
def no_direct_spawn(monkeypatch):
    """The direct B1 guard: any seat spawn or orphan scan of the loop's own fails the test."""

    def refuse(what):
        def fail(*_a, **_kw):
            raise AssertionError(f"the loop reached {what}: it must drive the runtime instead")

        return fail

    monkeypatch.setattr(localloop, "spawn_seat", refuse("localloop.spawn_seat"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse("create_subprocess_exec"))
    monkeypatch.setattr(localloop, "find_orphan_seats", refuse("the orphan scan"))


def _ws(tmp_path: Path, bead: str) -> str:
    path = tmp_path / "worktrees" / bead
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _claimer(store: Store, tmp_path: Path, beads: list[str]):
    """`bh work next`'s part, against the real store: claim the next open bead of the list."""

    def claim() -> localloop.ClaimResult:
        for bead in beads:
            if store.show(bead).get("status") == "open":
                store.bd("update", bead, "--claim")
                return localloop.ClaimResult(claimed=bead, worktree=_ws(tmp_path, bead))
        return localloop.ClaimResult(reason="empty_queue")

    return claim


def _loop(tmp_path, store, rt, epic, beads, *, clock=None, max_concurrency=3):
    clock = clock or FakeClock(T0)
    briefs = tmp_path / "briefs"
    briefs.mkdir(exist_ok=True)

    def instructions(action, bead, role):
        path = briefs / f"{bead}.{action}.md"
        path.write_text(f"{role} {action} {bead}\n")
        return str(path)

    return localloop.LocalLoop(
        hive_dir=store.path,
        epic=epic,
        actor=ACTOR,
        caps=localloop.Caps(max_concurrency=max_concurrency),
        runtime=rt,
        on_gate_resolved=rt.on_gate_resolved,
        claim=_claimer(store, tmp_path, beads),
        workspace_for=lambda bead: _ws(tmp_path, bead),
        routing=lambda _bead, _role: None,
        instructions=instructions,
        clock=clock.monotonic,
        wall_clock=clock.now,
    )


def _session(loop, bead: str) -> str:
    return loop.in_flight[bead].handle.session_id


def _cause_label(cause: str) -> str:
    return f"{localloop.DISPATCH_DIMENSION}:{cause}"


def _review_gate(store: Store, bead: str, sha: str) -> str:
    """Open the review gate `bh work submit` opens (same marker), and mark the bead pending."""
    created = coordination.gate_create(
        store.path, blocks=bead, gate_type="human", reason=f"bh:review {sha}", actor=ACTOR
    )
    assert created.ok, created.error
    store.bd("set-state", bead, "review=pending", "--reason", f"submitted {sha}")
    return created.gate_id


def _resolve_review(store: Store, gate: str, bead: str, *, verdict: str) -> None:
    """What `bh work bounce` / `approve` write at the bd level: the gate's close reason, then the
    bead's review state."""
    reviewer = "rev/loop-contract"
    if verdict == "changes_requested":
        reason, state = f"changes requested by {reviewer}: tighten the tests", "changes-requested"
    else:
        reason, state = f"approved by {reviewer}", "approved"
    store.bd("gate", "resolve", gate, "--reason", reason, actor=reviewer)
    store.bd("set-state", bead, f"review={state}", "--reason", reason, actor=reviewer)


# ---- resolution: the registered runtime IS the loop's runtime -----------------------------------


def test_the_hive_resolves_the_entry_point_runtime_the_loop_will_drive(resolve, contract_config):
    cfg, entry = contract_config
    assert config.work_runtime(cfg, None) == "local", "the host default is untouched"
    assert config.work_runtime(cfg, entry) == RUNTIME_NAME

    rt = resolve()

    assert isinstance(rt, FakeSessionRuntime) and rt.entry is entry
    assert resolve.entry_point.loads == 1
    # `bh work loop`'s own resolution now finds the same registered name and accepts it.
    looped = work_dispatch.loop_runtime(cfg, entry)
    assert isinstance(looped, FakeSessionRuntime) and isinstance(looped, runtime.Runtime)
    assert runtime.is_adoptable(looped)


# ---- the contract walk --------------------------------------------------------------------------


@async_test
async def test_the_loop_drives_the_registered_runtime_through_a_whole_turn(
    tmp_path, store, host, resolve
):
    epic, beads = store.molecule("ships", "needs a human", "hands off")
    done, blocked, handoff = beads
    rt = resolve()
    loop = _loop(tmp_path, store, rt, epic, beads)

    # 1. ONE schedule() per seat, with the settled role, bead, session and workspace.
    first = await loop.run_pass()
    assert first.dispatched == (done, blocked, handoff)
    sessions = {bead: _session(loop, bead) for bead in beads}
    assert len(set(sessions.values())) == 3
    assert rt.scheduled == [
        {"bead": b, "role": "developer", "workspace": _ws(tmp_path, b), "session_id": sessions[b]}
        for b in beads
    ]
    for bead in beads:
        row = store.show(bead)
        assert (row["status"], row["assignee"]) == ("in_progress", ACTOR)
        # bd show returns the handle the loop wrote in the shape runtime_handle.parse expects,
        # and the claim's lease in the shape lease_expired reads.
        persisted = runtime_handle.from_row(row)
        assert persisted is not None and persisted.handle == RoleHandle(bead, sessions[bead])
        assert (persisted.runtime, persisted.role, persisted.action) == (
            RUNTIME_NAME,
            "developer",
            "dispatch",
        )
        assert persisted.scheduled_at == T0
        assert "lease_expires_at" in row
        assert not runtime_handle.lease_expired(row, datetime.now(UTC))

    # 2. While observe() answers running: in flight, heartbeated, and bd reclaim leaves it.
    second = await loop.run_pass()
    assert second.harvested == () and second.dispatched == ()
    assert set(second.in_flight) == set(beads)
    assert set(second.heartbeats) == set(beads)
    assert second.reclaimed == ()
    assert all(store.show(b)["assignee"] == ACTOR for b in beads)
    assert len(rt.scheduled) == 3, "no second schedule() for a running session"
    assert all(host.alive(sessions[b]) for b in beads)

    # The seat really ran under the loop's argv: its own role, bead, session and workspace.
    host.finish(sessions[done], "done", "shipped it")
    host.finish(sessions[blocked], "blocked", "needs a decision")
    host.finish(sessions[handoff], "handoff", "over to review")
    for bead in beads:
        launched = host.launched(sessions[bead])
        assert launched == {
            "role": "developer",
            "bead": bead,
            "workspace": _ws(tmp_path, bead),
            "session_id": sessions[bead],
            "cwd": _ws(tmp_path, bead),
        }

    # 3. Harvest writes the right cause: none for done, blocked and handoff for the others.
    third = await loop.run_pass()
    assert set(third.harvested) == {(done, "done"), (blocked, "blocked"), (handoff, "handoff")}
    assert set(third.causes) == {
        (blocked, localloop.CAUSE_BLOCKED),
        (handoff, localloop.CAUSE_HANDOFF),
    }
    assert _cause_label(localloop.CAUSE_BLOCKED) in store.labels(blocked)
    assert _cause_label(localloop.CAUSE_HANDOFF) in store.labels(handoff)
    assert not any(label.startswith("dispatch:") for label in store.labels(done))
    (event,) = store.dispatch_events(blocked)
    assert f"session {sessions[blocked]}" in event["description"]
    assert all(runtime_handle.from_row(store.show(b)) is None for b in beads)
    assert rt.gates == []

    # 4. The done bead is submitted and bounced. The resolved gate reaches on_gate_resolved,
    #    and the LOOP — not the runtime — schedules the developer again, with a fresh session.
    gate = _review_gate(store, done, "abc1234")
    _resolve_review(store, gate, done, verdict="changes_requested")
    fourth = await loop.run_pass()
    assert fourth.gate_notified == 1
    assert rt.gates == [(GateResolution(gate, done, "changes_requested", feedback_ref=gate), 3)], (
        "delivered once, before any re-dispatch"
    )
    assert fourth.decision.row == "resume-changes-requested"
    assert fourth.dispatched == (done,)
    resumed = _session(loop, done)
    assert resumed not in sessions.values()
    assert rt.scheduled[3:] == [
        {"bead": done, "role": "developer", "workspace": _ws(tmp_path, done), "session_id": resumed}
    ]
    assert runtime_handle.from_row(store.show(done)).action == "resume"

    # 5. A stale session record does not answer: the first turn's done record is still on the
    #    bead, and the fresh session stays running. The gate is not delivered twice.
    assert outcome.metadata_key(sessions[done]) in store.show(done)["metadata"]
    assert outcome.observe(RoleHandle(done, resumed), cwd=store.path).status == "running"
    fifth = await loop.run_pass()
    assert fifth.harvested == () and done in fifth.in_flight
    assert fifth.gate_notified == 0 and len(rt.gates) == 1
    assert len(rt.scheduled) == 4

    # 6. The resumed seat resubmits and finishes; the loop's review row schedules the reviewer.
    second_gate = _review_gate(store, done, "def5678")
    host.finish(resumed, "done", "tests tightened")
    review = await loop.run_pass()
    assert review.harvested == ((done, "done"),)
    assert review.decision.row == "review"
    assert [s["role"] for s in rt.scheduled[4:]] == ["reviewer"]
    reviewer = rt.scheduled[4]["session_id"]

    # 7. The reviewer approves: that gate is delivered once too, and the merge row schedules the
    #    merger. Every seat had its own session.
    host.finish(reviewer, "done", "looks good")
    _resolve_review(store, second_gate, done, verdict="approved")
    merge = await loop.run_pass()
    assert merge.harvested == ((done, "done"),)
    assert merge.gate_notified == 1
    assert rt.gates[1] == (GateResolution(second_gate, done, "approved"), 5)
    assert merge.decision.row == "merge-exactly-one"
    assert [(s["role"], s["bead"]) for s in rt.scheduled[5:]] == [("merger", done)]
    assert len({s["session_id"] for s in rt.scheduled}) == len(rt.scheduled) == 6
    assert (await loop.run_pass()).gate_notified == 0

    # A plain shutdown closes the runtime and leaves its live session alone (close() must not
    # end an adoptable runtime's sessions).
    merger_session = rt.scheduled[5]["session_id"]
    await loop.shutdown()
    assert rt.closed == 1
    assert host.alive(merger_session)
    assert store.show(done)["assignee"] == ACTOR, "no claim released"


# ---- restart adoption ---------------------------------------------------------------------------


@async_test
async def test_a_restarted_loop_adopts_the_live_session_and_harvests_the_finished_one(
    tmp_path, store, host, resolve
):
    epic, beads = store.molecule("still running", "finishes while no loop runs")
    live, finished = beads
    first_rt = resolve()
    first = _loop(tmp_path, store, first_rt, epic, beads)
    assert (await first.run_pass()).dispatched == (live, finished)
    sessions = {bead: _session(first, bead) for bead in beads}

    # A plain stop leaves the sessions running, their claims held and their handles on the bead.
    await first.shutdown()
    assert first_rt.closed == 1
    assert host.alive(sessions[live]) and host.alive(sessions[finished])
    for bead in beads:
        row = store.show(bead)
        assert (row["status"], row["assignee"]) == ("in_progress", ACTOR)
        assert runtime_handle.from_row(row).session_id == sessions[bead]
        assert store.dispatch_events(bead) == []

    host.finish(sessions[finished], "blocked", "needs a human")

    # A NEW runtime instance (as after a process restart), over the same session host.
    second_rt = resolve()
    assert second_rt is not first_rt
    clock = FakeClock(T0 + timedelta(minutes=2))
    second = _loop(tmp_path, store, second_rt, epic, beads, clock=clock)
    report = await second.run_pass()

    assert report.reclaimed == ()
    assert report.adopted == (live,)
    assert second_rt.scheduled == [], "adoption is not a second schedule()"
    assert second.in_flight[live].handle == RoleHandle(live, sessions[live])
    assert second.in_flight[live].age(clock.monotonic()) == 120.0, "aged from scheduled_at"
    assert live in report.heartbeats
    assert report.harvested == ((finished, "blocked"),)
    assert report.causes == ((finished, localloop.CAUSE_BLOCKED),)
    assert runtime_handle.from_row(store.show(finished)) is None
    assert len(store.dispatch_events(finished)) == 1

    # The harvest's handle removal "failed" (it is best-effort): put the handle back, as a
    # failed `bd update --unset-metadata` would have left it, and restart once more. The cause is
    # already on the bead, so it is not written a second time; only the handle is dropped.
    stale = runtime_handle.PersistedHandle(
        bead_id=finished,
        runtime=RUNTIME_NAME,
        session_id=sessions[finished],
        role="developer",
        action="dispatch",
        scheduled_at=T0,
    )
    runtime_handle.write(store.path, stale, actor=ACTOR)
    await second.shutdown()
    third_rt = resolve()
    third = _loop(tmp_path, store, third_rt, epic, beads, clock=clock)
    again = await third.run_pass()
    assert again.adopted == (live,)
    assert again.harvested == () and again.causes == ()
    assert len(store.dispatch_events(finished)) == 1, "no duplicate permanent cause event"
    assert runtime_handle.from_row(store.show(finished)) is None

    # The adopted session finishes and is harvested like any other, with no second schedule().
    host.finish(sessions[live], "done")
    assert (await third.run_pass()).harvested == ((live, "done"),)
    assert third_rt.scheduled == []
    assert runtime_handle.from_row(store.show(live)) is None
    await third.shutdown()


# ---- lease expiry wins --------------------------------------------------------------------------


@async_test
async def test_lease_expiry_wins_over_a_persisted_handle_on_real_bd(tmp_path, store, host, resolve):
    epic, (bead,) = store.molecule("its loop died long ago")
    first_rt = resolve()
    first = _loop(tmp_path, store, first_rt, epic, [bead], max_concurrency=1)
    await first.run_pass()
    old = _session(first, bead)
    await first.shutdown()

    # Expired, but inside bd reclaim's default grace window (10m past expiry): real bd does NOT
    # revert it yet (the fake hive's reclaim models no grace window).
    now = datetime.now(UTC)
    store.backdate_lease(bead, now - timedelta(minutes=1))
    row = store.show(bead)
    assert runtime_handle.lease_expired(row, now)
    assert runtime_handle.reclaim_refusal(row, now=now) is None, "an expired lease frees it"
    assert coordination.reclaim(store.path, actor=ACTOR).reclaimed_ids == ()
    assert store.show(bead)["status"] == "in_progress"

    # Past the grace window: the restarted loop's own reclaim reverts it BEFORE adoption runs.
    store.backdate_lease(bead, now - timedelta(hours=2))
    second_rt = resolve()
    second = _loop(tmp_path, store, second_rt, epic, [bead], max_concurrency=1)
    report = await second.run_pass()

    assert report.reclaimed == (bead,)
    assert report.adopted == ()
    assert report.dispatched == (bead,), "re-dispatched as ordinary ready work"
    fresh = _session(second, bead)
    assert fresh != old
    assert second_rt.scheduled == [
        {"bead": bead, "role": "developer", "workspace": _ws(tmp_path, bead), "session_id": fresh}
    ]
    assert runtime_handle.from_row(store.show(bead)).session_id == fresh

    # The old session's late record never answers for the new turn.
    host.finish(old, "done", "late")
    assert outcome.metadata_key(old) in store.show(bead)["metadata"]
    late = await second.run_pass()
    assert late.harvested == () and bead in late.in_flight

    host.finish(fresh, "done")
    assert (await second.run_pass()).harvested == ((bead, "done"),)
    await second.shutdown()
