"""Foreground process ownership for qualified ``bh role`` launches.

Qualified packed seats cannot be handed to the legacy blocking runner: that runner exposes no
live output and owns neither the descendant process group nor journal terminal events.  This
module deliberately reuses LocalLoop's one spawn/reap discipline while keeping the direct role's
stdout contract intact: provider stdout/stderr are forwarded as chunks arrive and bh never emits
a second synthetic ``SeatRun``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from . import localloop, log, run_journal

_LOG = log.get_logger(__name__)


def _forward(stream, chunk: bytes) -> None:
    """Write one provider chunk immediately under real stdio and Typer's test capture."""

    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(chunk)
        binary.flush()
        return
    stream.write(chunk.decode("utf-8", errors="replace"))
    stream.flush()


async def _wait_direct_exit(seat: localloop.SeatProcess) -> None:
    while not seat.finished:
        await asyncio.sleep(0.02)


def _append_harvest(
    journal: run_journal.RunJournal,
    classification,
    *,
    exit_code: int | None,
    group_gone: bool,
) -> None:
    outcome, usage, cost = run_journal.activity_outcome(classification)
    activity: dict[str, object] = {
        "kind": "process.harvested",
        "phase": "finished" if group_gone else "failed",
        "outcome_code": outcome,
        "process": {"exit_code": exit_code, "group_gone": group_gone},
    }
    if usage:
        activity["usage"] = usage
    if cost:
        activity["cost_usd"] = cost
    if journal.degraded:
        activity["journal_degraded"] = True
    journal.append(activity, operation="harvest")


async def _cancel_shielded(seat: localloop.SeatProcess) -> None:
    task = asyncio.create_task(
        localloop.cancel(
            seat,
            rungs=(localloop.RUNG_SIGNAL,),
            envelope_grace=3.0,
            terminate_grace=5.0,
        )
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Caller cancellation must not interrupt the descendant reaper halfway through.
        await task
        raise


async def _run_foreground(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    journal: run_journal.RunJournal,
    bead: str,
    role: str,
    seat_process_id: str,
    provider_continuation: str,
) -> int:
    stop = asyncio.Event()
    received_signal: list[int] = []
    loop = asyncio.get_running_loop()
    installed: list[int] = []

    def request_stop(sig: int) -> None:
        received_signal[:] = [sig]
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop, sig)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    seat: localloop.SeatProcess | None = None
    try:
        seat = await localloop.spawn_seat(
            argv,
            bead_id=bead,
            role=role,
            action="direct-role",
            session_id=seat_process_id,
            provider_continuation=provider_continuation,
            cwd=cwd,
            env=env,
            journal=journal,
            stdout_sink=lambda chunk: _forward(sys.stdout, chunk),
            stderr_sink=lambda chunk: _forward(sys.stderr, chunk),
        )
        process_wait = asyncio.create_task(_wait_direct_exit(seat))
        signal_wait = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            (process_wait, signal_wait), return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if signal_wait in done and received_signal and not seat.finished:
            await _cancel_shielded(seat)
            return 128 + received_signal[0]

        stdout = await seat.collect(timeout=3.0)
        reap = await localloop.reap_group(seat, grace=5.0)
        classification = localloop.seatrun.classify_run(
            seat.proc.returncode if seat.proc.returncode is not None else -1,
            stdout,
            bead=bead or None,
        )
        _append_harvest(
            journal,
            classification,
            exit_code=seat.proc.returncode,
            group_gone=reap.group_gone,
        )
        _LOG.info(
            "role_process_harvested",
            bead=bead,
            role=role,
            session_id=seat.session_id,
            provider_continuation_observed=(
                classification.seat_run.session_id if classification.seat_run else ""
            ),
            group_gone=reap.group_gone,
        )
        if classification.seat_run is None or not reap.group_gone:
            return 1
        return seat.proc.returncode or 0
    except asyncio.CancelledError:
        if seat is not None and (not seat.finished or localloop.group_alive(seat.pgid)):
            await _cancel_shielded(seat)
        raise
    except BaseException:
        if seat is not None and (not seat.finished or localloop.group_alive(seat.pgid)):
            await _cancel_shielded(seat)
        raise
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)


def run_foreground(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    journal: run_journal.RunJournal,
    bead: str,
    role: str,
    seat_process_id: str,
    provider_continuation: str,
) -> int:
    """Run one qualified packed seat with live forwarding and descendant ownership."""

    return asyncio.run(
        _run_foreground(
            argv,
            cwd=cwd,
            env=env,
            journal=journal,
            bead=bead,
            role=role,
            seat_process_id=seat_process_id,
            provider_continuation=provider_continuation,
        )
    )
