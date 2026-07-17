"""Declared toolchains (bh-d0kb): KNOWLEDGE-ONLY metadata — a hive says what it uses.

A ``toolchain:`` declaration — global ``worktrees.toolchain`` or per-hive
``managed_repos[*].toolchain``, a name or a list of names — resolves against a shipped
template registry. The declaration **never drives behavior**: no init rules are derived
from it and no validate default consults it (revised triage decision, 2026-07-17 —
template assumptions about what ``npm ci`` / ``just setup`` mean in a given repo must
never be acted on implicitly). What it gives you instead:

- **Discovery**: each template's ``entrypoints_cmd`` lists the entrypoints that toolchain
  exposes in a repo (``bh toolchain show <name>`` runs it in the hive's main clone).
- **Suggestions**: ``suggested_init`` / ``suggested_validate_cmd`` are what an agent
  PROPOSES to an operator, who then sets explicit config (``worktrees.init`` /
  ``work.validate_cmd``) — bh never applies them automatically.
- **A driven exec seam**: ``bh toolchain exec [--hive H] -- <argv...>`` invokes an
  entrypoint in the hive's main clone through the ``run()`` seam (exit code passthrough).

``worktrees.toolchains: {name: template}`` overlays the shipped registry per-name
(replace, not merge), so a hive can amend a built-in or add its own. Full contract:
docs/design/toolchain-declaration.md.
"""

from __future__ import annotations

import json
import shlex

import typer

from . import config, otel
from . import registry as hive_registry
from .run import run

# Suggested provisioning keeps the bh-17n4 severity principle: a declaration doesn't
# guarantee a canonical setup recipe, so the suggested rules probe first and no-op with a
# quiet info echo — the ⚠ warn path stays reserved for rules that RAN and failed.
_JUST_SETUP = (
    "sh -c 'if just --show setup >/dev/null 2>&1; then just setup; "
    'else echo "just setup: not configured in this repo"; fi\''
)
_MAKE_SETUP = (
    "sh -c 'if make -n setup >/dev/null 2>&1; then make setup; "
    'else echo "make setup: not configured in this repo"; fi\''
)

# Entrypoint discovery commands. Pragmatic picks, documented:
# - just / npm ship a first-class listing verb (`just --list` / `npm run`).
# - make has NO portable listing verb; we dump the rule database (`make -pRrq :` runs
#   nothing) and grep for target-looking lines — best-effort, may include file targets.
# - uv projects declare entrypoints in pyproject's [project.scripts]; a python3 one-liner
#   (tomllib, py3.11+) reads them without needing uv itself. Still a command string so
#   hive overrides stay plain YAML data and tests fake the one run() seam.
_MAKE_ENTRYPOINTS = (
    'sh -c "make -pRrq : 2>/dev/null '
    "| grep -E '^[a-zA-Z0-9][^ :=]*:([^=]|$)' | cut -d: -f1 | sort -u\""
)
_UV_ENTRYPOINTS = (
    'python3 -c "import tomllib; '
    "s = tomllib.load(open('pyproject.toml', 'rb')).get('project', {}).get('scripts', {}); "
    "print('\\n'.join(f'{k} = {v}' for k, v in s.items()) or '(no [project.scripts])')\""
)

# Shipped built-in templates. Each entry:
#   entrypoints_cmd        — the discovery command `bh toolchain show` runs (read-only).
#   suggested_init         — init rules ({run, if_exists?, verify?}) an agent may PROPOSE
#                            for worktrees.init; the verify line is pre-drawn per bh-7k1p
#                            (dependency sync flagged, seat provisioning not). Never
#                            consumed by run_init — suggestion only.
#   suggested_validate_cmd — a validate_cmd an agent may PROPOSE for work.validate_cmd.
#                            Never consulted by config.validate_cmd — suggestion only.
TOOLCHAINS: dict[str, dict] = {
    "just": {
        "entrypoints_cmd": "just --list",
        "suggested_init": [{"if_exists": "justfile", "run": _JUST_SETUP}],
        "suggested_validate_cmd": "just check",
    },
    "uv": {
        "entrypoints_cmd": _UV_ENTRYPOINTS,
        "suggested_init": [{"if_exists": "pyproject.toml", "run": "uv sync", "verify": True}],
        "suggested_validate_cmd": "uv run pytest",
    },
    "npm": {
        "entrypoints_cmd": "npm run",
        "suggested_init": [{"if_exists": "package-lock.json", "run": "npm ci", "verify": True}],
        "suggested_validate_cmd": "npm test",
    },
    "make": {
        "entrypoints_cmd": _MAKE_ENTRYPOINTS,
        "suggested_init": [{"if_exists": "Makefile", "run": _MAKE_SETUP}],
        "suggested_validate_cmd": "make check",
    },
}


class ToolchainError(Exception):
    """A toolchain surface error (unknown name, empty argv, missing binary) — the shared
    core raises it; the CLI maps it to ✗ + exit 1, MCP to ToolError/ResourceError."""


def registry(cfg=None) -> dict[str, dict]:
    """The effective template registry: shipped built-ins overlaid with the hive config's
    ``worktrees.toolchains`` (per-name REPLACE — an override owns its whole template)."""
    out = {name: dict(tpl) for name, tpl in TOOLCHAINS.items()}
    for name, tpl in (config.worktrees_cfg(cfg).get("toolchains") or {}).items():
        out[str(name)] = dict(tpl or {})
    return out


def declared(cfg, entry) -> list[str]:
    """Declared toolchain names: per-hive ``entry['toolchain']`` > global
    ``worktrees.toolchain``. A bare string means a one-element list; unset ⇒ []."""
    raw = (entry or {}).get("toolchain")
    if raw is None:
        raw = config.worktrees_cfg(cfg).get("toolchain")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(name) for name in raw]


def template(cfg, name: str) -> dict:
    """The registry template for *name*, or ToolchainError when unknown."""
    reg = registry(cfg)
    if name not in reg:
        raise ToolchainError(
            f"unknown toolchain '{name}' — not in the registry "
            f"(shipped: {', '.join(sorted(TOOLCHAINS))}; overrides: worktrees.toolchains)"
        )
    return reg[name]


# ---- payload producers (shared by the CLI --json output and the MCP surface) -


def list_payload(cfg, entry) -> dict:
    """``bh toolchain list --json`` / ``beadhive://toolchain/list``: the declared names
    plus the effective registry (templates carry entrypoints_cmd + the suggested_* fields)."""
    return {"declared": declared(cfg, entry), "registry": registry(cfg)}


def show_payload(cfg, name: str, cwd) -> dict:
    """``bh toolchain show --json`` / ``beadhive://toolchain/show/{name}``: run the
    template's ``entrypoints_cmd`` in *cwd* (the hive's main clone) and bundle the raw
    listing with the template's suggestions. Unknown name / missing binary raise
    ToolchainError."""
    tpl = template(cfg, name)
    cmd = str(tpl.get("entrypoints_cmd") or "")
    entrypoints, exit_code = "", 0
    if cmd:
        try:
            res = run(shlex.split(cmd), cwd=str(cwd), check=False, capture=True)
        except FileNotFoundError as exc:
            raise ToolchainError(f"entrypoints_cmd for '{name}': command not found: {cmd}") from exc
        entrypoints, exit_code = res.stdout or "", res.returncode
    return {
        "name": name,
        "entrypoints_cmd": cmd,
        "entrypoints": entrypoints,
        "exit_code": exit_code,
        "suggestions": {
            "init": [dict(r) for r in (tpl.get("suggested_init") or [])],
            "validate_cmd": str(tpl.get("suggested_validate_cmd") or ""),
        },
    }


def exec_entrypoint(argv: list[str], cwd, capture: bool = False):
    """Invoke *argv* in *cwd* (the hive's main clone) through the ``run()`` seam.
    Returns the CompletedProcess (caller passes the exit code through). Empty argv and a
    missing binary raise ToolchainError."""
    if not argv:
        raise ToolchainError("toolchain exec: empty argv — pass the command after `--`")
    try:
        return run(list(argv), cwd=str(cwd), check=False, capture=capture)
    except FileNotFoundError as exc:
        raise ToolchainError(f"toolchain exec: command not found: {argv[0]}") from exc


# ---- CLI group (`bh toolchain …`) -------------------------------------------

app = typer.Typer(
    no_args_is_help=True,
    help="Declared toolchains — knowledge-only metadata: list / show entrypoints / exec.",
)

_HIVE = typer.Option("", "--hive", help="target hive (default: cwd's hive)")
_JSONOUT = typer.Option(False, "--json", help="machine payload (same shape as the MCP resource)")


def _entry(cfg, hive: str):
    """The managed_repos entry the per-hive declaration reads from: ``--hive`` resolved
    through the registry, else cwd's hive, else {} (global declaration only)."""
    if hive:
        return hive_registry.resolve_hive(cfg, hive)
    return hive_registry.current_hive(cfg) or {}


@app.command("list")
@otel.trace_verb("toolchain.list")
def list_(hive: str = _HIVE, as_json: bool = _JSONOUT):
    """Declared toolchains + the effective template registry. Read-only."""
    cfg = config.load()
    payload = list_payload(cfg, _entry(cfg, hive))
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    names = payload["declared"]
    typer.echo("declared: " + (", ".join(names) if names else "(none)"))
    typer.echo("registry:")
    for name, tpl in sorted(payload["registry"].items()):
        marker = "●" if name in names else "○"
        typer.echo(f"  {marker} {name}: entrypoints via `{tpl.get('entrypoints_cmd', '')}`")


@app.command("show")
@otel.trace_verb("toolchain.show")
def show(
    name: str = typer.Argument(..., help="toolchain name from the registry"),
    hive: str = _HIVE,
    as_json: bool = _JSONOUT,
):
    """Run the toolchain's entrypoints_cmd in the hive's main clone: the raw entrypoint
    listing + the template's suggestions (propose-only — never applied)."""
    cfg = config.load()
    cwd = hive_registry.hive_dir_for(cfg, hive)
    try:
        payload = show_payload(cfg, name, cwd)
    except ToolchainError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"# {name} — entrypoints via `{payload['entrypoints_cmd']}`")
    if payload["exit_code"]:
        typer.echo(f"  ⚠ entrypoints_cmd exited {payload['exit_code']}", err=True)
    listing = payload["entrypoints"].rstrip("\n")
    typer.echo(listing or "(no entrypoints reported)")
    sugg = payload["suggestions"]
    typer.echo("\n## Suggestions (propose to the operator — bh never applies these)")
    typer.echo(f"validate_cmd: {sugg['validate_cmd'] or '(none)'}")
    for rule in sugg["init"]:
        typer.echo(f"init rule:    {rule}")


@app.command(
    "exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
@otel.trace_verb("toolchain.exec")
def exec_(ctx: typer.Context, hive: str = _HIVE):
    """Invoke an entrypoint in the hive's main clone: `bh toolchain exec [--hive H] -- <argv...>`.
    Output streams through; the entrypoint's exit code passes through as bh's."""
    cfg = config.load()
    try:
        res = exec_entrypoint(list(ctx.args), hive_registry.hive_dir_for(cfg, hive))
    except ToolchainError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    raise typer.Exit(res.returncode)
