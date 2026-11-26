"""The runtime conformance kit (bh-m4vd5): LocalRuntime passes it; deliberately misbehaving
runtimes fail with a message naming the violated ADR invariant and its fix class."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from beadhive.localloop import LocalRuntime
from beadhive.runtime import RoleHandle, RoleOutcome
from beadhive.runtime.conformance import (
    BH_SIDE,
    GC_FIXABLE,
    LOOP_CHECKS,
    STRUCTURAL,
    BdHive,
    ConformanceFailure,
    FileHive,
    RuntimeConformanceSuite,
    RuntimeEnv,
    run_conformance,
    verdict,
)
from beadhive.runtime.conformance.checks import (
    CHECKS,
    INVARIANTS,
    check_no_lifecycle_writes,
    check_observe_running_to_terminal,
    new_session_id,
)
from beadhive.runtime.conformance.loop import check_loop_drives_one_turn
from beadhive.runtime.conformance.seat import scrubbed_env
from beadhive.runtime.conformance.state import BeadState
from harness.clock import FakeClock

needs_bd = pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed")


@pytest.fixture(scope="module")
def bd_hive(tmp_path_factory):
    """One scratch bd store (server mode) shared by every real-bd test in this module."""
    hive = BdHive(tmp_path_factory.mktemp("bdhive"))
    yield hive
    hive.close()


def _env(hive, tmp_path, **kw) -> RuntimeEnv:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    instructions = tmp_path / "i.md"
    instructions.write_text("conformance turn\n", encoding="utf-8")
    return RuntimeEnv(hive, hive.seat_command(), workspace, instructions, timeout=10.0, **kw)


def _local(env: RuntimeEnv) -> LocalRuntime:
    return LocalRuntime(seat_command=env.seat_command, terminate_grace=1.0, envelope_grace=1.0)


class TestLocalRuntimeConformance(RuntimeConformanceSuite):
    """Hermetic: the JSON FileHive (no bd needed)."""

    def make_hive(self, root):
        return FileHive(root / "hive")

    def make_runtime(self, env):
        return _local(env)


@pytest.mark.integration
@needs_bd
class TestLocalRuntimeConformanceOnBd(RuntimeConformanceSuite):
    """The default hive: a real scratch beads store, where the seat records through bd. Opts in
    to the loop probe, as a third-party suite would."""

    close_hive = False
    loop_probe = True

    @pytest.fixture(autouse=True)
    def _shared(self, bd_hive):
        self._hive = bd_hive

    def make_hive(self, root):
        return self._hive

    def make_runtime(self, env):
        return _local(env)


class _FakeRuntime:
    """A runtime with no processes: a turn is 'finished' once the hive releases it."""

    name = "fake"

    def __init__(self, env: RuntimeEnv):
        self.env = env
        self.sessions: dict[str, str] = {}  # reported session id -> bead

    def _session(self, session_id: str) -> str:
        return session_id

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, **_kw):
        session = self._session(session_id)
        self.on_schedule(bead_id)
        self.sessions[session] = bead_id
        with open(self.env.hive.root / "starts" / f"{bead_id}.log", "a") as log:  # type: ignore[attr-defined]
            log.write(session + "\n")
        return RoleHandle(bead_id=bead_id, session_id=session)

    def on_schedule(self, bead_id):
        pass

    def observe(self, handle):
        if not (self.env.hive.root / "release" / handle.session_id).exists():  # type: ignore[attr-defined]
            return RoleOutcome(status="running")
        return RoleOutcome(status="done", summary="conformance turn")

    def on_gate_resolved(self, resolution):
        pass


class _LabelWriter(_FakeRuntime):
    def on_schedule(self, bead_id):
        self.env.hive.add_label(bead_id, "runtime-was-here")


class _IgnoresSessionId(_FakeRuntime):
    def _session(self, session_id: str) -> str:
        return "runtime-made-this-up"


class _StaleAnswerer(_FakeRuntime):
    """Answers a finished earlier session's outcome for the bead's next session."""

    finished: set = set()

    def observe(self, handle):
        released = (self.env.hive.root / "release" / handle.session_id).exists()
        if released or handle.bead_id in self.finished:
            self.finished.add(handle.bead_id)
            return RoleOutcome(status="done", summary="conformance turn")
        return RoleOutcome(status="running")


def _run(factory, tmp_path):
    return run_conformance(factory, hive=FileHive(tmp_path / "h"), timeout=10.0)


def _failures(factory, tmp_path) -> dict[str, str]:
    return {r.check: r.message for r in _run(factory, tmp_path) if not r.passed}


def test_well_behaved_fake_passes(tmp_path):
    assert _failures(_FakeRuntime, tmp_path) == {}


def test_a_runtime_without_the_optional_capabilities_is_still_a_conforming_runtime(tmp_path):
    """cancel / notify / close / adoptable are optional (bh-lhxy8): a third-party runtime that
    implements only the three protocol verbs is still a `Runtime` and still passes the kit."""
    from beadhive.runtime import Runtime, can_cancel, can_close, can_notify, is_adoptable

    runtime = _FakeRuntime(_env(FileHive(tmp_path / "probe"), tmp_path))
    assert isinstance(runtime, Runtime)
    assert not any(hasattr(runtime, attr) for attr in ("cancel", "notify", "close", "adoptable"))
    assert (can_cancel(runtime), can_notify(runtime), can_close(runtime)) == (False,) * 3
    assert is_adoptable(runtime) is False
    assert _failures(_FakeRuntime, tmp_path) == {}


def test_go_on_a_filehive_carries_the_caveat(tmp_path):
    text = verdict(_run(_FakeRuntime, tmp_path))
    assert text.startswith("GO (FileHive: bd-level lifecycle writes NOT exercised)")


def test_stale_answering_runtime_is_not_yet_not_no_go(tmp_path):
    _StaleAnswerer.finished = set()
    results = _run(_StaleAnswerer, tmp_path)
    failed = [r for r in results if not r.passed]
    assert [r.check for r in failed] == ["stale_session_rejected"]
    assert failed[0].fix_class == GC_FIXABLE
    assert "[stale-session-rejected]" in failed[0].message and "GC-fixable" in failed[0].message
    assert verdict(results).startswith("NOT YET")


def test_a_crashing_runtime_is_gc_fixable_not_yet(tmp_path):
    class Crashes(_FakeRuntime):
        def schedule(self, *a, **kw):
            raise RuntimeError("boom")

    results = _run(Crashes, tmp_path)
    crashed = [r for r in results if not r.passed]
    assert crashed and all(r.fix_class == GC_FIXABLE for r in crashed)
    assert any("RuntimeError: boom" in r.message for r in crashed)
    assert verdict(results).startswith("NOT YET")


def test_polling_uses_the_injected_clock_and_sleep(tmp_path):
    class NeverFinishes(_FakeRuntime):
        def observe(self, handle):
            return RoleOutcome(status="running")

    ticks = {"now": 0.0, "slept": 0}

    def sleep(seconds):
        ticks["slept"] += 1
        ticks["now"] += 5.0

    env = _env(FileHive(tmp_path / "h"), tmp_path, clock=lambda: ticks["now"], sleep=sleep)
    with pytest.raises(ConformanceFailure, match="observe-transitions"):
        check_observe_running_to_terminal(env, NeverFinishes)
    assert ticks["slept"] >= 2  # polled through the fake clock, never a real sleep


def test_stale_records_never_answer_a_new_session_in_bh_sink_reader():
    """BH's own reader (moved out of the runtime-facing kit check, which tests the runtime)."""
    from beadhive import outcome

    line = json.dumps(
        {
            "outcome": {"status": "done", "summary": "old run", "bead_id": "b"},
            "session_id": "old",
            "cost_usd": 0.0,
            "usage": {},
            "packs": [],
        }
    )
    metadata = {outcome.metadata_key("old"): outcome.record_document(line, "old", exit_code=0)}
    assert outcome.from_metadata(metadata, "new", bead_id="b") is None
    forged = dict(metadata)
    forged[outcome.metadata_key("new")] = metadata[outcome.metadata_key("old")]
    assert outcome.from_metadata(forged, "new", bead_id="b") is None
    assert outcome.from_metadata(metadata, "old", bead_id="b").status == "done"


def test_label_writing_runtime_fails_naming_invariant_and_class(tmp_path):
    failed = _failures(_LabelWriter, tmp_path)
    message = failed["no_lifecycle_writes"]
    assert "[no-lifecycle-writes]" in message
    assert "Decision 4" in message
    assert "structural" in message and "Mode 2" in message
    assert "runtime-was-here" in message
    assert verdict(_run(_LabelWriter, tmp_path)).startswith("NO-GO")


def test_session_id_ignoring_runtime_fails_structural(tmp_path):
    failed = _failures(_IgnoresSessionId, tmp_path)
    message = failed["schedule_handle_shape"]
    assert "[same-session-id]" in message
    assert "structural" in message
    assert "runtime-made-this-up" in message


def test_gate_hook_that_redispatches_is_caught(tmp_path):
    class Redispatcher(_FakeRuntime):
        def on_gate_resolved(self, resolution):
            self.schedule(
                resolution.bead_id, "developer", workspace="", instructions="", session_id="again"
            )

    message = _failures(Redispatcher, tmp_path)["gate_resolution_idempotent"]
    assert "[gate-idempotent]" in message and "GC-fixable" in message


def test_failure_carries_the_invariant():
    try:
        raise ConformanceFailure("no-lifecycle-writes", "x")
    except ConformanceFailure as exc:
        assert exc.invariant.fix_class == STRUCTURAL
    assert ConformanceFailure("stale-session-rejected", "x").invariant.fix_class == GC_FIXABLE
    assert ConformanceFailure("gate-idempotent", "x").invariant.fix_class == GC_FIXABLE


# ---- the real-bd hive: lifecycle writes made THROUGH bd are seen ----------------------------


class _BdDirectWriter(_FakeRuntime):
    """Misbehaves through the real bd binary, not through the kit's back-channel, the way a
    runtime shelling out to `bd` would. Subclasses pick the write."""

    def on_schedule(self, bead_id):
        self.write(bead_id)

    def write(self, bead_id):
        raise NotImplementedError

    def bd(self, *args):
        store = self.env.hive.store
        subprocess.run(
            ["bd", "-C", str(store), "--actor", "gc/runtime", *args],
            check=True,
            capture_output=True,
            timeout=120,
            env=scrubbed_env(),
        )


class _BdLabel(_BdDirectWriter):
    def write(self, bead_id):
        self.bd("update", bead_id, "--add-label", "gc-was-here")


class _BdClaim(_BdDirectWriter):
    def write(self, bead_id):
        self.bd("update", bead_id, "--assignee", "gc/runtime", "--force")


class _BdStatus(_BdDirectWriter):
    def write(self, bead_id):
        self.bd("update", bead_id, "--status", "blocked")


class _BdMetadata(_BdDirectWriter):
    def write(self, bead_id):
        self.bd("update", bead_id, "--metadata", '{"gc.note": "hello"}')


class _BdGateResolve(_BdDirectWriter):
    def write(self, bead_id):
        gate = self.env.hive._gates[bead_id]
        self.bd("gate", "resolve", gate, "--reason", "runtime did it")


@pytest.mark.integration
@needs_bd
def test_well_behaved_fake_passes_on_real_bd_without_caveat(bd_hive):
    results = run_conformance(_FakeRuntime, hive=bd_hive, timeout=30.0)
    assert [r.check for r in results if not r.passed] == []
    assert verdict(results).startswith("GO: ")
    assert results[0].hive_kind == "bd"


@pytest.mark.integration
@needs_bd
@pytest.mark.parametrize(
    ("runtime", "needle"),
    [
        (_BdLabel, "gc-was-here"),
        (_BdClaim, "assignee"),
        (_BdStatus, "status"),
        (_BdMetadata, "gc.note"),
        (_BdGateResolve, "gates"),
    ],
)
def test_runtime_writing_through_bd_directly_fails_no_lifecycle_writes(
    bd_hive, tmp_path, runtime, needle
):
    """The JSON FileHive cannot see these; the bd-backed hive must (review B1)."""
    with pytest.raises(ConformanceFailure) as caught:
        check_no_lifecycle_writes(_env(bd_hive, tmp_path), runtime)
    message = str(caught.value)
    assert "[no-lifecycle-writes]" in message and needle in message
    assert caught.value.invariant.fix_class == STRUCTURAL


@pytest.mark.integration
@needs_bd
def test_default_hive_is_the_real_bd_store_and_a_bd_writer_is_no_go(tmp_path):
    results = run_conformance(_BdLabel, workdir=tmp_path, timeout=30.0)
    assert {r.hive_kind for r in results} == {"bd"}
    assert [r.check for r in results if not r.passed] == ["no_lifecycle_writes"]
    assert verdict(results).startswith("NO-GO")


def test_cli_entry_point_reports_pass(tmp_path):

    mod = tmp_path / "goodrt.py"
    mod.write_text(
        "from beadhive.localloop import LocalRuntime\n"
        "def make(env):\n"
        "    return LocalRuntime(seat_command=env.seat_command, terminate_grace=1.0,"
        " envelope_grace=1.0)\n"
    )
    import os

    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), *sys.path]))
    done = subprocess.run(
        [sys.executable, "-m", "beadhive.runtime.conformance", "goodrt:make", "--timeout", "30"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "GO" in done.stdout


@pytest.mark.parametrize("status", ["done", "blocked", "handoff"])
def test_kit_sink_reader_matches_bh_role_output(status):
    """The records the kit's sink check reads are the ones `bh role --outcome-sink bead` writes:
    both go through outcome.record_document/from_metadata, so a stdout line stored as printed
    reads back identically."""
    import json

    from beadhive import outcome

    line = json.dumps(
        {
            "outcome": {"status": status, "summary": "s", "bead_id": "b"},
            "session_id": "S",
            "cost_usd": 0.0,
            "usage": {},
            "packs": [],
        }
    )
    doc = outcome.record_document(line, "S", exit_code=0)
    assert outcome.from_metadata({outcome.metadata_key("S"): doc}, "S").status == status


_SINK_LINES = (("done", "shipped"), ("blocked", "needs a decision"), ("handoff", "over to review"))


def _sink_round_trip(env: RuntimeEnv) -> None:
    """Independent of any runtime: the sink as Gas City uses it (`bh role --outcome-sink bead
    --session-id`) round-trips every seat verdict and an empty run classifies failed. This
    exercises only the hive's writer and BH's reader, so it is BH's test, not a conformance
    check: no runtime can fail it, so it never counts toward the verdict."""
    from beadhive import outcome

    for expected, summary in _SINK_LINES:
        bead, session_id = env.hive.create_bead(), new_session_id()
        line = json.dumps(
            {
                "outcome": {"status": expected, "summary": summary, "bead_id": bead},
                "session_id": session_id,
                "cost_usd": 0.0,
                "usage": {},
                "packs": [],
            }
        )
        assert env.hive.record_outcome(bead, session_id, line), "a fresh record was refused"
        found = outcome.from_metadata(env.hive.snapshot(bead).metadata, session_id, bead_id=bead)
        assert found is not None and found.status == expected and found.summary == summary
        assert not env.hive.record_outcome(bead, session_id, line.replace(summary, "rewritten"))
    bead, session_id = env.hive.create_bead(), new_session_id()
    env.hive.record_outcome(bead, session_id, "")
    found = outcome.from_metadata(env.hive.snapshot(bead).metadata, session_id, bead_id=bead)
    assert found is not None and found.status == "failed"


def test_sink_round_trip_is_not_a_conformance_check(tmp_path):
    """The kit-only sink round trip never calls the runtime, so the verdict must ignore it."""
    assert "sink-round-trip" not in INVARIANTS
    assert not any("sink" in name and "stale" not in name for name in CHECKS)
    results = _run(_FakeRuntime, tmp_path)
    assert [r.check for r in results] == list(CHECKS)
    assert len(results) == 6
    assert verdict(results).startswith("GO")


# ---- the sink as Gas City uses it: through the real `bh role` command ------------------------


def test_sink_check_passes_through_bh_role_outcome_sink(monkeypatch, tmp_path):
    """BH's own sink round trip (no runtime involved), with the record written by
    `bh role developer --bead B --outcome-sink bead --session-id S` (a stand-in seat prints the
    line) onto a FileHive."""
    from types import SimpleNamespace

    from tests.test_outcome_sink import _wire_headless
    from typer.testing import CliRunner

    from beadhive import bd, bd_cli
    from beadhive.cli import app

    class RoleSinkHive(FileHive):
        def record_outcome(self, bead_id, session_id, seat_stdout):
            _wire_headless(monkeypatch, tmp_path, seat_stdout, 0)
            before = self.snapshot(bead_id).metadata.get(f"bh.outcome.{session_id}")
            result = CliRunner().invoke(
                app,
                [
                    "role",
                    "developer",
                    "--bead",
                    bead_id,
                    "--outcome-sink",
                    "bead",
                    "--session-id",
                    session_id,
                ],
            )
            assert result.exit_code == 0, result.output
            return before is None

    hive = RoleSinkHive(tmp_path / "hive")

    class Routes:
        def issue_set_metadata_document(self, bead, key, document, *, actor="", capture=True):
            hive.set_metadata(bead, key, document)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        bd, "show", lambda bead, cwd, **_kw: {"metadata": hive.snapshot(bead).metadata}
    )
    monkeypatch.setattr(bd_cli, "routes", lambda cwd: Routes())
    env = RuntimeEnv(hive, hive.seat_command(), tmp_path, tmp_path / "i.md")

    _sink_round_trip(env)


_BLOCK_PYTEST = """
import runpy, sys
from importlib.abc import MetaPathFinder

class _Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "pytest" or name.startswith(("pytest.", "_pytest")):
            raise ImportError("pytest is blocked")

sys.meta_path.insert(0, _Block())
import beadhive.runtime.conformance as kit
assert "pytest" not in sys.modules
sys.argv = ["beadhive.runtime.conformance", "--help"]
try:
    runpy.run_module("beadhive.runtime.conformance", run_name="__main__")
except SystemExit as exc:
    assert exc.code in (0, None), exc.code
assert "pytest" not in sys.modules
print("ok")
"""


def test_kit_runs_without_pytest() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _BLOCK_PYTEST], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")


def test_suite_still_importable_lazily() -> None:
    from beadhive.runtime import conformance

    assert issubclass(conformance.RuntimeConformanceSuite, object)
    assert "RuntimeConformanceSuite" in conformance.__all__


# ---- the loop probe (bh-lxv3v): BeadHive's real loop drives the candidate ------------------------


class _NeverStarts(_FakeRuntime):
    """schedule() hands back a handle and launches nothing; observe() reads the outcome sink,
    which therefore never answers."""

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, **_kw):
        return RoleHandle(bead_id=bead_id, session_id=session_id)

    def observe(self, handle):
        return RoleOutcome(status="running", summary="no record yet")


class _AdoptableFake(_FakeRuntime):
    """Sessions outlive the loop, so the loop writes (and drops) a bh.runtime.handle itself."""

    adoptable = True


class _BdHandleWriter(_BdDirectWriter):
    """NOT adoptable, yet writes a bh.runtime.handle document through bd on schedule."""

    def write(self, bead_id):
        self.bd("update", bead_id, "--metadata", '{"bh.runtime.handle": {"runtime": "fake"}}')


def _fake_clock_env(hive, tmp_path) -> RuntimeEnv:
    """The probe's waits run on a FakeClock: a turn that never finishes gives up after a couple
    of passes instead of a real timeout."""
    clock = FakeClock()
    base = _env(hive, tmp_path)
    return RuntimeEnv(
        hive,
        base.seat_command,
        base.workspace,
        base.instructions,
        timeout=1.0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )


@pytest.mark.integration
@needs_bd
def test_loop_probe_passes_for_local_runtime_through_the_runner(bd_hive):
    results = run_conformance(_local, hive=bd_hive, timeout=30.0, loop=True)
    assert [r.check for r in results] == [*CHECKS, *LOOP_CHECKS]
    assert [r.check for r in results if not r.passed] == []
    assert verdict(results).startswith("GO: ")


@pytest.mark.integration
@needs_bd
def test_loop_probe_fails_naming_the_check_when_schedule_never_starts_a_session(bd_hive, tmp_path):
    with pytest.raises(ConformanceFailure) as caught:
        check_loop_drives_one_turn(_fake_clock_env(bd_hive, tmp_path), _NeverStarts)
    message = str(caught.value)
    assert message.startswith("[loop-session-started]"), message
    assert "no seat session ever started" in message
    assert caught.value.invariant.fix_class == GC_FIXABLE

    # Through the runner: the result names the check and the invariant, and reads NOT YET.
    # timeout=0 makes every wait on the never-finishing turn give up after one look.
    results = run_conformance(_NeverStarts, hive=bd_hive, timeout=0.0, loop=True)
    (loop_result,) = [r for r in results if r.check == "loop_drives_one_turn"]
    assert not loop_result.passed and loop_result.fix_class == GC_FIXABLE
    assert "[loop-session-started]" in loop_result.message
    assert verdict(results).startswith("NOT YET")


@pytest.mark.integration
@needs_bd
def test_loop_probe_attributes_the_loops_handle_to_the_loop_for_an_adoptable_runtime(
    bd_hive, tmp_path
):
    """Carry-forward from the bh-fwm82 review: an adoptable candidate's bead carries the loop's
    bh.runtime.handle mid-turn; that write is the loop's, so the candidate passes."""
    check_loop_drives_one_turn(_fake_clock_env(bd_hive, tmp_path), _AdoptableFake)


@pytest.mark.integration
@needs_bd
def test_loop_probe_blames_a_non_adoptable_runtime_that_writes_the_handle_key(bd_hive, tmp_path):
    with pytest.raises(ConformanceFailure) as caught:
        check_loop_drives_one_turn(_fake_clock_env(bd_hive, tmp_path), _BdHandleWriter)
    assert str(caught.value).startswith("[no-lifecycle-writes]")
    assert "bh.runtime.handle" in str(caught.value)
    assert caught.value.invariant.fix_class == STRUCTURAL


def test_the_loop_probe_on_a_file_hive_is_bh_side_never_gc_fixable(tmp_path):
    results = run_conformance(_FakeRuntime, hive=FileHive(tmp_path / "h"), timeout=10.0, loop=True)
    (loop_result,) = [r for r in results if r.check == "loop_drives_one_turn"]
    assert not loop_result.passed
    assert loop_result.fix_class == BH_SIDE
    assert "[bh-loop]" in loop_result.message and "BH-side" in loop_result.message
    assert verdict(results).startswith("INCONCLUSIVE")


def test_a_bh_side_failure_never_outranks_a_structural_one():
    from beadhive.runtime.conformance import CheckResult

    bh = CheckResult("loop_drives_one_turn", False, "x", BH_SIDE, "bd")
    structural = CheckResult("no_lifecycle_writes", False, "x", STRUCTURAL, "bd")
    fixable = CheckResult("stale_session_rejected", False, "x", GC_FIXABLE, "bd")
    assert verdict([bh, structural]).startswith("NO-GO")
    assert verdict([bh, fixable]).startswith("INCONCLUSIVE")
    assert verdict([fixable]).startswith("NOT YET")


def test_lifecycle_diff_attributes_loop_keys_to_the_loop_only():
    from beadhive.runtime.conformance.checks import _lifecycle_diff

    before = BeadState("in_progress", "dev/conformance", frozenset(), {})
    after = BeadState(
        "in_progress", "dev/conformance", frozenset(), {"bh.runtime.handle": {"runtime": "gc"}}
    )
    assert _lifecycle_diff(before, after, set()) == ["metadata key 'bh.runtime.handle' changed"]
    assert _lifecycle_diff(before, after, set(), loop_keys=frozenset({"bh.runtime.handle"})) == []


def test_an_exception_inside_the_runtime_is_the_runtimes_and_outside_is_bh_side():
    from beadhive.runtime.conformance.loop import _raised_in

    class Crashy:
        def schedule(self):
            raise TypeError("runtime bug")

    rt = Crashy()
    try:
        rt.schedule()
    except TypeError as exc:
        assert _raised_in(rt, exc)
    try:
        raise KeyError("loop bug")
    except KeyError as exc:
        assert not _raised_in(rt, exc)


def test_suite_runs_the_loop_probe_only_when_opted_in():
    assert RuntimeConformanceSuite.loop_probe is False
    assert hasattr(RuntimeConformanceSuite, "test_loop_drives_one_turn")
    assert TestLocalRuntimeConformanceOnBd.loop_probe is True
