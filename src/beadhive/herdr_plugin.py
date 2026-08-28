# ruff: noqa: E501
"""Optional, best-effort integration with the :command:`herdr` terminal server.

This initial plugin deliberately owns no worktree lifecycle.  ``bh`` remains the
authority for managed worktrees and branches; herdr is only an interactive
execution surface.  Every probe in this module is fenced: an absent executable,
stopped server, or failed subprocess must never make importing ``beadhive`` (or
the generic plugin registry) fail.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import typer

from . import config, operator_actions, plugins, run, worktree

_SESSION = "bh-supervisor"
_WARMUP_TOKEN = "BH_HERDR_WARMUP_OK"
_LAUNCH_SCHEMA = 1
_LIFECYCLE_SCHEMA = 1
_MAX_PROMPT_BYTES = 1024 * 1024
_ROSTER_SCHEMA = 1
_OWNERSHIP_MARKER = "bh.plugin.herdr/v1"
_METADATA_SOURCE = "beadhive"
_TOKEN_OWNER = "bh_owner"
_TOKEN_HIVE = "bh_hive_id"
_TOKEN_BEAD = "bh_bead_id"
_TOKEN_TARGET = "bh_target"
_TOKEN_SCHEMA = "bh_schema"
_HERDR_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _operation_id(value: str) -> str:
    """Validate a caller correlation ID or mint one without consulting durable state."""
    if not value:
        return f"op-{uuid.uuid4().hex}"
    if _OPERATION_ID_RE.fullmatch(value) is None:
        raise ValueError(
            "--operation-id must be 1-128 ASCII letters, digits, dot, underscore, colon, or dash"
        )
    return value


def _observed_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _lifecycle_payload(
    operation: str,
    disposition: str,
    *,
    operation_id: str,
    outcome: str = "succeeded",
    hive: str | None = None,
    bead: str | None = None,
    target: str | None = None,
    workspace: str | None = None,
    pane: str | None = None,
    worktree_path: str | None = None,
    capabilities: list[str] | None = None,
    warnings: list[str] | None = None,
    retained_resources: list[dict] | None = None,
    error: dict | None = None,
    **result,
) -> dict:
    """Build the shared additive v1 lifecycle receipt.

    Prompt and transcript content are intentionally not accepted by this builder.  Keeping the
    receipt vocabulary explicit makes it difficult for a future caller to accidentally serialize
    either one into stdout or an activity log.
    """
    from . import jsonout

    payload = {
        "operation_id": operation_id,
        "operation": operation,
        "outcome": outcome,
        "disposition": disposition,
        "observed_at": _observed_at(),
        "hive": hive,
        "bead": bead,
        "target": target,
        "session": _SESSION,
        "workspace": workspace,
        "pane": pane,
        "worktree": worktree_path,
        "capabilities": capabilities or [],
        "warnings": warnings or [],
        "retained_resources": retained_resources or [],
        **result,
    }
    if error is not None:
        payload["error"] = error
    return jsonout.envelope(f"plugin herdr {operation}", _LIFECYCLE_SCHEMA, payload)


def _emit_lifecycle(operation: str, disposition: str, *, operation_id: str, **fields) -> None:
    from . import jsonout

    jsonout.emit(_lifecycle_payload(operation, disposition, operation_id=operation_id, **fields))


def _lifecycle_failure(
    operation: str,
    *,
    operation_id: str,
    code: str,
    message: str,
    subsystem: str = "herdr",
    retryable: bool = False,
    disposition: str = "failed",
    exit_code: int = 1,
    **fields,
) -> None:
    """Emit one stable machine failure and preserve the caller-visible exit category."""
    _emit_lifecycle(
        operation,
        disposition,
        operation_id=operation_id,
        outcome="refused" if disposition == "refused" else "failed",
        error={
            "code": code,
            "message": message,
            "subsystem": subsystem,
            "retryable": retryable,
            "details": {},
        },
        **fields,
    )
    raise typer.Exit(exit_code)


def _has_cli() -> bool:
    """Whether the herdr executable is available without raising."""
    try:
        return shutil.which("herdr") is not None
    except Exception:  # noqa: BLE001 - optional integration availability probe
        return False


def _invoke(argv: list[str], *, timeout: float | None = None):
    """Run a read-only herdr probe, returning ``None`` for every failure."""
    try:
        kwargs = {"check": False, "capture": True}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return run.run(argv, **kwargs)
    except subprocess.TimeoutExpired:
        # Keep a bounded wait a clean, actionable CLI failure rather than leaking a traceback.
        return subprocess.CompletedProcess(
            argv, 124, stdout="", stderr=f"timed out after {timeout:g}s"
        )
    except Exception:  # noqa: BLE001 - server may be stopped or the process may fail
        return None


def server_up() -> bool:
    """True exactly when ``herdr status`` can reach a running server.

    The status command is herdr's authority for server health.  Its output is
    intentionally not reimplemented or parsed here, keeping this wrapper stable
    across herdr versions.
    """
    if not _has_cli():
        return False
    result = _invoke(["herdr", "status"])
    return result is not None and result.returncode == 0


def _output(result) -> str:
    """Best-effort text from a captured subprocess result."""
    return str(getattr(result, "stdout", "") or getattr(result, "stderr", "") or "").strip()


def _command(*args: str, timeout: float | None = None):
    """Run one session-scoped herdr command, fencing every external failure."""
    return _invoke(["herdr", "--session", _SESSION, *args], timeout=timeout)


def _session_socket_path() -> tuple[Path | None, str]:
    """Ask Herdr for the dedicated session socket without deriving private paths."""
    status = _invoke(["herdr", "--session", _SESSION, "status", "--json"])
    if status is None or status.returncode != 0:
        return None, _output(status) if status is not None else "status unavailable"
    try:
        payload = json.loads(str(status.stdout or ""))
    except (TypeError, ValueError):
        return None, "status returned invalid JSON"
    server = payload.get("server") if isinstance(payload, dict) else None
    path = server.get("socket") if isinstance(server, dict) else None
    if not isinstance(path, str) or not path:
        return None, "status response did not include the session socket"
    if server.get("running") is False:
        return None, "the bh-supervisor session is not running"
    return Path(path), ""


def _prompt_over_socket(target: str, prompt: str, *, timeout_ms: int = 60_000):
    """Submit sensitive prompt text over Herdr's local NDJSON socket.

    The ordinary CLI requires prompt text as a positional argument.  That is convenient for
    humans but exposes the text through process listings.  Safe stdin/file dispatch therefore
    uses Herdr's documented raw ``agent.prompt`` method.  No subprocess argv, environment,
    receipt, or error contains the prompt body.
    """
    path, detail = _session_socket_path()
    argv = ["herdr", "<local-socket>", "agent.prompt", target]
    if path is None:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=detail)
    request = {
        "id": f"bh_prompt_{uuid.uuid4().hex}",
        "method": "agent.prompt",
        "params": {
            "target": target,
            "text": prompt,
            "wait": {"until": ["idle", "done", "blocked"], "timeout_ms": timeout_ms},
        },
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout((timeout_ms / 1000) + 5)
            client.connect(str(path))
            client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
            with client.makefile("rb") as stream:
                response = stream.readline(1024 * 1024 + 1)
        if not response or len(response) > 1024 * 1024:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="Herdr returned an empty or oversized response"
            )
        decoded = json.loads(response)
    except TimeoutError:
        return subprocess.CompletedProcess(
            argv, 124, stdout="", stderr=f"timed out after {timeout_ms / 1000:g}s"
        )
    except (OSError, TypeError, ValueError) as exc:
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr=f"local Herdr socket request failed: {exc}"
        )
    if not isinstance(decoded, dict) or decoded.get("id") != request["id"]:
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Herdr returned a mismatched agent.prompt response"
        )
    if decoded.get("error") is not None:
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Herdr refused the agent.prompt request"
        )
    result = decoded.get("result")
    if (
        not isinstance(result, dict)
        or result.get("type") != "agent_prompt"
        or result.get("agent_status") not in {"idle", "done", "blocked"}
    ):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Herdr returned an invalid agent.prompt response"
        )
    return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(decoded), stderr="")


def _prompt_input(
    positional: str | None, *, from_stdin: bool, prompt_file: str
) -> tuple[str, str, bool]:
    """Read exactly one prompt source and report whether it uses the safe transport."""
    selected = int(positional is not None) + int(from_stdin) + int(bool(prompt_file))
    if selected != 1:
        raise ValueError("provide exactly one of PROMPT, --stdin, or --prompt-file PATH")
    if from_stdin:
        binary_stdin = getattr(sys.stdin, "buffer", None)
        if binary_stdin is not None:
            prompt = _decode_prompt_bytes(binary_stdin.read(_MAX_PROMPT_BYTES + 1))
        else:
            prompt = sys.stdin.read(_MAX_PROMPT_BYTES + 1)
        source = "stdin"
        safe = True
    elif prompt_file:
        try:
            with Path(prompt_file).open("rb") as stream:
                prompt = _decode_prompt_bytes(stream.read(_MAX_PROMPT_BYTES + 1))
        except OSError as exc:
            raise ValueError(f"could not read --prompt-file: {exc}") from exc
        source = "file"
        safe = True
    else:
        prompt = positional or ""
        source = "argument"
        safe = False
    if not prompt:
        raise ValueError("prompt input must not be empty")
    try:
        encoded = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("prompt input must be valid UTF-8") from exc
    if len(encoded) > _MAX_PROMPT_BYTES:
        raise ValueError(f"prompt input exceeds the {_MAX_PROMPT_BYTES}-byte limit")
    return prompt, source, safe


def _decode_prompt_bytes(raw: bytes) -> str:
    """Decode at most one bounded prompt, rejecting invalid UTF-8 without replacement."""
    if len(raw) > _MAX_PROMPT_BYTES:
        raise ValueError(f"prompt input exceeds the {_MAX_PROMPT_BYTES}-byte limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("prompt input must be valid UTF-8") from exc


def _require(result, action: str):
    """Return successful output or turn an optional-tool failure into a clear CLI error."""
    if result is None or result.returncode != 0:
        detail = _output(result) if result is not None else "herdr did not start"
        typer.echo(f"✗ herdr {action} failed: {detail or 'server unavailable'}", err=True)
        raise typer.Exit(1)
    return _output(result)


def _decoded(result):
    """Decode one Herdr JSON response, retaining legacy plain-text output."""
    output = _output(result)
    try:
        return json.loads(output)
    except (TypeError, ValueError):
        return output


def _response_error(action: str, field: str) -> None:
    """Report a successful but unusable Herdr response without leaking a traceback."""
    typer.echo(f"✗ herdr {action} failed: response missing {field}", err=True)
    raise typer.Exit(1)


def _result_payload(value):
    """Unwrap Herdr's ``{id, result}`` protocol envelope when present."""
    if isinstance(value, dict) and isinstance(value.get("result"), dict):
        return value["result"]
    return value


def _string_field(value, *keys: str) -> str | None:
    """Read the first non-empty string field from one response record."""
    if not isinstance(value, dict):
        return None
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item
    return None


def _required_id(result, action: str, record: str, *keys: str) -> str:
    """Extract an ID from a structured Herdr response or a legacy bare-ID response."""
    value = _decoded(result)
    if isinstance(value, str) and value:
        return value
    payload = _result_payload(value)
    candidate = payload.get(record) if isinstance(payload, dict) else None
    identifier = _string_field(candidate, *keys)
    if identifier is None:
        _response_error(action, keys[0])
    return identifier


def _workspace_from_snapshot(label: str) -> tuple[str, str] | None:
    """Find a previously-created bh workspace by its visible label, if snapshot is usable."""
    result = _command("api", "snapshot")
    if result is None or result.returncode != 0:
        return None
    data = _result_payload(_decoded(result))
    if isinstance(data, dict) and isinstance(data.get("snapshot"), dict):
        data = data["snapshot"]
    if not isinstance(data, dict):
        return None

    workspaces = data.get("workspaces")
    if not isinstance(workspaces, list):
        return None
    workspace = next(
        (item for item in workspaces if isinstance(item, dict) and item.get("label") == label),
        None,
    )
    if workspace is None:
        return None
    workspace_id = _string_field(workspace, "workspace_id", "id")
    if workspace_id is None:
        _response_error("workspace lookup", "workspace_id")

    pane_id = _string_field(workspace, "root_pane_id", "focused_pane_id", "pane_id")
    layouts = data.get("layouts")
    if pane_id is None and isinstance(layouts, list):
        layout = next(
            (
                item
                for item in layouts
                if isinstance(item, dict) and item.get("workspace_id") == workspace_id
            ),
            None,
        )
        pane_id = _string_field(layout, "focused_pane_id", "root_pane_id", "pane_id")
    panes = data.get("panes")
    if pane_id is None and isinstance(panes, list):
        pane = next(
            (
                item
                for item in panes
                if isinstance(item, dict) and item.get("workspace_id") == workspace_id
            ),
            None,
        )
        pane_id = _string_field(pane, "pane_id", "id")
    if pane_id is None:
        _response_error("workspace lookup", "pane_id")
    return workspace_id, pane_id


def _workspace(hive: str, cwd: Path) -> tuple[str, str]:
    """Reuse the hive's isolated workspace, or create it without focusing the operator's TTY."""
    label = f"bh:{hive}"
    if existing := _workspace_from_snapshot(label):
        return existing
    result = _command("workspace", "create", "--cwd", str(cwd), "--label", label, "--no-focus")
    output = _require(result, "workspace create")
    decoded = _decoded(result)
    if isinstance(decoded, str):
        return output, f"{output}:p1"
    payload = _result_payload(decoded)
    workspace = payload.get("workspace") if isinstance(payload, dict) else None
    root_pane = payload.get("root_pane") if isinstance(payload, dict) else None
    if root_pane is None and isinstance(payload, dict):
        root_pane = payload.get("pane")
    workspace_id = _string_field(workspace, "workspace_id", "id")
    pane_id = _string_field(root_pane, "pane_id", "id")
    if workspace_id is None:
        _response_error("workspace create", "workspace_id")
    if pane_id is None:
        _response_error("workspace create", "pane_id")
    return workspace_id, pane_id


def _managed_worktree(hive: str, bead: str, cfg=None) -> tuple[dict, Path]:
    """Resolve, but never provision, a claimed native bh worktree for ``bead``."""
    cfg = cfg if cfg is not None else config.load()
    entry, _main, target, _branch = worktree.locate(cfg, hive, bead)
    if not target.is_dir() or not (target / ".git").exists():
        typer.echo(f"✗ no managed bh worktree for {bead} in {hive} — claim it first", err=True)
        raise typer.Exit(1)
    return entry, target


def _close_pane(pane: str) -> None:
    """Best-effort cleanup for a failed spawn; never mask the original failure."""
    _invoke(["herdr", "--session", _SESSION, "pane", "close", pane, "--no-focus"])


def _hive_id(entry: dict) -> str:
    """The stable registry identity used in launch output and Herdr workspace labels."""
    return "/".join(str(entry[key]) for key in ("provider", "org", "repo"))


def _launch_target(bead: str) -> str:
    """Return one deterministic, Herdr-valid name for every Beads ID.

    Keep the established ``bh-<bead>`` spelling whenever Herdr accepts it. Dotted child IDs
    and long IDs need encoding; their readable stem is paired with a digest of the *original*
    value so normalization and truncation cannot collide.
    """
    legacy = f"bh-{bead}"
    if _HERDR_NAME_RE.fullmatch(legacy):
        return legacy
    digest = hashlib.sha256(bead.encode()).hexdigest()[:16]
    stem = re.sub(r"[^a-z0-9_-]+", "-", bead.lower()).strip("-_") or "bead"
    room = 32 - len("bh--") - len(digest)
    return f"bh-{stem[:room]}-{digest}"


def _integration_ready(kind: str) -> tuple[bool, str]:
    """Read Herdr's integration status without installing or changing anything."""
    result = _invoke(["herdr", "integration", "status"])
    if result is None or result.returncode != 0:
        return False, _output(result) if result is not None else "status unavailable"
    for line in _output(result).splitlines():
        name, separator, state = line.partition(":")
        if separator and name.strip() == kind:
            status = state.strip().lower()
            return ("not installed" not in status), state.strip()
    return False, f"{kind} was not reported"


def _session_snapshot():
    """Use or create the dedicated session noninteractively and return its snapshot."""
    result = _command("api", "snapshot")
    if result is None or result.returncode != 0:
        return None
    data = _result_payload(_decoded(result))
    if isinstance(data, dict) and isinstance(data.get("snapshot"), dict):
        data = data["snapshot"]
    return data if isinstance(data, dict) else None


def _snapshot_agent_records(snapshot: dict) -> list[dict]:
    """Join snapshot agent, pane, and workspace facts for strict reuse proof."""
    panes = {
        str(item.get("pane_id") or item.get("id")): item
        for item in snapshot.get("panes", [])
        if isinstance(item, dict) and (item.get("pane_id") or item.get("id"))
    }
    workspaces = {
        str(item.get("workspace_id") or item.get("id")): item
        for item in snapshot.get("workspaces", [])
        if isinstance(item, dict) and (item.get("workspace_id") or item.get("id"))
    }
    raw = snapshot.get("agents")
    if not isinstance(raw, list):
        raw = _agent_records(snapshot, unique_by_name=False)
    records: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        record = dict(item)
        if isinstance(item.get("agent"), dict):
            record.update(item["agent"])
        pane_id = _record_pane_id(record)
        pane = panes.get(pane_id or "", {})
        workspace_id = record.get("workspace_id") or (
            pane.get("workspace_id") if isinstance(pane, dict) else None
        )
        workspace = workspaces.get(str(workspace_id or ""), {})
        if pane:
            record["pane_record"] = pane
        if workspace:
            record["workspace_record"] = workspace
        records.append(record)
    return records


def _metadata_tokens(record: dict) -> dict[str, str]:
    """Return Herdr's effective metadata tokens for a joined live record."""
    tokens: dict[str, str] = {}
    for source in (
        record.get("workspace_record"),
        record.get("pane_record"),
        record.get("pane"),
        record,
    ):
        if not isinstance(source, dict):
            continue
        raw = source.get("tokens")
        if isinstance(raw, dict):
            tokens.update({str(key): str(value) for key, value in raw.items() if value is not None})
    return tokens


def _tag_ownership(workspace: str, pane: str, hive: str, bead: str, target: str) -> None:
    """Persist explicit live correlation in Herdr-owned presentation metadata.

    The target remains opaque and length-bounded.  These tokens are the reversible association;
    the roster never attempts to decode a hashed target name.
    """
    workspace_result = _command(
        "workspace",
        "report-metadata",
        workspace,
        "--source",
        _METADATA_SOURCE,
        "--token",
        f"{_TOKEN_OWNER}={_OWNERSHIP_MARKER}",
        "--token",
        f"{_TOKEN_HIVE}={hive}",
        "--token",
        f"{_TOKEN_SCHEMA}={_ROSTER_SCHEMA}",
    )
    _require(workspace_result, "workspace ownership metadata")
    pane_result = _command(
        "pane",
        "report-metadata",
        pane,
        "--source",
        _METADATA_SOURCE,
        "--token",
        f"{_TOKEN_OWNER}={_OWNERSHIP_MARKER}",
        "--token",
        f"{_TOKEN_HIVE}={hive}",
        "--token",
        f"{_TOKEN_BEAD}={bead}",
        "--token",
        f"{_TOKEN_TARGET}={target}",
        "--token",
        f"{_TOKEN_SCHEMA}={_ROSTER_SCHEMA}",
    )
    _require(pane_result, "pane ownership metadata")


def _value(record: dict, *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _same_path(left: str | None, right: Path) -> bool:
    if not left:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return left == str(right)


def _action_capabilities(
    ownership_state: str, lifecycle_state: str, reason: str
) -> dict[str, dict[str, str]]:
    """Advertise only operations that the ownership gates can safely target."""
    availability, _reason_code, detail = operator_actions.agent_action_availability(
        ownership_state, lifecycle_state, reason
    )
    # This pre-existing compatibility field has a two-state vocabulary.  Rich
    # policy facts live in advertised_actions below.
    legacy_availability = "allowed" if availability == "allowed" else "unavailable"
    return {
        action: {
            "availability": legacy_availability,
            "reason": detail,
        }
        for action in ("attach", "dispatch", "watch", "reap")
    }


def _roster_agent(
    record: dict,
    cfg: dict,
    target_count: dict[str, int],
    pane_count: dict[str, int],
    managed_paths: dict[str, str],
    *,
    advertised_at: int,
) -> dict:
    """Project one joined Herdr record into the versioned correlation contract."""
    target = _value(record, "name", "agent_name", "target") or ""
    state = (
        _value(record, "state", "status", "agent_status", "lifecycle", "lifecycle_state")
        or "unknown"
    ).lower()
    pane_id = _record_pane_id(record)
    pane_name = _snapshot_value(record, "pane_name", "pane_label", "label", "title")
    workspace_id = _snapshot_value(record, "workspace_id")
    workspace_label = _snapshot_value(record, "workspace_label")
    if workspace_label is None and isinstance(record.get("workspace_record"), dict):
        workspace_label = _string_field(record["workspace_record"], "label", "name")
    cwd = _snapshot_value(record, "cwd", "working_directory", "current_dir", "foreground_cwd")
    tab_id = _snapshot_value(record, "tab_id")
    tokens = _metadata_tokens(record)

    marker = tokens.get(_TOKEN_OWNER)
    hive = tokens.get(_TOKEN_HIVE)
    bead = tokens.get(_TOKEN_BEAD)
    association = "metadata" if marker == _OWNERSHIP_MARKER else "none"
    if association == "none":
        match = _LEGACY_TARGET_RE.fullmatch(target)
        if match and isinstance(workspace_label, str) and workspace_label.startswith("bh:"):
            bead = match.group("bead")
            hive = workspace_label[3:]
            marker = "legacy-target-v0"
            association = "legacy"

    reason = "pane is not marked as bh-managed"
    ownership_state = "foreign"
    worktree_state = "unknown"
    branch = None
    expected: Path | None = None
    if association != "none" and hive and bead:
        try:
            _entry, _main, expected, branch = worktree.locate(cfg, hive, bead)
        except (KeyError, TypeError, ValueError, typer.Exit):
            reason = "ownership metadata names an unknown or ambiguous hive"
        else:
            expected_key = str(expected.resolve())
            inventory_branch = managed_paths.get(expected_key)
            exists = expected.is_dir() and inventory_branch is not None
            if inventory_branch is not None:
                branch = inventory_branch
            worktree_state = "available" if exists else "missing"
            exact_target = target == _launch_target(bead)
            exact_metadata_target = association == "legacy" or tokens.get(_TOKEN_TARGET) == target
            unique = (
                target_count.get(target, 0) == 1
                and bool(pane_id)
                and pane_count.get(pane_id or "", 0) == 1
            )
            workspace_matches = workspace_label == f"bh:{hive}"
            pane_matches = pane_name == target
            cwd_matches = _same_path(cwd, expected)
            if all(
                (
                    exact_target,
                    exact_metadata_target,
                    unique,
                    workspace_matches,
                    pane_matches,
                    cwd_matches,
                    exists,
                )
            ):
                ownership_state = "owned"
                reason = "explicit bh correlation and live resource identities agree"
            else:
                ownership_state = "stale"
                failed = []
                if not exists:
                    failed.append("managed worktree is missing")
                if not cwd_matches:
                    failed.append("pane cwd does not match the managed worktree")
                if not exact_target or not exact_metadata_target:
                    failed.append("target does not match its bead association")
                if not workspace_matches:
                    failed.append("workspace does not match the canonical hive")
                if not pane_matches:
                    failed.append("visible pane name does not match the target")
                if not unique:
                    failed.append("target or pane identity is ambiguous")
                reason = "; ".join(failed) or "ownership proof is stale"

    live = state in _LIVE_AGENT_STATES
    if ownership_state == "owned" and not live:
        ownership_state = "stale" if state != "unknown" else "unknown"
        reason = f"Herdr lifecycle state is {state}"

    revision_facts = [
        target,
        hive,
        bead,
        state,
        _value(record, "launched_at", "started_at", "created_at", "start_time"),
        _value(record, "active_at", "last_activity_at", "updated_at", "last_seen_at"),
        pane_id,
        pane_name,
        workspace_id,
        workspace_label,
        tab_id,
        ownership_state,
        marker,
        association,
        cwd,
        str(expected) if expected is not None else None,
        worktree_state,
        branch,
    ]
    revision = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(revision_facts, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
    )
    target_ref = {"hiveId": hive, "kind": "agent", "id": target}

    return {
        "revision": revision,
        "target": target or None,
        "hive": hive,
        "bead": bead,
        "lifecycle": {
            "state": state,
            "launched_at": _value(record, "launched_at", "started_at", "created_at", "start_time"),
            "active_at": _value(
                record, "active_at", "last_activity_at", "updated_at", "last_seen_at"
            ),
        },
        "worktree": {
            "path": str(expected) if expected is not None else cwd,
            "state": worktree_state,
            "branch": branch,
        },
        "presentation": {
            "session": _SESSION,
            "workspace": workspace_id,
            "workspace_label": workspace_label,
            "tab": tab_id,
            "pane": pane_id,
        },
        "ownership": {
            "marker": marker,
            "association": association,
            "state": ownership_state,
            "reason": reason,
        },
        "capabilities": _action_capabilities(ownership_state, state, reason),
        "advertised_actions": (
            operator_actions.agent_actions(
                target=target_ref,
                ownership_state=ownership_state,
                lifecycle_state=state,
                reason=reason,
                revision=revision,
                advertised_at=advertised_at,
                max_prompt_bytes=_MAX_PROMPT_BYTES,
            )
            if target
            else []
        ),
    }


def _roster_payload(snapshot: dict, cfg: dict | None = None, *, operation_id: str = "") -> dict:
    """Build one lifecycle receipt containing an atomic authoritative live roster."""
    cfg = cfg if cfg is not None else config.load()
    records = _snapshot_agent_records(snapshot)
    targets = [_value(record, "name", "agent_name", "target") or "" for record in records]
    panes = [_record_pane_id(record) or "" for record in records]
    target_count = {value: targets.count(value) for value in set(targets) if value}
    pane_count = {value: panes.count(value) for value in set(panes) if value}
    try:
        managed_paths = {
            str(Path(path).resolve()): branch for _prefix, path, branch in worktree.managed(cfg)
        }
    except (OSError, TypeError, ValueError):
        managed_paths = {}
    advertised_at = int(datetime.now(UTC).timestamp() * 1000)
    agents = [
        _roster_agent(
            record,
            cfg,
            target_count,
            pane_count,
            managed_paths,
            advertised_at=advertised_at,
        )
        for record in records
    ]
    agents.sort(
        key=lambda agent: (
            str(agent.get("target") or ""),
            str((agent.get("presentation") or {}).get("pane") or ""),
        )
    )
    revision_input = json.dumps(
        ["herdr-roster-v1", _SESSION, [agent["revision"] for agent in agents]],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    roster_revision = f"sha256:{hashlib.sha256(revision_input).hexdigest()}"
    payload = _lifecycle_payload(
        "ps",
        "listed" if agents else "empty",
        operation_id=_operation_id(operation_id),
        capabilities=["status", "ps"],
        revision=roster_revision,
        agents=agents,
        count=len(agents),
        authoritative_session=True,
        roster_schema_version=_ROSTER_SCHEMA,
    )
    payload["generated_at"] = payload["observed_at"]
    return payload


def _snapshot_value(record: dict, *keys: str):
    """Read a field from an agent, its joined pane, or its joined workspace."""
    for source in (
        record,
        record.get("pane") if isinstance(record.get("pane"), dict) else {},
        record.get("pane_record") if isinstance(record.get("pane_record"), dict) else {},
        record.get("workspace") if isinstance(record.get("workspace"), dict) else {},
        record.get("workspace_record") if isinstance(record.get("workspace_record"), dict) else {},
    ):
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _strict_live_target(target: str, hive: str, cwd: Path) -> tuple[str, str] | None:
    """Prove exact live target ownership, refusing ambiguous or conflicting records."""
    snapshot = _session_snapshot()
    if snapshot is None:
        raise RuntimeError("the bh-supervisor session became unavailable")
    matches = [
        record
        for record in _snapshot_agent_records(snapshot)
        if _agent_identity(record)[0] == target
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError(f"target {target!r} is ambiguous ({len(matches)} live records)")
    record = matches[0]
    state = _agent_identity(record)[3].lower()
    pane_id = _record_pane_id(record)
    pane_record = record.get("pane_record")
    pane_value = record.get("pane")
    pane_name = None
    if isinstance(pane_value, dict):
        pane_name = _string_field(pane_value, "name", "label", "title", "pane_name")
    if pane_name is None and isinstance(pane_record, dict):
        pane_name = _string_field(pane_record, "name", "label", "title", "pane_name")
    if pane_name is None:
        pane_name = _string_field(record, "pane_name", "pane_label", "pane_title")
    workspace_id = _snapshot_value(record, "workspace_id")
    workspace_label = _snapshot_value(record, "workspace_label")
    if workspace_label is None:
        workspace = record.get("workspace_record")
        if isinstance(workspace, dict):
            workspace_label = _string_field(workspace, "label", "name")
    working_dir = _snapshot_value(record, "cwd", "working_directory", "current_dir")
    same_cwd = False
    if working_dir:
        try:
            same_cwd = Path(working_dir).resolve() == cwd.resolve()
        except OSError:
            same_cwd = working_dir == str(cwd)
    if (
        state not in _LIVE_AGENT_STATES
        or not pane_id
        or pane_name != target
        or workspace_label != f"bh:{hive}"
        or not workspace_id
        or not same_cwd
    ):
        raise RuntimeError(
            f"target {target!r} exists but does not prove the requested live pane, "
            "workspace, and worktree ownership"
        )
    return workspace_id, pane_id


@dataclass(frozen=True)
class _LaunchResult:
    disposition: str
    hive: str
    bead: str
    kind: str
    worktree: Path
    workspace: str
    pane: str
    target: str

    def payload(self) -> dict:
        from . import jsonout

        return jsonout.envelope(
            "plugin herdr launch",
            _LAUNCH_SCHEMA,
            {
                "status": "ready",
                "disposition": self.disposition,
                "hive": self.hive,
                "bead": self.bead,
                "kind": self.kind,
                "worktree": str(self.worktree),
                "workspace": self.workspace,
                "pane": self.pane,
                "target": self.target,
            },
        )


def _launch_fail(stage: str, detail: str, *, claim=None, target: str = "") -> None:
    """Render one actionable failure, retaining native claim/worktree resources."""
    typer.echo(f"✗ herdr launch failed stage={stage}: {detail}", err=True)
    if claim is not None:
        typer.echo(
            f"  retained: bead={claim.bead.get('id', '')} claim={claim.disposition} "
            f"worktree={claim.worktree}",
            err=True,
        )
        typer.echo(f"  status: bh work issue {claim.bead.get('id', '')} --json", err=True)
        if target:
            typer.echo(f"  attach: bh plugin herdr attach {target}", err=True)
        typer.echo(f"  retry: bh plugin herdr launch {claim.bead.get('id', '')}", err=True)
    raise typer.Exit(1)


def _launch_warm(target: str) -> tuple[bool, str]:
    """Warm one newly-created target using spawn's established read-back proof."""
    prompt = _command(
        "agent",
        "prompt",
        target,
        f"Reply with exactly {_WARMUP_TOKEN}.",
        "--wait",
        "--timeout",
        "60000",
    )
    if prompt is None or prompt.returncode != 0:
        return False, f"agent warm-up: {_output(prompt) or 'server unavailable'}"
    read = _command("agent", "read", target, "--source", "visible", "--lines", "80")
    if read is None or read.returncode != 0:
        return False, f"agent read: {_output(read) or 'server unavailable'}"
    visible = _output(read)
    if _WARMUP_TOKEN in visible:
        return True, ""
    dismiss = _command("agent", "send-keys", target, "esc")
    if dismiss is None or dismiss.returncode != 0:
        return False, f"agent warm-up dismiss: {_output(dismiss) or 'server unavailable'}"
    retry = _command(
        "agent",
        "prompt",
        target,
        f"Reply with exactly {_WARMUP_TOKEN}.",
        "--wait",
        "--timeout",
        "60000",
    )
    if retry is None or retry.returncode != 0:
        return False, f"agent warm-up retry: {_output(retry) or 'server unavailable'}"
    read = _command("agent", "read", target, "--source", "visible", "--lines", "80")
    if read is None or read.returncode != 0:
        return False, f"agent read: {_output(read) or 'server unavailable'}"
    if _WARMUP_TOKEN not in _output(read):
        return False, "warm-up did not reach an idle agent prompt"
    return True, ""


cli = typer.Typer(no_args_is_help=True, help="herdr terminal/agent-pane integration.")


def _launch_kind(explicit: str | None, cfg, entry) -> str:
    """Resolve launch kind with stage-labelled failures and no external mutation."""
    kinds = supported_kinds()
    if not kinds:
        _launch_fail(
            "kind",
            "could not discover supported kinds; run `herdr agent start --help`",
        )
    configured = config.herdr_kind(cfg, entry)
    if explicit is not None:
        candidate, remedy = explicit, "pass a supported --kind value"
    elif configured is not None:
        candidate, remedy = configured, "change herdr.kind or pass --kind"
    else:
        harness = config.harness_name(cfg, entry)
        if harness in kinds:
            return harness
        if "claude" in kinds:
            return "claude"
        _launch_fail(
            "kind",
            f"no deterministic default; supported kinds: {', '.join(kinds)}; "
            "pass --kind KIND or configure herdr.kind",
        )
    if candidate not in kinds:
        _launch_fail(
            "kind",
            f"unsupported kind {candidate!r}; supported kinds: {', '.join(kinds)}; {remedy}",
        )
    return candidate


def _launch_lease(cfg, entry: dict, hive: str, adopt_expired: bool) -> None:
    """Apply launch's explicit, never-forced host-lease policy before claiming."""
    from . import guard

    state = guard.primary_state(hive, cfg=cfg, entry=entry)
    if state is None:
        return
    _prefix, this_host, lease = state
    if lease.held_by(this_host):
        return
    if not lease.is_expired():
        _launch_fail(
            "lease",
            f"active foreign host lease: {lease.describe()}; ask its holder to release it or "
            f"use the separate dangerous `bh host lease adopt {hive} --force` operation",
        )
    if not adopt_expired:
        _launch_fail(
            "lease",
            f"host lease is {lease.describe()}; retry with --adopt-expired to use the normal "
            "non-forced adoption path",
        )
    try:
        _adopt_expired_lease(cfg, entry)
    except Exception as exc:  # noqa: BLE001 - primitive has several typed refusal classes
        _launch_fail("lease", f"non-forced adoption failed: {exc}")


def _adopt_expired_lease(cfg, entry: dict):
    """Invoke the normal fence-then-lease adoption core without a force escape hatch."""
    from . import host, host_adopt, host_lease, hosts, registry

    hq_dir = config.hq_dir()
    git_probe = run.run(
        ["git", "-C", str(hq_dir), "rev-parse", "--git-dir"], check=False, capture=True
    )
    if git_probe.returncode != 0:
        raise RuntimeError(
            f"no Factory HQ clone at {hq_dir}; run `bh hq init` or `bh hq clone` first"
        )
    host_id = host.host_id()
    manifest = hosts.load(hq_dir, host_id)
    ttl = host_lease.ttl_for_role(manifest.role, config.host_lease_ttl(cfg))
    return host_adopt.adopt(
        prefix=str(entry["prefix"]),
        hive_remote="origin",
        hq_remote="origin",
        hive_cwd=registry.hive_dir(entry),
        hq_cwd=hq_dir,
        host_id=host_id,
        label=manifest.label,
        ttl=ttl,
        force=False,
    )


@cli.command(
    "launch",
    help=(
        "bh plugin herdr launch nvhack-lvxi --json\n\n"
        "Claim one exact bead and start or reuse its warm Herdr coding agent. The one-argument "
        "path discovers the hive and kind. All options are overrides: --hive disambiguates, "
        "--kind selects an installed integration, --as selects the developer identity, "
        "--adopt-expired uses only non-forced host adoption, --direction defaults right, "
        "--no-focus is the safe focus default, and --json is the agent contract. Herdr never "
        "creates or removes a worktree, never installs an integration, and never seizes an "
        "active foreign host lease."
    ),
)
def _launch_cmd(
    bead: str = typer.Argument(..., metavar="BEAD_ID", help="exact Beads issue ID"),
    hive: str = typer.Option(
        "", "--hive", help="explicit registered hive when exact bead lookup is ambiguous"
    ),
    kind: str | None = typer.Option(
        None, "--kind", help="Herdr kind; overrides per-hive/global herdr.kind defaults"
    ),
    as_: str = typer.Option(
        "", "--as", help="developer identity; otherwise use normal bh work claim precedence"
    ),
    adopt_expired: bool = typer.Option(
        False,
        "--adopt-expired",
        help="non-forcibly adopt only an expired or released host lease before claiming",
    ),
    direction: str = typer.Option(
        "right", "--direction", help="new pane direction: right (default) or down"
    ),
    focus: bool = typer.Option(
        False, "--focus/--no-focus", help="focus the new pane (safe default: --no-focus)"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="emit only the versioned launch result on stdout"
    ),
) -> None:
    """Launch the one-argument happy path.

    Example: ``bh plugin herdr launch nvhack-lvxi --json``

    The command performs read-only Herdr preflight first, then uses native ``bh work claim``
    ownership to provision the managed worktree. Herdr never creates or removes a worktree.
    Integration installation is explicit (``bh plugin herdr integrate KIND``), and an active
    foreign host lease is never seized. ``spawn`` remains the low-level command for callers
    that intentionally prepared the claim and worktree themselves.
    """
    from . import jsonout, registry

    # Import the lifecycle facade only when launch is actually invoked. The optional plugin is
    # statically reachable from public publish/export code through the plugin registry; making
    # the broad work facade a module dependency would falsely widen that read-only boundary to
    # work's aggregate intake modules even though publishing can never execute this command.
    work = importlib.import_module(".work", __package__)

    if direction not in {"right", "down"}:
        _launch_fail("input", "--direction must be `right` or `down`")
    if not _has_cli():
        _launch_fail("cli", "herdr CLI is not on PATH; install Herdr and retry")

    cfg = config.load()
    resolution = registry.resolve_bead_hive(cfg, bead, hive=hive)
    if resolution.ambiguous:
        candidates = ", ".join(_hive_id(entry) for entry in resolution.candidates)
        _launch_fail(
            "bead",
            f"bead {bead!r} is ambiguous across {candidates}; retry with --hive HIVE",
        )
    if not resolution.found or resolution.entry is None:
        suffix = f" in hive {hive!r}" if hive else ""
        _launch_fail(
            "bead", f"bead {bead!r} not found{suffix}; run `bh hive list` and verify the ID"
        )
    entry = resolution.entry
    resolved_hive = _hive_id(entry)
    resolved_kind = _launch_kind(kind, cfg, entry)
    integrated, integration_detail = _integration_ready(resolved_kind)
    if not integrated:
        _launch_fail(
            "integration",
            f"Herdr integration for {resolved_kind!r} is missing ({integration_detail}); "
            f"run `bh plugin herdr integrate {resolved_kind}`",
        )
    if _session_snapshot() is None:
        _launch_fail(
            "session",
            f"could not use or create the noninteractive {_SESSION!r} session; "
            "run `bh plugin herdr status`, then retry",
        )

    _launch_lease(cfg, entry, resolved_hive, adopt_expired)
    try:
        claim = work._claim_single_bead(cfg, resolved_hive, bead, as_)
    except Exception as exc:  # noqa: BLE001 - lifecycle core maps typed refusals to Typer exits
        detail = "native bh work claim refused the bead"
        if not isinstance(exc, typer.Exit) and str(exc):
            detail = str(exc)
        _launch_fail("claim", f"{detail}; inspect with `bh work issue {bead} --json`")

    target = _launch_target(bead)
    try:
        existing = _strict_live_target(target, resolved_hive, claim.worktree)
    except RuntimeError as exc:
        _launch_fail("reuse", str(exc), claim=claim, target=target)
    if existing is not None:
        result = _LaunchResult(
            "reused",
            resolved_hive,
            bead,
            resolved_kind,
            claim.worktree,
            existing[0],
            existing[1],
            target,
        )
    else:
        try:
            workspace, root_pane = _workspace(resolved_hive, claim.worktree)
        except Exception as exc:  # noqa: BLE001 - normalize optional-server failures by stage
            _launch_fail("workspace", str(exc) or "workspace unavailable", claim=claim)
        pane = ""
        split = _command(
            "pane",
            "split",
            "--pane",
            root_pane,
            "--direction",
            direction,
            "--cwd",
            str(claim.worktree),
            "--focus" if focus else "--no-focus",
        )
        if split is None or split.returncode != 0:
            _launch_fail(
                "pane", _output(split) or "pane split unavailable", claim=claim, target=target
            )
        try:
            pane = _required_id(split, "pane split", "pane", "pane_id", "id")
        except typer.Exit:
            _launch_fail("pane", "response missing pane_id", claim=claim, target=target)

        start = _command("agent", "start", target, "--kind", resolved_kind, "--pane", pane)
        if start is None or start.returncode != 0:
            detail = _output(start) or "agent start unavailable"
            duplicate = any(word in detail.lower() for word in ("duplicate", "already", "unique"))
            winner = None
            if duplicate:
                try:
                    winner = _strict_live_target(target, resolved_hive, claim.worktree)
                except RuntimeError:
                    winner = None
            if winner is not None and winner[1] != pane:
                _close_pane(pane)
                result = _LaunchResult(
                    "reused",
                    resolved_hive,
                    bead,
                    resolved_kind,
                    claim.worktree,
                    winner[0],
                    winner[1],
                    target,
                )
            else:
                _close_pane(pane)
                _launch_fail("startup", detail, claim=claim, target=target)
        else:
            rename = _command("pane", "rename", pane, target)
            if rename is None or rename.returncode != 0:
                _close_pane(pane)
                _launch_fail(
                    "startup",
                    f"pane rename: {_output(rename) or 'server unavailable'}",
                    claim=claim,
                    target=target,
                )
            try:
                _tag_ownership(workspace, pane, resolved_hive, bead, target)
            except typer.Exit:
                _close_pane(pane)
                _launch_fail(
                    "startup",
                    "could not record reversible bh ownership metadata",
                    claim=claim,
                    target=target,
                )
            warm, detail = _launch_warm(target)
            if not warm:
                _close_pane(pane)
                _launch_fail("warmup", detail, claim=claim, target=target)
            result = _LaunchResult(
                "created",
                resolved_hive,
                bead,
                resolved_kind,
                claim.worktree,
                workspace,
                pane,
                target,
            )

    payload = result.payload()
    if as_json:
        jsonout.emit(payload)
        return
    typer.echo(
        "herdr ready "
        + " ".join(
            f"{key}={payload[key]}"
            for key in (
                "disposition",
                "hive",
                "bead",
                "kind",
                "worktree",
                "workspace",
                "pane",
                "target",
            )
        )
    )


def supported_kinds() -> list[str]:
    """Discover agent kinds from the installed herdr CLI's own help text.

    Herdr owns this vocabulary and it can change independently of bh, so this
    deliberately does not keep a copied list.  Current clap-style help renders
    the values as ``[possible values: ...]``; accepting the older ``supported
    kinds: ...`` wording keeps the wrapper useful across nearby releases.
    """
    result = _invoke(["herdr", "agent", "start", "--help"])
    if result is None or result.returncode != 0:
        return []

    text = "\n".join(
        str(part or "") for part in (getattr(result, "stdout", ""), getattr(result, "stderr", ""))
    )
    values: list[str] = []
    for match in re.finditer(
        r"(?:possible values|supported (?:agent )?kinds?)\s*:\s*([^\n]+)",
        text,
        flags=re.IGNORECASE,
    ):
        values.extend(re.findall(r"[A-Za-z][A-Za-z0-9_-]*", match.group(1)))

    ignored = {"and", "or"}
    return list(dict.fromkeys(value for value in values if value.lower() not in ignored))


def _resolve_kind(explicit: str | None, cfg, entry) -> str:
    """Resolve a Herdr kind without allowing host-dependent guesswork.

    Configuration is intentionally open-ended because Herdr releases kinds
    independently.  Its discovered list is checked here, at the launch
    boundary, rather than during config validation or integration install.
    """
    kinds = supported_kinds()
    if not kinds:
        typer.echo(
            "herdr: could not determine supported agent kinds from 'herdr agent start --help'",
            err=True,
        )
        raise typer.Exit(1)

    configured = config.herdr_kind(cfg, entry)
    if explicit is not None:
        candidate, remedy = explicit, "choose a supported --kind value"
    elif configured is not None:
        candidate, remedy = configured, "change herdr.kind or pass --kind"
    else:
        harness = config.harness_name(cfg, entry)
        if harness in kinds:
            return harness
        if "claude" in kinds:
            return "claude"
        typer.echo(
            "herdr: no deterministic default kind; supported kinds: "
            f"{', '.join(kinds)}; pass --kind KIND or configure herdr.kind",
            err=True,
        )
        raise typer.Exit(2)

    if candidate not in kinds:
        typer.echo(
            f"herdr: unsupported agent kind {candidate!r}; supported kinds: {', '.join(kinds)}; "
            f"{remedy}",
            err=True,
        )
        raise typer.Exit(2)
    return candidate


# ``spawn`` reserves ``bh-<bead-id>``. Since bead ids themselves begin with
# ``bh-``, only the resulting ``bh-bh-*`` values are claimed. Full matching
# keeps names such as ``operator-bh-foo`` user-owned.
_BEAD_RE = re.compile(r"^bh-(?P<bead>bh-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)$")
_LEGACY_TARGET_RE = re.compile(r"^bh-(?P<bead>[a-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9]+)*)$")
_LIVE_AGENT_STATES = frozenset({"idle", "working", "blocked"})


def _record_pane_id(record: dict) -> str | None:
    """Extract a pane ID without requiring the optional visible pane label."""
    pane = record.get("pane")
    pane_id = record.get("pane_id")
    if isinstance(pane, str):
        pane_id = pane
    elif isinstance(pane, dict):
        pane_id = pane.get("id") or pane.get("pane_id") or pane_id
        nested = pane.get("pane")
        if not pane_id and isinstance(nested, dict):
            pane_id = nested.get("id") or nested.get("pane_id")
    return pane_id if isinstance(pane_id, str) and pane_id.strip() else None


def _agent_records(
    value, *, unique_by_name: bool = True, include_pane_claims: bool = False
) -> list[dict]:
    """Extract agent-shaped records from herdr's versioned JSON responses.

    ``ps`` requests the stable, deduplicated view.  ``reap`` intentionally
    retains distinct raw records so it can refuse duplicate agent-to-pane claims.
    A wrapper's sibling pane data is merged into its nested ``agent`` identity,
    then that child is skipped while walking to avoid a second logical record.
    """
    records: list[dict] = []

    def visit(item) -> None:
        if isinstance(item, dict):
            nested = item.get("agent")
            candidate = dict(item)
            if isinstance(nested, dict):
                candidate.update(nested)
            name = candidate.get("name") or candidate.get("agent_name") or candidate.get("target")
            state = (
                candidate.get("state")
                or candidate.get("status")
                or candidate.get("agent_status")
                or candidate.get("lifecycle")
                or candidate.get("lifecycle_state")
            )
            is_agent = isinstance(name, str) and isinstance(state, (str, int, float))
            is_pane_claim = include_pane_claims and _record_pane_id(candidate) is not None
            if is_agent or is_pane_claim:
                records.append(candidate)
            for key, child in item.items():
                if key == "agent" and isinstance(nested, dict):
                    continue
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    if not unique_by_name:
        return records
    unique: dict[str, dict] = {}
    for record in records:
        name = str(record.get("name") or record.get("agent_name") or record.get("target"))
        unique.setdefault(name, record)
    return list(unique.values())


def _agent_identity(record: dict) -> tuple[str, str | None, str | None, str]:
    """Return name, hive, bead, and lifecycle state from one herdr record."""
    name = str(record.get("name") or record.get("agent_name") or record.get("target"))
    bead_match = _BEAD_RE.search(name)
    bead = bead_match.group("bead") if bead_match else None
    hive = record.get("hive") or record.get("hive_id")
    if not hive:
        workspace = record.get("workspace") or record.get("workspace_label")
        if isinstance(workspace, dict):
            workspace = workspace.get("label") or workspace.get("name")
        if isinstance(workspace, str) and workspace.startswith("bh:"):
            hive = workspace[3:]
    return (
        name,
        str(hive) if hive else None,
        bead,
        str(
            record.get("state")
            or record.get("status")
            or record.get("agent_status")
            or record.get("lifecycle")
            or record.get("lifecycle_state")
        ),
    )


def _looks_like_agent_list(value) -> bool:
    """Recognize a successful (including empty) ``agent list --json`` payload."""
    if isinstance(value, list):
        return not value or all(isinstance(item, dict) for item in value)
    if not isinstance(value, dict):
        return False
    if isinstance(value.get("agents"), list):
        return True
    result = value.get("result")
    return (
        isinstance(result, dict)
        and result.get("type") == "agent_list"
        and isinstance(result.get("agents"), list)
    )


def _read_agents() -> list[dict] | None:
    """Read live agents, falling back to a snapshot when list is unsupported."""
    for argv in (("agent", "list", "--json"), ("agent", "list"), ("api", "snapshot")):
        result = _command(*argv)
        if result is None or result.returncode != 0:
            continue
        data = _decoded(result)
        if isinstance(data, str):
            continue
        records = _agent_records(data)
        if argv[:2] == ("agent", "list") and _looks_like_agent_list(data):
            return records
        if records or argv[-1] == "snapshot":
            return records
    return None


def _owned_live_agent(target: str) -> dict | None:
    """Return an agent only when the current roster proves live bh ownership."""
    snapshot = _session_snapshot()
    if snapshot is None:
        return None
    roster = _roster_payload(snapshot, config.load())
    matches = [agent for agent in roster["agents"] if agent.get("target") == target]
    if len(matches) != 1:
        return None
    agent = matches[0]
    ownership = agent.get("ownership") or {}
    lifecycle = agent.get("lifecycle") or {}
    if ownership.get("state") != "owned":
        return None
    lifecycle_state = str(lifecycle.get("state") or "unknown").lower()
    availability, _reason_code, _reason = operator_actions.agent_action_availability(
        str(ownership.get("state")), lifecycle_state, str(ownership.get("reason") or "")
    )
    if availability != "allowed":
        return None
    return agent


def _owned_live_pane(target: str) -> str | None:
    """Return the pane locator from the same current proof used by roster actions."""
    agent = _owned_live_agent(target)
    if agent is None:
        return None
    pane = agent.get("presentation") or {}
    pane_id = pane.get("pane")
    return str(pane_id) if pane_id else None


@cli.command("status", help="show herdr server health and installed agent integrations.")
def _status_cmd(
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    """A safe one-shot health report for herdr and its per-kind hook integrations."""
    try:
        op_id = _operation_id(operation_id)
    except ValueError as exc:
        if as_json:
            _lifecycle_failure(
                "status",
                operation_id="invalid",
                code="invalid_operation_id",
                message=str(exc),
                subsystem="beadhive",
                exit_code=2,
            )
        raise typer.BadParameter(str(exc), param_hint="--operation-id") from exc
    if not _has_cli():
        if as_json:
            _emit_lifecycle(
                "status",
                "unavailable",
                operation_id=op_id,
                capabilities=["status"],
                server={"available": False, "reason": "herdr_cli_unavailable"},
                integrations=[],
                warnings=["Herdr CLI is not on PATH."],
            )
            return
        typer.echo("herdr: server=down (herdr CLI not on PATH)")
        typer.echo("herdr integrations: unavailable")
        return

    status = _invoke(["herdr", "status"])
    server_available = status is not None and status.returncode == 0
    integrations = _invoke(["herdr", "integration", "status"])
    integration_rows = []
    if integrations is not None and integrations.returncode == 0:
        for line in _output(integrations).splitlines():
            kind, separator, state = line.partition(":")
            integration_rows.append(
                {"kind": kind.strip(), "state": state.strip()}
                if separator
                else {"kind": line.strip(), "state": "unknown"}
            )
    if as_json:
        capabilities = ["status"]
        if server_available:
            capabilities.extend(["ps", "spawn", "dispatch", "watch", "attach", "reap"])
        warnings = []
        if integrations is None or integrations.returncode != 0:
            warnings.append("Herdr integration status is unavailable.")
        _emit_lifecycle(
            "status",
            "available" if server_available else "unavailable",
            operation_id=op_id,
            capabilities=capabilities,
            server={"available": server_available, "detail": _output(status)},
            integrations=integration_rows,
            warnings=warnings,
        )
        return
    if status is None or status.returncode != 0:
        typer.echo("herdr: server=down")
    else:
        typer.echo("herdr: server=up")
        if output := _output(status):
            typer.echo(output)
    if integrations is None or integrations.returncode != 0:
        typer.echo("herdr integrations: unavailable")
    else:
        typer.echo("herdr integrations:")
        if output := _output(integrations):
            typer.echo(output)


@cli.command("ps", help="list live herdr agents and their bh identity.")
def _ps_cmd(
    as_json: bool = typer.Option(
        False, "--json", help="emit a versioned lifecycle receipt with the live roster"
    ),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    """Show live agents without maintaining a bh-side identity table."""
    try:
        op_id = _operation_id(operation_id)
    except ValueError as exc:
        if as_json:
            _lifecycle_failure(
                "ps",
                operation_id="invalid",
                code="invalid_operation_id",
                message=str(exc),
                subsystem="beadhive",
                exit_code=2,
            )
        raise typer.BadParameter(str(exc), param_hint="--operation-id") from exc
    if not _has_cli():
        if as_json:
            _emit_lifecycle(
                "ps",
                "unavailable",
                operation_id=op_id,
                capabilities=["status"],
                agents=[],
                warnings=["Herdr CLI is not on PATH."],
            )
            return
        typer.echo("herdr: server=down (herdr CLI not on PATH)")
        return
    if not server_up():
        if as_json:
            _emit_lifecycle(
                "ps",
                "unavailable",
                operation_id=op_id,
                capabilities=["status"],
                agents=[],
                warnings=["Herdr server is down."],
            )
            return
        typer.echo("herdr: server=down")
        return
    if as_json:
        snapshot = _session_snapshot()
        if snapshot is None:
            _lifecycle_failure(
                "ps",
                operation_id=op_id,
                code="session_snapshot_unavailable",
                message="The authoritative Herdr session snapshot is unavailable.",
                retryable=True,
            )
        from . import jsonout

        jsonout.emit(_roster_payload(snapshot, operation_id=op_id))
        return
    records = _read_agents()
    if records is None:
        if as_json:
            _lifecycle_failure(
                "ps",
                operation_id=op_id,
                code="agent_list_unavailable",
                message="Herdr agent list is unavailable.",
                retryable=True,
            )
        typer.echo("herdr: agent list unavailable", err=True)
        raise typer.Exit(1)
    agents = []
    for record in records:
        name, hive, bead, state = _agent_identity(record)
        agents.append({"target": name, "hive": hive, "bead": bead, "state": state})
    typer.echo("name\thive\tbead\tstate")
    for name, hive, bead, state in (
        (item["target"], item["hive"], item["bead"], item["state"]) for item in agents
    ):
        typer.echo(f"{name}\t{hive or 'unmanaged'}\t{bead or 'unmanaged'}\t{state}")


@cli.command("integrate", help="install herdr lifecycle hooks for one agent kind.")
def _integrate_cmd(kind: str = typer.Argument(..., metavar="KIND")) -> None:
    """Install one explicitly requested herdr integration without hard-coded kinds."""
    if not _has_cli():
        typer.echo("herdr: cannot install integration — herdr CLI not on PATH", err=True)
        raise typer.Exit(1)

    kinds = supported_kinds()
    if not kinds:
        typer.echo(
            "herdr: could not determine supported agent kinds from 'herdr agent start --help'",
            err=True,
        )
        raise typer.Exit(1)
    if kind not in kinds:
        typer.echo(
            f"herdr: unsupported agent kind {kind!r}; supported kinds: {', '.join(kinds)}",
            err=True,
        )
        raise typer.Exit(2)

    result = _invoke(["herdr", "integration", "install", kind])
    if result is None or result.returncode != 0:
        detail = _output(result) if result is not None else ""
        message = f"herdr: failed to install integration for {kind}"
        typer.echo(f"{message}: {detail}" if detail else message, err=True)
        raise typer.Exit(1)

    typer.echo(f"herdr: integration installed for {kind}")
    if output := _output(result):
        typer.echo(output)


@cli.command("attach", help="print the command for a human to attach to an agent pane.")
def _attach_cmd(
    target: str = typer.Argument(..., metavar="TARGET"),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    """Print, but never run, the interactive attach command.

    Attaching transfers an operator's terminal.  Keeping this as a copy/paste
    instruction means a ``bh`` invocation cannot focus or take over their TTY.
    """
    argv = ["herdr", "--session", _SESSION, "agent", "attach", target]
    if as_json:
        try:
            op_id = _operation_id(operation_id)
        except ValueError as exc:
            _lifecycle_failure(
                "attach",
                operation_id="invalid",
                code="invalid_operation_id",
                message=str(exc),
                subsystem="beadhive",
                exit_code=2,
            )
        _emit_lifecycle(
            "attach",
            "instructions",
            operation_id=op_id,
            target=target,
            capabilities=["attach"],
            attach_argv=argv,
        )
        return
    typer.echo(shlex.join(argv))


@cli.command("reap", help="close a pane previously created by bh plugin herdr spawn.")
def _reap_cmd(
    target: str = typer.Argument(..., metavar="TARGET"),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    """Best-effort close of one live, bh-reserved spawned pane.

    Worktrees remain native-bh resources, and a hive workspace may host several
    bead panes, so neither is removed here.  A missing/ambiguous record is a
    refusal rather than a guess that could close an operator-owned pane.
    """
    try:
        op_id = _operation_id(operation_id)
    except ValueError as exc:
        if as_json:
            _lifecycle_failure(
                "reap",
                operation_id="invalid",
                code="invalid_operation_id",
                message=str(exc),
                subsystem="beadhive",
                exit_code=2,
                target=target,
            )
        raise typer.BadParameter(str(exc), param_hint="--operation-id") from exc
    if not server_up():
        if as_json:
            _lifecycle_failure(
                "reap",
                operation_id=op_id,
                code="herdr_server_unavailable",
                message="Herdr server is down; start Herdr before reaping an agent.",
                retryable=True,
                target=target,
                retained_resources=[{"kind": "target", "id": target}],
            )
        typer.echo("✗ herdr: server=down (start herdr before reaping an agent)", err=True)
        raise typer.Exit(1)
    pane = _owned_live_pane(target)
    if pane is None:
        if as_json:
            _lifecycle_failure(
                "reap",
                operation_id=op_id,
                code="ownership_not_proven",
                message="The target is unmanaged, stale, or ambiguous; no pane was closed.",
                disposition="refused",
                target=target,
                retained_resources=[{"kind": "target", "id": target}],
            )
        typer.echo(f"✗ herdr: refusing unmanaged or ambiguous target {target!r}", err=True)
        raise typer.Exit(1)
    closed = _command("pane", "close", pane, "--no-focus")
    if closed is None or closed.returncode != 0:
        if as_json:
            _lifecycle_failure(
                "reap",
                operation_id=op_id,
                code="pane_close_failed",
                message=_output(closed) or "Herdr did not close the proven pane.",
                retryable=True,
                target=target,
                pane=pane,
                retained_resources=[
                    {"kind": "target", "id": target},
                    {"kind": "pane", "id": pane},
                ],
            )
        _require(closed, "pane close")
    if as_json:
        _emit_lifecycle(
            "reap",
            "reaped",
            operation_id=op_id,
            target=target,
            pane=pane,
            capabilities=["status", "ps"],
        )
        return
    typer.echo(f"herdr target={target} reaped pane={pane}")


@cli.command("spawn", help="start a warm, steerable agent pane in an existing bh worktree.")
def _spawn_cmd(
    hive: str = typer.Option(..., "--hive", help="managed hive identifier"),
    bead: str = typer.Option(..., "--bead", help="already-claimed bead identifier"),
    kind: str = typer.Option(..., "--kind", help="Herdr agent kind, e.g. claude or codex"),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    """Create an isolated pane and prove its first conversational turn is promptable."""
    try:
        op_id = _operation_id(operation_id)
    except ValueError as exc:
        if as_json:
            _lifecycle_failure(
                "spawn",
                operation_id="invalid",
                code="invalid_operation_id",
                message=str(exc),
                subsystem="beadhive",
                exit_code=2,
                hive=hive,
                bead=bead,
            )
        raise typer.BadParameter(str(exc), param_hint="--operation-id") from exc
    if not server_up():
        if as_json:
            _lifecycle_failure(
                "spawn",
                operation_id=op_id,
                code="herdr_server_unavailable",
                message="Herdr server is down or its integration is unavailable.",
                retryable=True,
                hive=hive,
                bead=bead,
            )
        typer.echo("✗ herdr: server=down (start herdr and install its agent integration)", err=True)
        raise typer.Exit(1)
    pane = ""
    try:
        cfg = config.load()
        entry, cwd = _managed_worktree(hive, bead, cfg)
        kind = _resolve_kind(kind, cfg, entry)
        name = _launch_target(bead)
        existing = _strict_live_target(name, hive, cwd)
        if existing is not None:
            workspace, pane = existing
            if as_json:
                _emit_lifecycle(
                    "spawn",
                    "reused",
                    operation_id=op_id,
                    hive=hive,
                    bead=bead,
                    target=name,
                    workspace=workspace,
                    pane=pane,
                    worktree_path=str(cwd),
                    capabilities=["dispatch", "watch", "attach", "reap"],
                    kind=kind,
                )
                return
            typer.echo(f"herdr target={name} pane={pane} workspace={workspace} bead={bead}")
            return
        workspace, root_pane = _workspace(hive, cwd)
        split = _command(
            "pane",
            "split",
            "--pane",
            root_pane,
            "--direction",
            "right",
            "--cwd",
            str(cwd),
            "--no-focus",
        )
        _require(split, "pane split")
        pane = _required_id(split, "pane split", "pane", "pane_id", "id")
        _require(_command("agent", "start", name, "--kind", kind, "--pane", pane), "agent start")
        _require(_command("pane", "rename", pane, name), "pane rename")
        canonical_hive = (
            _hive_id(entry) if all(key in entry for key in ("provider", "org", "repo")) else hive
        )
        _tag_ownership(workspace, pane, canonical_hive, bead, name)
        _require(
            _command(
                "agent",
                "prompt",
                name,
                f"Reply with exactly {_WARMUP_TOKEN}.",
                "--wait",
                "--timeout",
                "60000",
            ),
            "agent warm-up",
        )
        visible = _require(
            _command("agent", "read", name, "--source", "visible", "--lines", "80"), "agent read"
        )
        if _WARMUP_TOKEN not in visible:
            _require(_command("agent", "send-keys", name, "esc"), "agent warm-up dismiss")
            _require(
                _command(
                    "agent",
                    "prompt",
                    name,
                    f"Reply with exactly {_WARMUP_TOKEN}.",
                    "--wait",
                    "--timeout",
                    "60000",
                ),
                "agent warm-up retry",
            )
            visible = _require(
                _command("agent", "read", name, "--source", "visible", "--lines", "80"),
                "agent read",
            )
        if _WARMUP_TOKEN not in visible:
            typer.echo("✗ herdr warm-up did not reach an idle agent prompt", err=True)
            raise typer.Exit(1)
    except Exception:
        if pane:
            _close_pane(pane)
        if as_json:
            _lifecycle_failure(
                "spawn",
                operation_id=op_id,
                code="spawn_failed",
                message="Herdr could not create and warm the requested agent pane.",
                retryable=False,
                hive=hive,
                bead=bead,
                target=locals().get("name"),
                workspace=locals().get("workspace"),
                pane=pane or None,
                worktree_path=str(locals()["cwd"]) if "cwd" in locals() else None,
                retained_resources=(
                    [{"kind": "worktree", "path": str(locals()["cwd"])}]
                    if "cwd" in locals()
                    else []
                ),
            )
        raise
    if as_json:
        _emit_lifecycle(
            "spawn",
            "created",
            operation_id=op_id,
            hive=hive,
            bead=bead,
            target=name,
            workspace=workspace,
            pane=pane,
            worktree_path=str(cwd),
            capabilities=["dispatch", "watch", "attach", "reap"],
            kind=kind,
        )
        return
    typer.echo(f"herdr target={name} pane={pane} workspace={workspace} bead={bead}")


@cli.command("dispatch", help="send a prompt and verify it reached the agent pane.")
def _dispatch_cmd(
    target: str = typer.Argument(..., metavar="TARGET", help="herdr agent target"),
    prompt: str | None = typer.Argument(
        None, metavar="[PROMPT]", help="legacy positional prompt; prefer --stdin or --prompt-file"
    ),
    from_stdin: bool = typer.Option(
        False, "--stdin", help="read prompt from stdin and keep it out of process arguments"
    ),
    prompt_file: str = typer.Option(
        "", "--prompt-file", help="read prompt from this file and keep it out of process arguments"
    ),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    """Deliver a prompt only when a before/after pane read proves a new real turn.

    Herdr's ``--wait`` observes a lifecycle transition, which can report ``done``
    even when first-run UI consumed the prompt.  A post-dispatch substring check
    alone is not evidence: the same text could be from an older turn.  Capture
    the pane first, then require the requested text to occur more often after the
    dispatch.  This preserves the exact user prompt while making the proof turn-
    specific, including when the user deliberately repeats a prompt.
    """
    try:
        op_id = _operation_id(operation_id)
    except ValueError as exc:
        if as_json:
            _lifecycle_failure(
                "dispatch",
                operation_id="invalid",
                code="invalid_operation_id",
                message=str(exc),
                subsystem="beadhive",
                exit_code=2,
                target=target,
            )
        raise typer.BadParameter(str(exc), param_hint="--operation-id") from exc
    try:
        prompt_text, input_source, safe_transport = _prompt_input(
            prompt, from_stdin=from_stdin, prompt_file=prompt_file
        )
    except ValueError as exc:
        if as_json:
            _lifecycle_failure(
                "dispatch",
                operation_id=op_id,
                code="invalid_prompt_input",
                message=str(exc),
                subsystem="beadhive",
                exit_code=2,
                target=target,
            )
        raise typer.BadParameter(str(exc)) from exc
    if not server_up():
        if as_json:
            _lifecycle_failure(
                "dispatch",
                operation_id=op_id,
                code="herdr_server_unavailable",
                message="Herdr server is down or its integration is unavailable.",
                retryable=True,
                target=target,
            )
        typer.echo("✗ herdr: server=down (start herdr and install its agent integration)", err=True)
        raise typer.Exit(1)
    if _owned_live_agent(target) is None:
        if as_json:
            _lifecycle_failure(
                "dispatch",
                operation_id=op_id,
                code="ownership_not_proven",
                message="The target is unmanaged, stale, or ambiguous; no prompt was sent.",
                disposition="refused",
                target=target,
            )
        typer.echo(f"✗ herdr: refusing unmanaged or ambiguous target {target!r}", err=True)
        raise typer.Exit(1)
    pre_read = _command("agent", "read", target, "--source", "visible", "--lines", "80")
    if pre_read is None or pre_read.returncode != 0:
        if as_json:
            _lifecycle_failure(
                "dispatch",
                operation_id=op_id,
                code="dispatch_pre_read_failed",
                message=_output(pre_read) or "Herdr could not read the target before dispatch.",
                retryable=True,
                target=target,
            )
        _require(pre_read, "agent dispatch pre-read")
    before = _output(pre_read)
    if safe_transport:
        dispatched = _prompt_over_socket(target, prompt_text)
    else:
        # Compatibility only. The safe stdin/file forms above avoid putting text in child argv.
        dispatched = _command(
            "agent", "prompt", target, prompt_text, "--wait", "--timeout", "60000"
        )
    if dispatched is None or dispatched.returncode != 0:
        if as_json:
            detail = (_output(dispatched) or "Herdr refused the agent prompt.").replace(
                prompt_text, "[redacted]"
            )
            code = (
                "dispatch_timeout"
                if getattr(dispatched, "returncode", None) == 124
                else "dispatch_failed"
            )
            _lifecycle_failure(
                "dispatch",
                operation_id=op_id,
                code=code,
                message=detail,
                retryable=False,
                target=target,
                input_source=input_source,
            )
        _require(dispatched, "agent dispatch")
    post_read = _command("agent", "read", target, "--source", "visible", "--lines", "80")
    if post_read is None or post_read.returncode != 0:
        if as_json:
            _lifecycle_failure(
                "dispatch",
                operation_id=op_id,
                code="dispatch_readback_failed",
                message=_output(post_read) or "Herdr could not verify the target after dispatch.",
                retryable=False,
                target=target,
                input_source=input_source,
            )
        _require(post_read, "agent dispatch read-back")
    visible = _output(post_read)
    # The local socket's successful structured response is the delivery proof for safe input.
    # A full prompt can be far larger than Herdr's bounded visible-pane readback, so requiring
    # the entire body to reappear there would turn successful large dispatches into refusals.
    # Legacy argv transport has no such protocol acknowledgement and retains the stricter
    # before/after visible-turn proof.
    if not safe_transport and visible.count(prompt_text) <= before.count(prompt_text):
        if as_json:
            _lifecycle_failure(
                "dispatch",
                operation_id=op_id,
                code="dispatch_unverified",
                message="The prompt was not proven in a new real agent turn.",
                retryable=False,
                disposition="refused",
                target=target,
                input_source=input_source,
            )
        typer.echo(
            "✗ herdr dispatch did not reach a new real agent turn; prompt was absent from new pane content",
            err=True,
        )
        raise typer.Exit(1)
    if as_json:
        warnings = [] if safe_transport else ["Legacy positional prompt transport was used."]
        _emit_lifecycle(
            "dispatch",
            "dispatched",
            operation_id=op_id,
            target=target,
            capabilities=["watch", "attach", "dispatch"],
            warnings=warnings,
            input_source=input_source,
            delivery_verified=True,
        )
        return
    typer.echo(f"herdr dispatched target={target}")


@cli.command("watch", help="wait for an agent to become blocked or finish.")
def _watch_cmd(
    target: str = typer.Argument(..., metavar="TARGET"),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        min=0.0,
        help="seconds to wait before giving up (herdr's millisecond timeout is derived from it)",
    ),
    as_json: bool = typer.Option(False, "--json", help="emit a versioned lifecycle receipt"),
    operation_id: str = typer.Option("", "--operation-id", help="caller correlation ID"),
) -> None:
    """Wait for TARGET's blocked/settled state without polling from bh.

    Herdr owns lifecycle state and its ``agent wait`` command is the source of truth.  The
    wrapper only fences availability and translates bh's seconds-oriented timeout option to
    herdr's millisecond argument; a timed-out wait is reported as a normal command failure.
    """
    try:
        op_id = _operation_id(operation_id)
    except ValueError as exc:
        if as_json:
            _lifecycle_failure(
                "watch",
                operation_id="invalid",
                code="invalid_operation_id",
                message=str(exc),
                subsystem="beadhive",
                exit_code=2,
                target=target,
            )
        raise typer.BadParameter(str(exc), param_hint="--operation-id") from exc
    if not _has_cli():
        if as_json:
            _lifecycle_failure(
                "watch",
                operation_id=op_id,
                code="herdr_cli_unavailable",
                message="Herdr CLI is not on PATH.",
                target=target,
            )
        typer.echo("✗ herdr: cannot watch — herdr CLI not on PATH", err=True)
        raise typer.Exit(1)
    if not server_up():
        if as_json:
            _lifecycle_failure(
                "watch",
                operation_id=op_id,
                code="herdr_server_unavailable",
                message="Herdr server is down; start Herdr before watching an agent.",
                retryable=True,
                target=target,
            )
        typer.echo("✗ herdr: server=down (start herdr before watching an agent)", err=True)
        raise typer.Exit(1)

    wait_args = ["agent", "wait", target, "--until", "blocked"]
    if timeout is not None:
        wait_args.extend(["--timeout", str(int(timeout * 1000))])
    waited = _command(*wait_args, timeout=timeout)
    if waited is None or waited.returncode != 0:
        if as_json:
            timed_out = getattr(waited, "returncode", None) == 124
            _lifecycle_failure(
                "watch",
                operation_id=op_id,
                code="watch_timeout" if timed_out else "watch_failed",
                message=_output(waited) or "Herdr agent wait failed.",
                retryable=True,
                disposition="timed_out" if timed_out else "failed",
                target=target,
            )
        _require(waited, "agent wait")
    output = _output(waited)
    if as_json:
        decoded = _decoded(waited)
        state = None
        if isinstance(decoded, dict):
            data = _result_payload(decoded)
            state = _string_field(data, "agent_status", "status", "state")
        if state is None:
            match = re.search(r"(?:agent_)?status\s*[:=]\s*([A-Za-z_-]+)", output)
            state = match.group(1) if match else None
        _emit_lifecycle(
            "watch",
            "settled",
            operation_id=op_id,
            target=target,
            capabilities=["watch", "attach", "dispatch"],
            resulting_state=state,
        )
        return
    if output:
        typer.echo(output)
    else:
        typer.echo(f"herdr target={target} settled")


PLUGIN = plugins.Plugin(
    name="herdr",
    cli=cli,
    enabled=lambda cfg, entry: server_up(),
)

# Keep the presentation adapter in its own module: this lifecycle wrapper remains the authority
# for launch/dispatch/watch/attach/reap, while ``view`` only binds those commands declaratively.
from . import herdr_views as _herdr_views  # noqa: E402

cli.add_typer(_herdr_views.cli, name="view")
