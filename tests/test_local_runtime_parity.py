"""The loop, driving seats through LocalRuntime, does exactly what its own spawn path did.

B1 step 1 (bh-lhxy8) proved LocalRuntime behaviour-identical to the loop's own spawn path; step 2
(bh-03rvd) deleted that path — the loop now schedules every seat through the runtime. So the
comparison here is no longer loop-vs-runtime (that would be the runtime compared with itself).
It is the loop AS IT IS NOW against a FROZEN REFERENCE of the pre-change loop path —
:func:`_reference_spawn_and_harvest`, a verbatim transcription of the old ``_spawn_for`` /
``_harvest`` / ``_cause_for`` / ``_cause_reason`` built only from this module's process
primitives (``seat_argv``, ``spawn_seat``, ``reap_group``, ``classify_run``). Both run the SAME
seat, and these tests compare what each one actually did:

- the ``asyncio.create_subprocess_exec`` call itself (argv, cwd, the child's full environment
  including ``BAML_PROFILE_DIR`` and the run-journal child env, ``start_new_session`` and the
  three pipes), recorded by a pass-through wrapper so the real process still runs;
- the process group (its own session, reaped once harvested);
- the run-journal records, byte-for-byte apart from wall-clock timestamps and pids;
- the cause the loop's harvest writes and its reason, byte for byte;
- the pass report's fields (``dispatched``, ``harvested``, ``causes``, ``routing``);
- the runtime's ``observe()`` answer against :func:`beadhive.outcome.role_outcome` over the
  reference's own classification, field by field.

The uuid minting both paths do is pinned to one value so the minted identities (provider
continuation, seat process id, run id) line up and the comparison can be exact. Real processes
throughout, for the reason ``tests/test_localloop.py`` gives; completion is awaited through the
seat's own bounded ``wait_exit``, never a sleep.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from beadhive import localloop, outcome, run_journal, seatrun
from beadhive.complexity import ComplexityTier
from beadhive.model_routing import ModelBlockedVerdict, ModelSelection
from beadhive.runtime import (
    RoleHandle,
    RoleOutcome,
    Runtime,
    ScheduleRefused,
    can_cancel,
    can_close,
    can_notify,
    is_adoptable,
)

STUB_SEAT = Path(__file__).parent / "fixtures" / "stub_seat.py"
STUB_COMMAND = f"{sys.executable} {STUB_SEAT}"
BEAD = "bh-par1"
HIVE = "github/acme/core"
DIGEST = "sha256:" + "a" * 64
FIXED = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
PROVIDER = f"provider-{FIXED}"


def _run(coro):
    return asyncio.run(coro)


def _selection():
    return ModelSelection(
        required_tier=ComplexityTier.COMPLEX,
        preferred_model=None,
        selected_model="anthropic/claude-sonnet-4-5",
        policy="loose",
        role="developer",
        harness="claude",
        endpoint=None,
        availability_source="explicit_configuration",
        selection_reason="least-overpowered available model covering the required complexity",
    )


def _blocked():
    return ModelBlockedVerdict(
        required_tier=ComplexityTier.REASONING,
        preferred_model="openai/missing",
        policy="strict",
        role="developer",
        harness="claude",
        availability_source="gateway_live",
        reason="preferred model openai/missing is unavailable",
        remediation="configure an available route",
    )


def _identity() -> run_journal.RunIdentity:
    return run_journal.RunIdentity(
        hive=HIVE, bead=BEAD, driver="baml", provider="anthropic", manifest_digest=DIGEST
    )


@pytest.fixture
def launches(monkeypatch):
    """Every subprocess launch either path makes, recorded before the real launch runs."""
    calls: list[dict] = []
    real = asyncio.create_subprocess_exec

    async def recording(*argv, **kwargs):
        calls.append({"argv": tuple(argv), **kwargs})
        return await real(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    monkeypatch.setattr(uuid, "uuid4", lambda: FIXED)
    return calls


@pytest.fixture
def causes(monkeypatch):
    """What the loop's harvest would write onto the bead, without a bd."""
    written: list[tuple[str, str, str]] = []

    def record(_cwd, bead, cause, *, reason, actor=""):
        written.append((bead, cause, reason))
        return True

    monkeypatch.setattr(localloop, "record_cause", record)
    return written


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "wt" / BEAD
    ws.mkdir(parents=True)
    (tmp_path / "hive").mkdir()
    return ws


def _envelope_seat(tmp_path: Path) -> str:
    """A seat that dies with only a killed-run envelope on stdout (the INCOMPLETE-but-priced
    shape), whatever argv it is handed."""
    script = tmp_path / "envelope_seat.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'session_id': 'provider-killed', 'cost_usd': 0.25, 'usage': {},"
        " 'terminal_reason': 'sigterm'}))\n"
        "sys.exit(143)\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


# The cases: one per cause the loop's classify path can write, across every launch seam.
#   name: (instruction directives, seam, seat_bundle)
#   seam "launch"   -> a SeatLaunchContext (the qualified BAML path: command, bundle, journal)
#   seam "identity" -> the run_identity hook (journal minted from the loop's action)
#   seam "plain"    -> neither: seat_command + seat_bundle, no journal
CASES = {
    "done": (("STUB_STATUS=done", "STUB_SUMMARY=all good"), "launch", ""),
    "blocked": (("STUB_STATUS=blocked", "STUB_SUMMARY=need a human"), "identity", ""),
    "handoff": (("STUB_STATUS=handoff",), "identity", ""),
    "mismatch": (("STUB_STATUS=done", "STUB_BEAD_MISMATCH=true"), "launch", ""),
    # The stub rejects --bundle, so a bundle-carrying argv is the no-SeatRun failure case.
    "failed": (("STUB_STATUS=done",), "plain", "/bundles/developer.json"),
    "envelope": ((), "envelope", ""),
}


def _seams(name: str, tmp_path: Path):
    """(seat_command, seat_bundle, launch_context, run_identity hook, identity calls)."""
    directives, seam, bundle = CASES[name]
    calls: list[tuple] = []
    command = _envelope_seat(tmp_path) if seam == "envelope" else STUB_COMMAND
    launch = None
    identity = None
    if seam in ("launch", "envelope"):
        ctx = localloop.SeatLaunchContext(
            command=command,
            bundle="",
            hive=HIVE,
            driver="baml",
            provider="anthropic",
            manifest_digest=DIGEST,
        )
        launch = lambda role: ctx  # noqa: E731 - one-line seam stand-in
    elif seam == "identity":

        def identity(bead, action, role, decision):
            calls.append((bead, action, role, decision))
            return _identity()

    return command, bundle, launch, identity, calls


def _journal_records(path: Path) -> list[dict]:
    """Journal lines with the two facts that legitimately differ between two real runs blanked:
    the wall-clock timestamp and the OS-assigned pid/pgid."""
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        record.pop("timestamp_ms")
        process = record["activity"].get("process", {})
        for key in ("pid", "pgid"):
            if key in process:
                process[key] = "<pid>"
        records.append(record)
    return records


# ---- the frozen reference: the loop's own spawn path as it was before bh-03rvd ------------------


def _legacy_cause_for(cls: seatrun.Classification) -> str:
    """``LocalLoop._cause_for`` before bh-03rvd, verbatim (over a classification)."""
    if cls.bead_id_mismatch:
        return localloop.CAUSE_MISMATCH
    return {
        seatrun.RunOutcome.DONE: "",
        seatrun.RunOutcome.BLOCKED: localloop.CAUSE_BLOCKED,
        seatrun.RunOutcome.HANDOFF: localloop.CAUSE_HANDOFF,
        seatrun.RunOutcome.INCOMPLETE: localloop.CAUSE_FAILED,
    }[cls.outcome]


def _legacy_cause_reason(cls: seatrun.Classification, exit_code, action: str) -> str:
    """``LocalLoop._cause_reason`` before bh-03rvd, verbatim — it branched on the envelope
    being PRESENT, not on its session id."""
    parts = [f"exit {exit_code}", f"action {action}"]
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


def _reference_spawn_and_harvest(
    *, command, bundle, launch, identity, decision, action, workspace, instructions, env, base
):
    """The pre-bh-03rvd ``LocalLoop._spawn_for`` + ``_harvest`` for one seat, transcribed from
    the deleted code: the loop built the argv and the journal itself, called ``spawn_seat``
    directly, then collected, reaped, classified and journaled the harvest itself. Returns
    ``(seat, classification, report, causes)`` — the observable record that path left."""
    report = localloop.PassReport()
    causes: list[tuple[str, str, str]] = []
    routing_payload = None
    translated = None
    if isinstance(decision, ModelSelection):
        routing_payload = {"bead": BEAD, **decision.as_dict()}
        report.routing += ((BEAD, routing_payload),)
        translated = localloop.model_routing.translate_for_harness(
            decision.selected_model, decision.harness
        )
    seat_process_id = f"seat-{uuid.uuid4()}"
    provider_continuation = f"provider-{uuid.uuid4()}"
    ctx = launch("developer") if launch is not None else None
    argv = localloop.seat_argv(
        ctx.command if ctx is not None else command,
        "developer",
        workspace=str(workspace),
        bead=BEAD,
        instructions=str(instructions),
        session_id=provider_continuation,
        model=translated,
        bundle=ctx.bundle if ctx is not None else bundle,
    )
    journal = None
    if ctx is not None:
        journal = run_journal.RunJournal.create(ctx.run_identity(BEAD), base=base)
    elif identity is not None:
        found = identity(BEAD, action, "developer", decision)
        if found is not None:
            journal = run_journal.RunJournal.create(found, base=base)

    async def go():
        seat = await localloop.spawn_seat(
            argv,
            bead_id=BEAD,
            role="developer",
            action=action,
            session_id=seat_process_id,
            provider_continuation=provider_continuation,
            cwd=str(workspace),
            env=env,
            routing=routing_payload,
            journal=journal,
        )
        report.dispatched += (BEAD,)
        assert seat.pgid == seat.pid != os.getpgrp(), "the reference seat leads its own group"
        assert await seat.wait_exit(30), "reference seat never exited"
        stdout = await seat.collect(timeout=3.0)
        reap = await localloop.reap_group(seat, grace=5.0)
        cls = seatrun.classify_run(seat.proc.returncode or 0, stdout, bead=BEAD)
        report.harvested += ((BEAD, str(cls.outcome)),)
        cause = _legacy_cause_for(cls)
        if cause:
            causes.append((BEAD, cause, _legacy_cause_reason(cls, seat.proc.returncode, action)))
            report.causes += ((BEAD, cause),)
        localloop._journal_harvest(seat, cls, reap)
        return seat, cls

    seat, cls = _run(go())
    return seat, cls, report, causes


def _drive_loop(tmp_path, workspace, instructions, *, name, decision, action, journal_base):
    """The loop as it is now: one ``_spawn_for`` + harvest, every seat through LocalRuntime."""
    command, bundle, launch, identity, calls = _seams(name, tmp_path)
    env = dict(os.environ, BH_PARITY_MARKER="loop-and-runtime")
    loop = localloop.LocalLoop(
        hive_dir=tmp_path / "hive",
        epic="epic-1",
        actor="disp/loop",
        seat_command=command,
        seat_bundle=bundle,
        routing=lambda _bead, _role: decision,
        instructions=lambda _a, _b, _r: str(instructions),
        workspace_for=lambda _bead: str(workspace),
        launch_context=launch,
        run_identity=identity,
        journal_base=journal_base,
        env=env,
    )
    report = localloop.PassReport()

    async def go():
        await loop._spawn_for(BEAD, action=action, role="developer", report=report)
        assert loop.in_flight[BEAD].action == action
        seat = loop.runtime._runs[BEAD]
        assert seat.pgid == seat.pid != os.getpgrp(), "the loop seat leads its own group"
        assert await seat.wait_exit(30), "loop seat never exited"
        await loop._harvest(report)
        assert BEAD not in loop.in_flight
        observed = loop.runtime._terminal[BEAD][1]
        await loop.shutdown()
        return seat, observed

    seat, observed = _run(go())
    return seat, observed, report, calls


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_loop_through_local_runtime_matches_its_pre_change_spawn_path(
    name, tmp_path, workspace, launches, causes
):
    directives = CASES[name][0]
    instructions = tmp_path / "brief.md"
    instructions.write_text("\n".join(directives) + "\n", encoding="utf-8")
    decision = _selection() if name in ("done", "envelope") else None
    action = "resume"
    journal_base = tmp_path / "journals"

    command, bundle, launch, identity, ref_identity_calls = _seams(name, tmp_path)
    ref_seat, ref_cls, ref_report, ref_causes = _reference_spawn_and_harvest(
        command=command,
        bundle=bundle,
        launch=launch,
        identity=identity,
        decision=decision,
        action=action,
        workspace=workspace,
        instructions=instructions,
        env=dict(os.environ, BH_PARITY_MARKER="loop-and-runtime"),
        base=journal_base,
    )
    ref_journal = journal_base.parent / "reference-journal.jsonl"
    if any(journal_base.rglob("*.jsonl")):
        (journal,) = journal_base.rglob("*.jsonl")
        journal.rename(ref_journal)
    loop_seat, observed, loop_report, loop_identity_calls = _drive_loop(
        tmp_path,
        workspace,
        instructions,
        name=name,
        decision=decision,
        action=action,
        journal_base=journal_base,
    )

    # 1. The launch: one exec per path, identical in every argument.
    assert len(launches) == 2
    ref_launch, loop_launch = launches
    assert loop_launch["argv"] == ref_launch["argv"]
    assert loop_launch["env"] == ref_launch["env"]
    assert loop_launch["env"]["BH_PARITY_MARKER"] == "loop-and-runtime"
    assert loop_launch["env"]["BAML_PROFILE_DIR"].endswith(PROVIDER)
    assert loop_launch == ref_launch
    assert loop_launch["start_new_session"] is True
    assert (loop_seat.session_id, loop_seat.provider_continuation, loop_seat.action) == (
        ref_seat.session_id,
        ref_seat.provider_continuation,
        ref_seat.action,
    )
    assert loop_seat.routing == ref_seat.routing

    # 2. The process group: led by the seat, gone once harvested.
    assert not localloop.group_alive(ref_seat.pgid)
    assert not localloop.group_alive(loop_seat.pgid)

    # 3. The journal: same records, same order, same run identity.
    assert loop_identity_calls == ref_identity_calls
    if CASES[name][1] == "plain":
        assert not ref_journal.exists() and not any(journal_base.rglob("*.jsonl"))
        assert "BH_RUN_ID" not in loop_launch["env"]
    else:
        (loop_journal,) = journal_base.rglob("*.jsonl")
        assert loop_launch["env"]["BH_RUN_JOURNAL_PATH"] == str(loop_journal.resolve())
        ref_records = _journal_records(ref_journal)
        assert [r["activity"]["kind"] for r in ref_records] == [
            "run.created",
            "process.spawned",
            "process.harvested",
        ]
        assert _journal_records(loop_journal) == ref_records

    # 4. The causes the loop wrote onto the bead: same cause, same reason text, byte for byte.
    assert causes == ref_causes
    assert (
        localloop.cause_for_outcome(observed)
        == _legacy_cause_for(ref_cls)
        == (EXPECTED_CAUSES[name])
    )

    # 5. The pass report's observable fields.
    assert loop_report.dispatched == ref_report.dispatched == (BEAD,)
    assert loop_report.harvested == ref_report.harvested
    assert loop_report.causes == ref_report.causes
    assert loop_report.as_dict()["routing"] == ref_report.as_dict()["routing"]

    # 6. observe() is outcome.role_outcome over the reference's own classification.
    assert observed == outcome.role_outcome(
        ref_cls, routing=ref_seat.routing, exit_code=ref_seat.proc.returncode
    )


EXPECTED_CAUSES = {
    "done": "",
    "blocked": localloop.CAUSE_BLOCKED,
    "handoff": localloop.CAUSE_HANDOFF,
    "mismatch": localloop.CAUSE_MISMATCH,
    "failed": localloop.CAUSE_FAILED,
    "envelope": localloop.CAUSE_FAILED,
}

#: The bh-lhxy8 review repro: a killed envelope whose session is JSON null. The old loop quoted
#: the envelope; an outcome-side check on ``session_id is not None`` quoted the detail instead.
NULL_SESSION_ENVELOPE = '{"session_id":null,"cost_usd":0.2,"usage":{},"terminal_reason":"sigterm"}'


@pytest.mark.parametrize(
    ("name", "stdout", "exit_code"),
    [
        ("done", '{"outcome":{"status":"done","summary":"ok"},"session_id":"p","cost_usd":1.5}', 0),
        ("blocked", '{"outcome":{"status":"blocked","summary":"why"},"session_id":"p"}', 0),
        ("handoff", '{"outcome":{"status":"handoff"},"session_id":"p","cost_usd":0.1}', 0),
        (
            "mismatch",
            '{"outcome":{"status":"done","bead_id":"other"},"session_id":"p","cost_usd":0.1}',
            0,
        ),
        ("failed", "", 2),
        ("envelope", '{"session_id":"p","cost_usd":0.2,"usage":{}}', 143),
        ("envelope", NULL_SESSION_ENVELOPE, 143),
    ],
)
def test_outcome_cause_mapping_matches_the_loops_classify_path(name, stdout, exit_code):
    """Pure parity over the classify path: cause AND reason, without any process, against the
    frozen pre-change mapping."""
    cls = seatrun.classify_run(exit_code, stdout, bead=BEAD)
    mapped = outcome.role_outcome(cls, exit_code=exit_code)
    assert _legacy_cause_for(cls) == localloop.cause_for_outcome(mapped)
    assert localloop.cause_for_outcome(mapped) == EXPECTED_CAUSES[name]
    assert _legacy_cause_reason(cls, exit_code, "x") == localloop.cause_reason_for_outcome(
        mapped, action="x"
    )


def test_a_null_session_envelope_quotes_the_envelope_not_the_detail():
    """The golden text for the divergence the bh-lhxy8 review found, spelled out."""
    cls = seatrun.classify_run(143, NULL_SESSION_ENVELOPE, bead=BEAD)
    mapped = outcome.role_outcome(cls, exit_code=143)
    assert (mapped.session_id, mapped.envelope_present) == (None, True)
    assert localloop.cause_reason_for_outcome(mapped, action="dispatch") == (
        "exit 143; action dispatch; session None; terminal_reason sigterm"
    )


def test_a_running_outcome_has_no_cause():
    with pytest.raises(ValueError, match="non-terminal"):
        localloop.cause_for_outcome(RoleOutcome(status="running"))


# ---- refusals: the ValueError contract carries what the loop records ----------------------------


def test_an_invalid_workspace_refuses_with_the_loops_cause_and_reason(tmp_path, launches, causes):
    (tmp_path / "hive").mkdir()
    missing = tmp_path / "no-such-worktree"
    loop = localloop.LocalLoop(
        hive_dir=tmp_path / "hive",
        epic="epic-1",
        actor="disp/loop",
        seat_command=STUB_COMMAND,
        routing=lambda _b, _r: None,
        instructions=lambda _a, _b, _r: str(tmp_path),
        workspace_for=lambda _bead: str(missing),
    )
    _run(loop._spawn_for(BEAD, action="dispatch", role="dispatcher", report=localloop.PassReport()))

    rt = localloop.LocalRuntime(seat_command=STUB_COMMAND)
    with pytest.raises(ScheduleRefused, match="no such path") as refused:
        rt.schedule(
            BEAD, "dispatcher", workspace=str(missing), instructions=str(tmp_path), session_id="s"
        )

    assert isinstance(refused.value, ValueError)
    assert causes == [
        (
            BEAD,
            localloop.cause_for_outcome(RoleOutcome(status=refused.value.status)),
            refused.value.reason,
        )
    ]
    assert launches == [] and rt._runs == {}


def test_a_strict_routing_block_refuses_with_the_loops_cause_reason_and_evidence(
    tmp_path, workspace, launches, causes
):
    blocked = _blocked()
    loop = localloop.LocalLoop(
        hive_dir=tmp_path / "hive",
        epic="epic-1",
        actor="disp/loop",
        seat_command=STUB_COMMAND,
        routing=lambda _b, _r: blocked,
        instructions=lambda _a, _b, _r: str(tmp_path),
        workspace_for=lambda _bead: str(workspace),
    )
    report = localloop.PassReport()
    _run(loop._spawn_for(BEAD, action="dispatch", role="developer", report=report))

    rt = localloop.LocalRuntime(seat_command=STUB_COMMAND)
    with pytest.raises(ScheduleRefused) as refused:
        rt.schedule(
            BEAD,
            "developer",
            workspace=str(workspace),
            instructions=str(tmp_path),
            session_id="s",
            decision=blocked,
        )

    exc = refused.value
    assert causes == [
        (BEAD, localloop.cause_for_outcome(RoleOutcome(status=exc.status)), exc.reason)
    ]
    assert report.as_dict()["routing"][BEAD] == exc.payload
    assert launches == [] and rt._runs == {}


# ---- optional capabilities --------------------------------------------------------------------


def test_local_runtime_offers_cancel_notify_close_and_is_not_adoptable():
    rt = localloop.LocalRuntime()
    assert isinstance(rt, Runtime)
    assert (can_cancel(rt), can_notify(rt), can_close(rt)) == (True, True, True)
    assert is_adoptable(rt) is False


def _hanging(tmp_path, workspace, rt, *, session="provider-hang"):
    inst = tmp_path / "hang.md"
    inst.write_text("STUB_HANG=true\n", encoding="utf-8")
    return rt.schedule(
        BEAD, "developer", workspace=str(workspace), instructions=str(inst), session_id=session
    )


def test_cancel_by_handle_walks_the_ladder_and_settles_the_outcome(tmp_path, workspace):
    # Generous graces (bh-lhxy8 review): they bound how long a loaded host may take to deliver
    # the envelope and empty the group, and a tight one measures machine load, not behaviour.
    with localloop.LocalRuntime(
        seat_command=STUB_COMMAND, terminate_grace=5.0, envelope_grace=5.0
    ) as rt:
        handle = _hanging(tmp_path, workspace, rt)
        seat = rt._runs[BEAD]
        stale = RoleHandle(bead_id=BEAD, session_id="provider-earlier-turn")

        assert rt.cancel(stale) is None, "a stale handle never cancels the current run"
        assert not seat.finished
        assert rt.cancel(RoleHandle(bead_id="bh-other", session_id="x")) is None

        result = rt.cancel(handle)
        assert result is not None
        # The cooperative rung: the stub wraps up with a handoff SeatRun and exits 0.
        assert (result.status, result.exit_code, result.session_id, result.cancel_rung) == (
            "handoff",
            0,
            "provider-hang",
            localloop.RUNG_COOPERATIVE,
        )
        assert localloop.cause_for_outcome(result) == localloop.CAUSE_HANDOFF
        assert not localloop.group_alive(seat.pgid)
        assert rt.observe(handle) is result, "observe answers the settled cancel outcome"
        assert rt.cancel(handle) is result, "cancelling a settled run does not act again"


def test_cancel_of_a_finished_run_harvests_it_instead(tmp_path, workspace):
    inst = tmp_path / "done.md"
    inst.write_text("STUB_STATUS=done\n", encoding="utf-8")
    with localloop.LocalRuntime(seat_command=STUB_COMMAND) as rt:
        handle = rt.schedule(
            BEAD, "developer", workspace=str(workspace), instructions=str(inst), session_id="p1"
        )
        assert rt._submit(rt._runs[BEAD].wait_exit(30))
        result = rt.cancel(handle)
        assert result is not None and result.status == "done"
        assert result.cancel_rung == localloop.RUNG_EXITED, "the ladder's own 'exited' answer"
        assert rt.observe(handle) is result


def test_notify_writes_to_the_live_runs_stdin_only(tmp_path, workspace):
    # Generous graces (bh-lhxy8 review): they bound how long a loaded host may take to deliver
    # the envelope and empty the group, and a tight one measures machine load, not behaviour.
    with localloop.LocalRuntime(
        seat_command=STUB_COMMAND, terminate_grace=5.0, envelope_grace=5.0
    ) as rt:
        assert rt.notify(RoleHandle(bead_id=BEAD, session_id="p"), "hello") is False
        handle = _hanging(tmp_path, workspace, rt)
        seat = rt._runs[BEAD]
        stale = RoleHandle(bead_id=BEAD, session_id="provider-earlier-turn")

        assert rt.notify(stale, "sibling bh-x was cancelled") is False
        assert rt.notify(handle, "sibling bh-x was cancelled") is True
        # The stub reads any plain stdin line as the wrap-up trigger, which proves delivery.
        assert rt._submit(seat.wait_exit(30)), "the notified seat never read its stdin"
        assert rt.observe(handle).status == "handoff"
        assert rt.notify(handle, "too late") is False


def test_role_outcome_additions_are_defaulted_and_positional_construction_is_unchanged():
    legacy = RoleOutcome("done", "summary", {"bead": BEAD})
    assert (legacy.status, legacy.summary, legacy.routing) == ("done", "summary", {"bead": BEAD})
    assert (
        legacy.bead_id_mismatch,
        legacy.detail,
        legacy.exit_code,
        legacy.cost_usd,
        legacy.session_id,
        legacy.terminal_reason,
        legacy.envelope_present,
        legacy.cancel_rung,
    ) == (False, "", None, 0.0, None, None, False, None)
    assert RoleOutcome(status="running") == RoleOutcome("running", "", None)


def test_role_outcome_additions_are_keyword_only():
    """A fourth positional argument can never land in an additive field by position."""
    with pytest.raises(TypeError):
        RoleOutcome("done", "summary", None, True)  # type: ignore[misc]
    assert RoleOutcome("done", bead_id_mismatch=True).bead_id_mismatch is True
