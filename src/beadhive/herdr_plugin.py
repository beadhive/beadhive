"""Optional, best-effort integration with the :command:`herdr` terminal server.

This initial plugin deliberately owns no worktree lifecycle.  ``bh`` remains the
authority for managed worktrees and branches; herdr is only an interactive
execution surface.  Every probe in this module is fenced: an absent executable,
stopped server, or failed subprocess must never make importing ``beadhive`` (or
the generic plugin registry) fail.
"""

from __future__ import annotations

import re
import shutil

import typer

from . import plugins, run


def _has_cli() -> bool:
    """Whether the herdr executable is available without raising."""
    try:
        return shutil.which("herdr") is not None
    except Exception:  # noqa: BLE001 - optional integration availability probe
        return False


def _invoke(argv: list[str]):
    """Run a read-only herdr probe, returning ``None`` for every failure."""
    try:
        return run.run(argv, check=False, capture=True)
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


PLUGIN = plugins.Plugin(
    name="herdr",
    cli=cli,
    enabled=lambda cfg, entry: server_up(),
)
