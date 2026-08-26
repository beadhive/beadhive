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
import shutil
import signal
from pathlib import Path
from typing import Literal

from . import host, observaloop_env, private_paths

Lifecycle = Literal["running", "completed", "abandoned"]
Verdict = Literal["green", "red", "none"]
PROTOCOL_NAME = "beadhive-validation-result"
PROTOCOL_VERSION = 1
PROTOCOL_CONFIG_V1 = f"{PROTOCOL_NAME}/v1"
PROTOCOL_RESULT_ENV = "BH_VALIDATION_RESULT_PATH"
INFRASTRUCTURE_REASONS = frozenset(
    {"missing_binary", "checkout_failure", "setup_failure", "interrupted", "owner_dead"}
)


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
    hive = Path(hive)
    root = (
        private_paths.ensure_git_private_root(hive)
        if create
        else private_paths.git_private_root(hive)
    )
    return root / "validation" if root is not None else None


def artifact_root(hive: str | Path, configured: object = None) -> Path | None:
    """Absolute root for relocatable raw validation artifacts.

    Environment wins over config so CI can upload a run directory from a mounted
    volume. Relative values are rejected rather than acquiring accidental cwd
    semantics; the caller treats that as a validation setup error.
    """
    raw = os.environ.get("BH_VALIDATION_ARTIFACT_ROOT") or configured or ""
    if raw:
        root = Path(str(raw)).expanduser()
        if not root.is_absolute():
            raise ValueError("validation artifact root must be absolute")
        return root
    root = private_paths.ensure_repo_private_root(hive)
    return root / "validation" / "runs" if root is not None else None


def artifact_paths(hive: str | Path, run_id: str, configured: object = None) -> dict | None:
    """Allocate one fresh complete artifact directory for a run, or safely miss."""
    root = artifact_root(hive, configured)
    if root is None:
        return None
    directory = root / run_id
    reports = directory / "reports"
    try:
        reports.mkdir(parents=True, exist_ok=False)
    except (FileExistsError, OSError):
        return None
    # The default lives in the primary checkout so artifact retention survives
    # verify-worktree removal; keep that private root out of ordinary git status.
    if not (os.environ.get("BH_VALIDATION_ARTIFACT_ROOT") or configured):
        observaloop_env._git_exclude(Path(hive), ".bh/")
    return {
        "directory": str(directory),
        "reports": str(reports),
        "gate_log": str(directory / "gate.log"),
    }


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
    artifact_root_config: object = None,
    admission: dict | None = None,
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
        try:
            artifacts = artifact_paths(hive, run_id, artifact_root_config)
        except ValueError:
            # A relative configured root is explicitly invalid; leave no partial
            # control directory that could be mistaken for a live run.
            try:
                directory.rmdir()
            except OSError:
                pass
            return None
        if artifacts is None:
            try:
                directory.rmdir()
            except OSError:
                pass
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
            "admission": {
                "slot": admission.get("slot"),
                "queue_seconds": admission.get("queue_seconds"),
            }
            if isinstance(admission, dict)
            else None,
            "artifacts": artifacts,
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


def mark_artifacts_uploaded(hive: str | Path, run_id: str) -> dict | None:
    """Record CI handoff, then retain only the newest raw artifact set per tree."""
    current = read_run(hive, run_id)
    if current is None:
        return None
    current["artifacts_uploaded_at"] = _now()
    root = _validation_root(hive)
    try:
        _atomic_json(root / "runs" / run_id / "manifest.json", current)
    except OSError:
        return None
    prune_artifacts(hive)
    return current


def attach_summary(hive: str | Path, run_id: str, summary: dict) -> dict | None:
    """Attach bounded report metadata to a run without copying raw artifacts."""
    current = read_run(hive, run_id)
    root = _validation_root(hive)
    if current is None or root is None:
        return None
    current["summary"] = summary
    try:
        _atomic_json(root / "runs" / run_id / "manifest.json", current)
    except OSError:
        return None
    return current


def prune_artifacts(hive: str | Path) -> int:
    """Apply bounded raw-artifact retention without deleting control manifests.

    A running run, a run still referenced by a gate-use, and the newest raw
    directory for a tree with retry/red history are protected.  After an upload
    handoff, superseded raw reports/logs are removable; manifests remain as the
    small durable execution history.
    """
    root = _validation_root(hive)
    runs_dir = root / "runs" if root else None
    if runs_dir is None or not runs_dir.is_dir():
        return 0
    runs = [read_run(hive, p.name) for p in runs_dir.iterdir() if p.is_dir()]
    runs = [r for r in runs if r]
    # Uses are an audit trail, not a raw-artifact retention lease: every ordinary
    # completed execution creates one, so treating them as permanent references
    # makes cleanup a no-op. The bounded verdict index is the actual live decision
    # index and (when it names a run) is the only decision reference that protects
    # raw artifacts after CI has acknowledged upload.
    referenced = _verdict_run_ids(root)
    if referenced is None:
        return 0
    by_tree: dict[str, list[dict]] = {}
    for run in runs:
        by_tree.setdefault(str(run.get("tree") or ""), []).append(run)
    keep: set[str] = set(referenced)
    for group in by_tree.values():
        group.sort(key=_run_order_key)
        if any(r.get("lifecycle") == "running" for r in group):
            keep.update(r["run_id"] for r in group if r.get("lifecycle") == "running")
        # Red or repeated same-tree history earns the newest uploadable raw set.
        if len(group) > 1 or any(r.get("verdict") == "red" for r in group):
            keep.add(group[-1]["run_id"])
    removed = 0
    for run in runs:
        artifacts = run.get("artifacts") or {}
        raw_directory = artifacts.get("directory")
        directory = (
            Path(raw_directory) if isinstance(raw_directory, str) and raw_directory else None
        )
        if (
            run["run_id"] not in keep
            and run.get("lifecycle") != "running"
            and run.get("artifacts_uploaded_at")
            and directory is not None
            and directory.is_dir()
        ):
            run["artifacts"] = {"pruned_at": _now()}
            try:
                _atomic_json(runs_dir / run["run_id"] / "manifest.json", run)
            except OSError:
                continue
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    return removed


def _verdict_run_ids(root: Path) -> set[str] | None:
    """Read canonical verdict pointers; malformed pointers fail retention closed."""
    verdicts = root / "verdicts"
    if not verdicts.is_dir():
        return set()
    referenced: set[str] = set()
    for path in verdicts.rglob("*.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        run_id = value.get("run_id") if isinstance(value, dict) else None
        if not isinstance(run_id, str) or not run_id:
            return None
        referenced.add(run_id)
    return referenced


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
    coalesced: bool = False,
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
            "coalesced": bool(coalesced),
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


def is_completed_verdict(value: object) -> bool:
    """Whether ``value`` is a structurally authentic completed execution verdict.

    JSON booleans are integers in Python, so exact ``type(...) is int`` checks are deliberate:
    ``schema: true`` and ``exit_code: false`` are malformed records, never v1/exit-zero facts.
    """
    if not isinstance(value, dict):
        return False
    exit_code = value.get("exit_code")
    signal_number = value.get("signal")
    reason = value.get("reason")
    return (
        type(value.get("schema")) is int
        and value.get("schema") == 1
        and isinstance(value.get("run_id"), str)
        and bool(value.get("run_id"))
        and value.get("lifecycle") == "completed"
        and value.get("verdict") in {"green", "red", "none"}
        and (exit_code is None or type(exit_code) is int)
        and (signal_number is None or type(signal_number) is int)
        and (reason is None or isinstance(reason, str))
    )


def is_qualifying_green(value: object) -> bool:
    """The one exact predicate that may authorize validation reuse."""
    if not is_completed_verdict(value):
        return False
    assert isinstance(value, dict)  # narrowed by is_completed_verdict
    return (
        value.get("verdict") == "green"
        and type(value.get("exit_code")) is int
        and value.get("exit_code") == 0
        and value.get("signal") is None
        and value.get("reason") not in INFRASTRUCTURE_REASONS
    )


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
    return max(matches, key=_run_order_key, default=None)


def matching_runs(hive: str | Path, *, tree: str, command_hash: str) -> list[dict]:
    """Every readable manifest for one exact verdict identity.

    The verdict index is derived state, so reconstruction must see red, none, and abandoned
    executions too: a later non-green execution revokes an earlier green pointer.
    """
    root = _validation_root(hive)
    directory = root / "runs" if root else None
    if directory is None or not directory.is_dir():
        return []
    return [
        value
        for child in sorted(directory.iterdir(), key=lambda path: path.name)
        if (value := read_run(hive, child.name)) is not None
        and value.get("tree") == tree
        and value.get("command_hash") == command_hash
    ]


def latest_run(hive: str | Path, *, tree: str, command_hash: str) -> dict | None:
    """Newest execution fact for an exact identity, regardless of lifecycle/verdict."""
    return max(
        matching_runs(hive, tree=tree, command_hash=command_hash),
        key=_run_order_key,
        default=None,
    )


def running_runs(
    hive: str | Path, *, bead: str | None = None, tree: str | None = None
) -> list[dict]:
    """Readable active executions, optionally narrowed to one submit subject."""
    root = _validation_root(hive)
    directory = root / "runs" if root else None
    if directory is None or not directory.is_dir():
        return []
    result = []
    for child in directory.iterdir():
        value = read_run(hive, child.name)
        if value is None or value.get("lifecycle") != "running":
            continue
        if bead is not None and value.get("bead") != bead:
            continue
        if tree is not None and value.get("tree") != tree:
            continue
        result.append(value)
    return sorted(result, key=_run_order_key)


def _run_order_key(item: dict) -> tuple[int, str, str]:
    """Stable authority-aware execution ordering, including deterministic ties.

    Imported compatibility history is retained and orders normally against other imported
    history, but it can never outrank an actual canonical execution for the same identity.  This
    second fence complements import-time clock normalization and also repairs stores migrated by
    an older build that retained a future source timestamp.
    """
    provenance = item.get("provenance")
    legacy = (
        isinstance(provenance, dict)
        and provenance.get("kind") == "legacy_import"
        or item.get("phase") in {"legacy-ledger-import", "legacy-triage-import"}
    )
    return (
        0 if legacy else 1,
        str(item.get("finished_at") or item.get("started_at") or ""),
        str(item.get("run_id") or ""),
    )


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
