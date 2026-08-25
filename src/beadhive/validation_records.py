"""Durable validation executions and decisions.

Runs are execution facts; uses are gate decisions.  They deliberately have different
identities so concurrent executions and repeated/reused decisions never overwrite one another.
All state lives below the shared git-private root, never in a verification checkout.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import signal
from pathlib import Path
from typing import Literal

from . import host, private_paths

Lifecycle = Literal["running", "completed", "abandoned"]
Verdict = Literal["green", "red", "none"]
PROTOCOL_NAME = "beadhive-validation-result"
PROTOCOL_VERSION = 1
PROTOCOL_CONFIG_V1 = f"{PROTOCOL_NAME}/v1"
PROTOCOL_RESULT_ENV = "BH_VALIDATION_RESULT_PATH"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(16)}"


def _validation_root(hive: str | Path, *, create: bool = False) -> Path | None:
    root = (
        private_paths.ensure_git_private_root(hive)
        if create
        else private_paths.git_private_root(hive)
    )
    return root / "validation" if root is not None else None


def begin_run(
    hive: str | Path,
    *,
    bead: str | None,
    phase: str,
    branch: str | None,
    worktree: str | Path | None,
    sha: str,
    tree: str,
    command_hash: str,
    command: str | None = None,
    owner_pid: int | None = None,
    owner_start: str | None = None,
) -> dict | None:
    """Allocate an independent running record using mkdir as the atomic claim."""
    root = _validation_root(hive, create=True)
    if root is None:
        return None
    for _ in range(16):
        run_id = _new_id("run")
        directory = root / "runs" / run_id
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        except OSError:
            return None
        pid = os.getpid() if owner_pid is None else owner_pid
        manifest = {
            "schema": 1,
            "run_id": run_id,
            "bead": bead,
            "phase": phase,
            "branch": branch,
            "worktree": str(Path(worktree).resolve()) if worktree else None,
            "sha": sha,
            "tree": tree,
            "command_hash": command_hash,
            "command": command,
            "owner": {
                "host": host.host_id(),
                "pid": pid,
                "start_token": owner_start,
            },
            "started_at": _now(),
            "finished_at": None,
            "lifecycle": "running",
            "verdict": "none",
            "exit_code": None,
            "signal": None,
            "reason": None,
        }
        try:
            _atomic_json(directory / "manifest.json", manifest)
            # Derived, reconstructable pointer.  It contains identity only, never lifecycle truth.
            _atomic_json(root / "active" / f"{run_id}.json", {"schema": 1, "run_id": run_id})
        except OSError:
            return None
        return manifest
    return None


def read_run(hive: str | Path, run_id: str) -> dict | None:
    root = _validation_root(hive)
    path = root / "runs" / run_id / "manifest.json" if root else None
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("run_id") == run_id else None


def finish_run(
    hive: str | Path,
    run_id: str,
    *,
    exit_code: int | None = None,
    signal_number: int | None = None,
    reason: str | None = None,
    protocol: dict | None = None,
) -> dict | None:
    """Atomically transition running to completed with a fail-closed typed verdict.

    Infrastructure/setup failures and interruptions have verdict ``none``.  An ordinary
    command is green only at exit zero and red otherwise.  A protocol may refine a nonzero
    result only when it is the exact trusted v1 schema; malformed or contradictory output is
    ignored and therefore cannot downgrade red.
    """
    current = read_run(hive, run_id)
    if current is None or current.get("lifecycle") != "running":
        return None
    verdict: Verdict
    if (
        signal_number is not None
        or exit_code is None
        or reason in {"missing_binary", "checkout_failure", "setup_failure", "interrupted"}
    ):
        verdict = "none"
    else:
        verdict = "green" if exit_code == 0 else "red"
    typed = parse_protocol(protocol, exit_code=exit_code)
    if typed is not None:
        verdict = typed["verdict"]
        reason = typed.get("reason") or reason
    current.update(
        lifecycle="completed",
        verdict=verdict,
        exit_code=exit_code,
        signal=signal_number,
        reason=reason,
        finished_at=_now(),
    )
    root = _validation_root(hive)
    if root is None:
        return None
    try:
        _atomic_json(root / "runs" / run_id / "manifest.json", current)
        (root / "active" / f"{run_id}.json").unlink(missing_ok=True)
    except OSError:
        return None
    return current


def parse_protocol(value: object, *, exit_code: int | None) -> dict | None:
    """Validate the explicitly versioned runner-result protocol.

    Schema: ``{"protocol":"beadhive-validation-result","version":1,
    "verdict":"green|red|none","reason":string|null}``.  Green is contradictory with a
    nonzero/missing exit and is rejected.  Unknown keys are rejected to keep v1 unambiguous.
    """
    if not isinstance(value, dict) or set(value) - {"protocol", "version", "verdict", "reason"}:
        return None
    if value.get("protocol") != PROTOCOL_NAME or value.get("version") != PROTOCOL_VERSION:
        return None
    verdict = value.get("verdict")
    reason = value.get("reason")
    if (
        verdict not in {"green", "red", "none"}
        or reason is not None
        and not isinstance(reason, str)
    ):
        return None
    if verdict == "green" and exit_code != 0:
        return None
    return {"verdict": verdict, "reason": reason}


def protocol_path(drop: Path, configured: object) -> Path | None:
    """Return the v1 drop path only for the exact explicit config value."""
    return Path(drop) / "validation-result.json" if configured == PROTOCOL_CONFIG_V1 else None


def read_protocol(path: Path | None) -> dict | None:
    """Read a regular JSON protocol drop; special/malformed/missing files are absent."""
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def abandon_run(hive: str | Path, run_id: str, *, reason: str = "owner_dead") -> dict | None:
    current = read_run(hive, run_id)
    if current is None or current.get("lifecycle") != "running":
        return None
    current.update(lifecycle="abandoned", verdict="none", reason=reason, finished_at=_now())
    root = _validation_root(hive)
    if root is None:
        return None
    try:
        _atomic_json(root / "runs" / run_id / "manifest.json", current)
        (root / "active" / f"{run_id}.json").unlink(missing_ok=True)
    except OSError:
        return None
    return current


def record_use(
    hive: str | Path,
    *,
    run_id: str,
    bead: str | None,
    phase: str,
    branch: str | None,
    worktree: str | Path | None,
    sha: str,
    tree: str,
    command_hash: str,
    reused: bool,
) -> dict | None:
    """Record one gate decision; reuse points at the original run without creating a run."""
    root = _validation_root(hive, create=True)
    if root is None or read_run(hive, run_id) is None:
        return None
    for _ in range(16):
        use_id = _new_id("use")
        path = root / "uses" / f"{use_id}.json"
        if path.exists():
            continue
        value = {
            "schema": 1,
            "use_id": use_id,
            "run_id": run_id,
            "reused": bool(reused),
            "bead": bead,
            "phase": phase,
            "branch": branch,
            "worktree": str(Path(worktree).resolve()) if worktree else None,
            "sha": sha,
            "tree": tree,
            "command_hash": command_hash,
            "created_at": _now(),
        }
        try:
            # Exclusive create prevents the vanishingly unlikely id collision from overwriting.
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
        except FileExistsError:
            continue
        except OSError:
            return None
        return value
    return None


def signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except (ValueError, TypeError):
        return str(number)


def completed_run(hive: str | Path, *, tree: str, command_hash: str) -> dict | None:
    """Newest completed run for an exact tree/command identity, or ``None``."""
    root = _validation_root(hive)
    directory = root / "runs" if root else None
    if directory is None or not directory.is_dir():
        return None
    matches = []
    for child in directory.iterdir():
        value = read_run(hive, child.name)
        if (
            value is not None
            and value.get("lifecycle") == "completed"
            and value.get("tree") == tree
            and value.get("command_hash") == command_hash
        ):
            matches.append(value)
    return max(matches, key=lambda item: str(item.get("finished_at") or ""), default=None)


def migrate_legacy_active(hive: str | Path) -> int:
    """Import 0.15.1 worktree-keyed markers as derived pointers only.

    A marker is linked only when its embedded ``run_id`` names a running manifest with matching
    ownership.  Otherwise it is ignored: active pointers can never manufacture or contradict
    authoritative lifecycle state.
    """
    root = _validation_root(hive, create=True)
    if root is None:
        return 0
    active = root / "active"
    if not active.is_dir():
        return 0
    migrated = 0
    for path in list(active.iterdir()):
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        run_id = value.get("run_id") if isinstance(value, dict) else None
        if not isinstance(run_id, str) or path.name == f"{run_id}.json":
            continue
        manifest = read_run(hive, run_id)
        if manifest is None or manifest.get("lifecycle") != "running":
            continue
        owner = manifest.get("owner", {})
        if any(
            value.get(k) != owner.get(target)
            for k, target in (("host", "host"), ("pid", "pid"), ("pid_start", "start_token"))
        ):
            continue
        try:
            _atomic_json(active / f"{run_id}.json", {"schema": 1, "run_id": run_id})
            path.unlink()
        except OSError:
            continue
        migrated += 1
    return migrated
