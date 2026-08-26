# ruff: noqa: E501
"""Optional, best-effort integration with the :command:`herdr` terminal server.

This initial plugin deliberately owns no worktree lifecycle.  ``bh`` remains the
authority for managed worktrees and branches; herdr is only an interactive
execution surface.  Every probe in this module is fenced: an absent executable,
stopped server, or failed subprocess must never make importing ``beadhive`` (or
the generic plugin registry) fail.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import typer

from . import config, plugins, run, worktree

_SESSION = "bh-supervisor"
_WARMUP_TOKEN = "BH_HERDR_WARMUP_OK"


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


def _live_agent_records() -> list[dict] | None:
    """Read authoritative live records without the snapshot fallback used by ``ps``."""
    for argv in (("agent", "list", "--json"), ("agent", "list")):
        result = _command(*argv)
        if result is None or result.returncode != 0:
            continue
        data = _decoded(result)
        if not _looks_like_agent_list(data):
            continue
        return _agent_records(data, unique_by_name=False, include_pane_claims=True)
    return None


def _record_pane(record: dict) -> tuple[str, str] | None:
    """Return the live pane id and visible name, only when both are unambiguous."""
    pane = record.get("pane")
    pane_id = _record_pane_id(record)
    pane_name = (
        record.get("pane_name")
        or record.get("pane_label")
        or record.get("pane_title")
        or record.get("label")
        or record.get("title")
    )
    if isinstance(pane, dict):
        pane_name = (
            pane.get("name")
            or pane.get("label")
            or pane.get("title")
            or pane.get("pane_name")
            or pane_name
        )
        nested = pane.get("pane")
        if not pane_name and isinstance(nested, dict):
            pane_name = (
                nested.get("name")
                or nested.get("label")
                or nested.get("title")
                or nested.get("pane_name")
            )
    if pane_id is None:
        return None
    if not isinstance(pane_name, str) or not pane_name.strip():
        return None
    return pane_id, pane_name


def _owned_live_pane(target: str) -> str | None:
    """Return a pane only when the live herdr records prove bh owns it."""
    if _BEAD_RE.fullmatch(target) is None:
        return None
    records = _live_agent_records()
    if records is None:
        return None
    matches = [record for record in records if _agent_identity(record)[0] == target]
    if len(matches) != 1 or _agent_identity(matches[0])[3].lower() not in _LIVE_AGENT_STATES:
        return None
    pane = _record_pane(matches[0])
    if pane is None:
        return None
    pane_id, pane_name = pane
    if pane_name != target:
        return None
    pane_records = [record for record in records if _record_pane_id(record) == pane_id]
    if len(pane_records) != 1:
        return None
    return pane_id


cli = typer.Typer(no_args_is_help=True, help="herdr terminal/agent-pane integration.")


@cli.command("status", help="show herdr server health and installed agent integrations.")
def _status_cmd() -> None:
    """A safe one-shot health report for herdr and its per-kind hook integrations."""
    if not _has_cli():
        typer.echo("herdr: server=down (herdr CLI not on PATH)")
        typer.echo("herdr integrations: unavailable")
        return

    status = _invoke(["herdr", "status"])
    if status is None or status.returncode != 0:
        typer.echo("herdr: server=down")
    else:
        typer.echo("herdr: server=up")
        if output := _output(status):
            typer.echo(output)
    integrations = _invoke(["herdr", "integration", "status"])
    if integrations is None or integrations.returncode != 0:
        typer.echo("herdr integrations: unavailable")
    else:
        typer.echo("herdr integrations:")
        if output := _output(integrations):
            typer.echo(output)


@cli.command("ps", help="list live herdr agents and their bh identity.")
def _ps_cmd() -> None:
    """Show live agents without maintaining a bh-side identity table."""
    if not _has_cli():
        typer.echo("herdr: server=down (herdr CLI not on PATH)")
        return
    if not server_up():
        typer.echo("herdr: server=down")
        return
    records = _read_agents()
    if records is None:
        typer.echo("herdr: agent list unavailable", err=True)
        raise typer.Exit(1)
    typer.echo("name\thive\tbead\tstate")
    for record in records:
        name, hive, bead, state = _agent_identity(record)
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
def _attach_cmd(target: str = typer.Argument(..., metavar="TARGET")) -> None:
    """Print, but never run, the interactive attach command.

    Attaching transfers an operator's terminal.  Keeping this as a copy/paste
    instruction means a ``bh`` invocation cannot focus or take over their TTY.
    """
    typer.echo(shlex.join(["herdr", "--session", _SESSION, "agent", "attach", target]))


@cli.command("reap", help="close a pane previously created by bh plugin herdr spawn.")
def _reap_cmd(target: str = typer.Argument(..., metavar="TARGET")) -> None:
    """Best-effort close of one live, bh-reserved spawned pane.

    Worktrees remain native-bh resources, and a hive workspace may host several
    bead panes, so neither is removed here.  A missing/ambiguous record is a
    refusal rather than a guess that could close an operator-owned pane.
    """
    if not server_up():
        typer.echo("✗ herdr: server=down (start herdr before reaping an agent)", err=True)
        raise typer.Exit(1)
    pane = _owned_live_pane(target)
    if pane is None:
        typer.echo(f"✗ herdr: refusing unmanaged or ambiguous target {target!r}", err=True)
        raise typer.Exit(1)
    _require(_command("pane", "close", pane, "--no-focus"), "pane close")
    typer.echo(f"herdr target={target} reaped pane={pane}")


@cli.command("spawn", help="start a warm, steerable agent pane in an existing bh worktree.")
def _spawn_cmd(
    hive: str = typer.Option(..., "--hive", help="managed hive identifier"),
    bead: str = typer.Option(..., "--bead", help="already-claimed bead identifier"),
    kind: str | None = typer.Option(
        None, "--kind", help="Herdr agent kind; overrides per-hive and global herdr.kind"
    ),
) -> None:
    """Create an isolated pane and prove its first conversational turn is promptable."""
    if not server_up():
        typer.echo("✗ herdr: server=down (start herdr and install its agent integration)", err=True)
        raise typer.Exit(1)
    cfg = config.load()
    entry, cwd = _managed_worktree(hive, bead, cfg)
    kind = _resolve_kind(kind, cfg, entry)
    workspace, root_pane = _workspace(hive, cwd)
    pane = ""
    try:
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
        name = f"bh-{bead}"
        _require(_command("agent", "start", name, "--kind", kind, "--pane", pane), "agent start")
        _require(_command("pane", "rename", pane, name), "pane rename")
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
        raise
    typer.echo(f"herdr target={name} pane={pane} workspace={workspace} bead={bead}")


@cli.command("dispatch", help="send a prompt and verify it reached the agent pane.")
def _dispatch_cmd(
    target: str = typer.Argument(..., metavar="TARGET", help="herdr agent target"),
    prompt: str = typer.Argument(..., metavar="PROMPT", help="prompt to deliver"),
) -> None:
    """Deliver a prompt only when a before/after pane read proves a new real turn.

    Herdr's ``--wait`` observes a lifecycle transition, which can report ``done``
    even when first-run UI consumed the prompt.  A post-dispatch substring check
    alone is not evidence: the same text could be from an older turn.  Capture
    the pane first, then require the requested text to occur more often after the
    dispatch.  This preserves the exact user prompt while making the proof turn-
    specific, including when the user deliberately repeats a prompt.
    """
    if not server_up():
        typer.echo("✗ herdr: server=down (start herdr and install its agent integration)", err=True)
        raise typer.Exit(1)
    before = _require(
        _command("agent", "read", target, "--source", "visible", "--lines", "80"),
        "agent dispatch pre-read",
    )
    _require(
        _command("agent", "prompt", target, prompt, "--wait", "--timeout", "60000"),
        "agent dispatch",
    )
    visible = _require(
        _command("agent", "read", target, "--source", "visible", "--lines", "80"),
        "agent dispatch read-back",
    )
    if not prompt or visible.count(prompt) <= before.count(prompt):
        typer.echo(
            "✗ herdr dispatch did not reach a new real agent turn; prompt was absent from new pane content",
            err=True,
        )
        raise typer.Exit(1)
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
) -> None:
    """Wait for TARGET's blocked/settled state without polling from bh.

    Herdr owns lifecycle state and its ``agent wait`` command is the source of truth.  The
    wrapper only fences availability and translates bh's seconds-oriented timeout option to
    herdr's millisecond argument; a timed-out wait is reported as a normal command failure.
    """
    if not _has_cli():
        typer.echo("✗ herdr: cannot watch — herdr CLI not on PATH", err=True)
        raise typer.Exit(1)
    if not server_up():
        typer.echo("✗ herdr: server=down (start herdr before watching an agent)", err=True)
        raise typer.Exit(1)

    wait_args = ["agent", "wait", target, "--until", "blocked"]
    if timeout is not None:
        wait_args.extend(["--timeout", str(int(timeout * 1000))])
    output = _require(_command(*wait_args, timeout=timeout), "agent wait")
    if output:
        typer.echo(output)
    else:
        typer.echo(f"herdr target={target} settled")


PLUGIN = plugins.Plugin(
    name="herdr",
    cli=cli,
    enabled=lambda cfg, entry: server_up(),
)
