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


def _workspace_from_snapshot(label: str) -> str | None:
    """Find a previously-created bh workspace by its visible label, if snapshot is usable."""
    result = _command("api", "snapshot")
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(_output(result))
    except (TypeError, ValueError):
        return None

    def visit(value):
        if isinstance(value, dict):
            if value.get("label") == label:
                return str(value.get("id") or value.get("workspace_id") or "") or None
            for child in value.values():
                if found := visit(child):
                    return found
        elif isinstance(value, list):
            for child in value:
                if found := visit(child):
                    return found
        return None

    return visit(data)


def _workspace(hive: str, cwd: Path) -> str:
    """Reuse the hive's isolated workspace, or create it without focusing the operator's TTY."""
    label = f"bh:{hive}"
    if existing := _workspace_from_snapshot(label):
        return existing
    return _require(
        _command("workspace", "create", "--cwd", str(cwd), "--label", label, "--no-focus"),
        "workspace create",
    )


def _managed_worktree(hive: str, bead: str) -> Path:
    """Resolve, but never provision, the native bh worktree for ``bead``."""
    cfg = config.load()
    _entry, _main, target, _branch = worktree.locate(cfg, hive, bead)
    if not target.is_dir() or not (target / ".git").exists():
        typer.echo(f"✗ no managed bh worktree for {bead} in {hive} — claim it first", err=True)
        raise typer.Exit(1)
    return target


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


# ``spawn`` reserves ``bh-<bead-id>``. Since bead ids themselves begin with
# ``bh-``, only the resulting ``bh-bh-*`` values are claimed. Full matching
# keeps names such as ``operator-bh-foo`` user-owned.
_BEAD_RE = re.compile(r"^bh-(?P<bead>bh-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?)$")


def _agent_records(value) -> list[dict]:
    """Extract agent-shaped records from herdr's versioned JSON responses.

    ``agent list`` and ``api snapshot`` have changed their envelope shape between
    herdr releases.  Keep this deliberately structural: only dictionaries with a
    name-like field and a lifecycle-like field are considered agents.
    """
    records: list[dict] = []

    def visit(item) -> None:
        if isinstance(item, dict):
            nested = item.get("agent")
            candidate = nested if isinstance(nested, dict) else item
            name = candidate.get("name") or candidate.get("agent_name") or candidate.get("target")
            state = (
                candidate.get("state")
                or candidate.get("status")
                or candidate.get("lifecycle")
                or candidate.get("lifecycle_state")
            )
            if isinstance(name, str) and isinstance(state, (str, int, float)):
                records.append(candidate)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
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
            or record.get("lifecycle")
            or record.get("lifecycle_state")
        ),
    )


def _looks_like_agent_list(value) -> bool:
    """Recognize a successful (including empty) ``agent list --json`` payload."""
    if isinstance(value, list):
        return not value or all(isinstance(item, dict) for item in value)
    return isinstance(value, dict) and isinstance(value.get("agents"), list)


def _read_agents() -> list[dict] | None:
    """Read live agents, falling back to a snapshot when list is unsupported."""
    for argv in (("agent", "list", "--json"), ("agent", "list"), ("api", "snapshot")):
        result = _command(*argv)
        if result is None or result.returncode != 0:
            continue
        raw = _output(result)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        records = _agent_records(data)
        if argv[:2] == ("agent", "list") and _looks_like_agent_list(data):
            return records
        if records or argv[-1] == "snapshot":
            return records
    return None


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


@cli.command("spawn", help="start a warm, steerable agent pane in an existing bh worktree.")
def _spawn_cmd(
    hive: str = typer.Option(..., "--hive", help="managed hive identifier"),
    bead: str = typer.Option(..., "--bead", help="already-claimed bead identifier"),
    kind: str = typer.Option(..., "--kind", help="herdr agent kind, e.g. claude or codex"),
) -> None:
    """Create an isolated pane and prove its first conversational turn is promptable."""
    if not server_up():
        typer.echo("✗ herdr: server=down (start herdr and install its agent integration)", err=True)
        raise typer.Exit(1)
    cwd = _managed_worktree(hive, bead)
    workspace = _workspace(hive, cwd)
    pane = ""
    try:
        pane = _require(
            _command(
                "pane",
                "split",
                "--pane",
                f"{workspace}:p1",
                "--direction",
                "right",
                "--cwd",
                str(cwd),
                "--no-focus",
            ),
            "pane split",
        )
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
