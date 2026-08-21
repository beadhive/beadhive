"""Command-coupled, append-only measured-fact replication.

``bh checkpoint run`` is deliberately the only mutation surface in this module: a checkpoint
cannot be recorded, and a wisp step cannot be closed, independently of the command whose result
they describe. The persistent record is metadata only. This helper never calls ``bd dep`` and
therefore cannot create the persistent-to-ephemeral edge that wisp GC can strip.

The measurement is a JSON object stored below one checkpoint-specific metadata key. It must
carry ``measured_at`` and at least one concrete measured field. Keys are append-only because bd's
metadata history cannot recover an overwritten value (bh-yber2.1 M6).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import tempfile
from pathlib import Path

import typer

from . import bd, config, registry
from .run import run

app = typer.Typer(
    no_args_is_help=True,
    help="Run a command, then append its measured fact and optionally close its wisp step.",
)

_HIVE = typer.Option("", "--hive", help="target hive (default: cwd's hive)")
_VALUE = typer.Option(
    ...,
    "--value",
    metavar="JSON",
    help='measurement object, including "measured_at" and at least one concrete fact',
)
_STEP = typer.Option(
    "",
    "--step",
    metavar="WISP_ID",
    help="ephemeral wisp step to close after the metadata write succeeds",
)


class CheckpointError(RuntimeError):
    """An operator-facing validation or checkpoint-mutation failure."""


def checkpoint_lock_dir() -> Path:
    """Host-wide lock directory shared by every ``bh checkpoint`` process.

    The hive has one active write host, and parallel developer/releaser processes on that host
    need one shared namespace. System temp is the established location for this repo's flock
    coordination (see ``hub._aggregate_slot``); lock files contain no state and the kernel drops
    ownership on process death.
    """
    return Path(tempfile.gettempdir()) / "bh-checkpoint-locks"


@contextlib.contextmanager
def _checkpoint_lock(main: Path, bead_id: str, key: str):
    """Serialize one checkpoint key, or fail before the real command runs.

    This closes the read/check/write TOCTOU between helper invocations. It deliberately does not
    claim to make a raw ``bd update --metadata`` safe: raw bd is the convention bypass this
    bh-native surface exists to replace.
    """
    identity = f"{main.resolve()}\0{bead_id}\0{key}".encode()
    name = hashlib.sha256(identity).hexdigest()
    directory = checkpoint_lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / f"{name}.lock").open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CheckpointError(
                f"checkpoint {key} on {bead_id} is already running; no command was executed"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def parse_measurement(raw: str) -> dict:
    """Parse the required timestamped, concrete measurement shape."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckpointError(f"--value is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise CheckpointError("--value must be a JSON object")
    measured_at = value.get("measured_at")
    if not isinstance(measured_at, str) or not measured_at.strip():
        raise CheckpointError('--value must contain a non-empty string "measured_at"')
    try:
        parsed = dt.datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckpointError('"measured_at" must be an RFC 3339 timestamp') from exc
    if parsed.tzinfo is None:
        raise CheckpointError('"measured_at" must include a timezone')
    if not any(name != "measured_at" for name in value):
        raise CheckpointError('--value must include a concrete fact in addition to "measured_at"')
    return value


def _record(main: Path, bead_id: str) -> dict:
    record = bd.show(bead_id, main)
    if record is None:
        raise CheckpointError(f"no such persistent bead: {bead_id}")
    if record.get("ephemeral") is True:
        raise CheckpointError(f"checkpoint record must be persistent, not ephemeral: {bead_id}")
    return record


def _ensure_key_free(main: Path, bead_id: str, key: str) -> None:
    metadata = _record(main, bead_id).get("metadata") or {}
    if not isinstance(metadata, dict):
        raise CheckpointError(f"bead {bead_id} has malformed metadata")
    if key in metadata:
        raise CheckpointError(
            f"checkpoint key already exists on {bead_id}: {key} (checkpoint keys are append-only)"
        )


def _validate_step(main: Path, step_id: str) -> None:
    if not step_id:
        return
    step = bd.show(step_id, main)
    if step is None:
        raise CheckpointError(f"no such wisp step: {step_id}")
    if step.get("ephemeral") is not True:
        raise CheckpointError(f"--step must name an ephemeral wisp step: {step_id}")
    if step.get("status") == "closed":
        raise CheckpointError(f"wisp step is already closed: {step_id}")


def execute(
    main: Path,
    bead_id: str,
    key: str,
    measurement: dict,
    command: list[str],
    *,
    step_id: str = "",
) -> int:
    """Run ``command`` and couple successful completion to record-then-close mutations.

    Returns the command's non-zero status verbatim. Metadata is written before step closure, so a
    metadata failure can never falsely advance the wisp.
    """
    key = key.strip()
    if not key:
        raise CheckpointError("checkpoint key must not be empty")
    if not command:
        raise CheckpointError("a command is required after --")

    # The lock spans the operation. Locking only around either read still lets two processes pass
    # the post-command read and then shallow-merge the same key last-writer-wins.
    with _checkpoint_lock(main, bead_id, key):
        # Refuse before a potentially irreversible command when its fact cannot be appended.
        _ensure_key_free(main, bead_id, key)
        _validate_step(main, step_id)

        result = run(command, check=False, cwd=main)
        if result.returncode != 0:
            return result.returncode

        # A raw bd writer can bypass bh's lock while the command runs. The recheck catches an
        # already-visible value; only cooperating helper invocations receive the atomic guarantee.
        _ensure_key_free(main, bead_id, key)
        payload = json.dumps({key: measurement}, separators=(",", ":"), sort_keys=True)
        updated = bd.run(["update", bead_id, "--metadata", payload], main, capture=True)
        if updated.returncode != 0:
            raise CheckpointError(
                f"could not record checkpoint on {bead_id}: {bd.err_detail(updated)}"
            )

        if step_id:
            closed = bd.run(
                ["close", step_id, "--reason", f"checkpoint {key} command succeeded"],
                main,
                capture=True,
            )
            if closed.returncode != 0:
                raise CheckpointError(
                    f"checkpoint recorded, but could not close {step_id}: {bd.err_detail(closed)}"
                )
    return 0


@app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_checkpoint(
    ctx: typer.Context,
    bead: str = typer.Argument(..., metavar="BEAD", help="persistent bead receiving the fact"),
    key: str = typer.Argument(..., metavar="KEY", help="new, checkpoint-specific metadata key"),
    value: str = _VALUE,
    step: str = _STEP,
    hive: str = _HIVE,
):
    """Run COMMAND; on exit 0 append VALUE at KEY, then optionally close --step.

    Syntax: ``bh checkpoint run BEAD KEY --value JSON [--step WISP] -- COMMAND [ARG ...]``.
    """
    try:
        measurement = parse_measurement(value)
        main = registry.hive_dir_for(config.load(), hive)
        rc = execute(main, bead, key, measurement, list(ctx.args), step_id=step)
    except CheckpointError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    if rc:
        raise typer.Exit(rc)
    typer.echo(f"✓ recorded checkpoint {key} on {bead}" + (f"; closed {step}" if step else ""))
