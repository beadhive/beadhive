"""`ws-mcp` / `ws mcp serve` — a FastMCP stdio server exposing ws as MCP tools.

Scaffold built the `FastMCP("ws")` instance + the graceful
absent-`fastmcp` path. This bead wires the *complex-input*
commands as `@mcp.tool` wrappers over the existing Typer-free core fns — the ones
whose value over the CLI is structured I/O (typed specs / squash plans in, JSON
previews + validation problems out), so an MCP client never marshals YAML temp
files or scrapes CLI strings:

  * `plan_check` — validate a molecule spec (structured) → {valid, problems}.
  * `plan_file`  — file a molecule spec (structured, no temp YAML) → epic/counts,
                   or a structured preview under `dry_run`.
  * `work_refine`— squash local checkpoint noise via a structured plan (or
                   autosquash / since) → the refine report.
  * `bd_create`  — batch-create beads (identity triplet auto-applied) → created ids.

Simple / bulk CLI-only commands are deliberately NOT exposed — they carry no
structured-I/O advantage over the shell.  Core exceptions (`MoleculeError`,
`PlanError`, `WorkError`) map to FastMCP `ToolError`s so the client sees a clean,
actionable message instead of a stack trace.

`fastmcp` is imported lazily inside `build_server` so that `import ws.mcp` — and
therefore the `ws mcp serve` subcommand registration in the CLI — is always safe
even when the optional `[mcp]` extra isn't installed.
"""

from __future__ import annotations

import functools
import json
import os
import sys
import tempfile
import time

from . import bd, config, log, molecule, otel, plan, work
from .identity import resolve_actor

# Hint shown when the optional extra is missing. Kept as a module constant so both
# the console-script (`ws-mcp`) and the `ws mcp serve` subcommand surface the same text.
INSTALL_HINT = (
    "the ws MCP server needs the optional 'fastmcp' dependency, which isn't installed.\n"
    "  install the extra:  uv tool install 'ws[mcp]'   (or: pip install 'ws[mcp]')"
)


class MCPUnavailable(RuntimeError):
    """Raised when the MCP server is requested but `fastmcp` can't be imported.

    Carries the install hint as its message so callers can print it verbatim.
    """


# ---- structured-payload builders (pure; no fastmcp / no bd) ------------------


def _bd_create_args(item: dict) -> list[str]:
    """Translate a structured bd_create item into `bd create` positional/flag args.

    Mirrors the flag taxonomy `plan._create_issue` uses; the identity triplet is NOT
    added here — `bd.create` appends it for us so the wrapper stays a thin batch loop.
    Assumes `title` is present (the tool checks before calling)."""
    args: list[str] = [str(item["title"]).strip()]
    if item.get("type"):
        args += ["--type", str(item["type"])]
    if item.get("priority") not in (None, ""):
        args += ["-p", str(item["priority"])]
    if item.get("description"):
        args += ["-d", str(item["description"])]
    if item.get("acceptance"):
        args += ["--acceptance", str(item["acceptance"])]
    if item.get("design"):
        args += ["--design", str(item["design"])]
    if item.get("parent"):
        args += ["--parent", str(item["parent"])]
    if labels := (item.get("labels") or []):
        args += ["-l", ",".join(str(label) for label in labels)]
    if deps := (item.get("deps") or []):
        args += ["--deps", ",".join(str(dep) for dep in deps)]
    return args


def _preview_payload(spec: dict, cwd) -> dict:
    """A structured `plan_file --dry-run` preview: epic + issues (topo order, labels,
    deps) + roots. Reuses plan's ordering/label helpers so it never drifts from what
    `file` would actually create. Makes NO bd calls (side-effect-free)."""
    epic = spec["epic"]
    issues = spec["issues"]
    ordered = []
    for issue in plan._topo_order(issues):
        labels = plan._issue_labels(issue, cwd)
        ordered.append(
            {
                "handle": issue["handle"],
                "title": issue["title"],
                "type": str(issue.get("type") or "task"),
                "labels": labels[1] if labels else "",
                "deps": list(issue.get("deps") or []),
            }
        )
    return {
        "dry_run": True,
        "epic": {"title": epic.get("title")},
        "issues": ordered,
        "roots": [r["handle"] for r in plan._roots(issues)],
    }


def _observe_mcp_error(tool: str, exc: BaseException) -> None:
    """Observe an *unhandled* exception escaping an MCP tool body (the boundary error step).

    Logs a structlog ``mcp_tool_error`` line (always, even otel-off), records the exception on the
    active span (ERROR status, no-op when off), and bumps the ``ws.errors`` counter (no-op when
    off). The clean ``ToolError`` surface is raised by the caller. Already-mapped ``ToolError``s
    (the jnv contract — invalid spec, PlanError, WorkError) are *expected* and never reach here, so
    they're surfaced unchanged and not counted as boundary errors."""
    log.get_logger(__name__).error(
        "mcp_tool_error", tool=tool, error_type=type(exc).__name__, error=str(exc)
    )
    otel.record_exception(exc)
    otel.count_error("mcp", type(exc).__name__)


# ---- server ------------------------------------------------------------------


def build_server():
    """Construct and return the ws `FastMCP` server with the complex-input tools wired.

    Raises `MCPUnavailable` (with an install hint) if the `fastmcp` extra is absent.
    Tools return structured (JSON-able) dicts; core exceptions map to `ToolError`s so
    the client gets a clean message rather than a stack trace.
    """
    try:
        from fastmcp import FastMCP
        from fastmcp.exceptions import ToolError
    except ImportError as exc:  # ModuleNotFoundError is a subclass
        raise MCPUnavailable(INSTALL_HINT) from exc

    mcp = FastMCP("ws")

    def _measured_tool(fn):
        """Register *fn* as an mcp.tool with per-tool otel metrics (central seam).

        Times the call, tags ``ws.mcp.tool`` + ``ws.mcp.outcome`` (ok/error), and
        records both a counter and a latency histogram via ``otel.record_mcp_invocation``.
        The tool name is captured from ``fn.__name__`` at registration time.  ``functools.wraps``
        preserves the original signature so FastMCP still introspects the right parameter schema.
        No-op + zero overhead when otel is off — the recording call delegates to ``_instrument``
        which returns a shared no-op shim on the off-path."""
        tool_name = fn.__name__

        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            _start = time.monotonic()
            _outcome = "ok"
            try:
                return fn(*args, **kwargs)
            except ToolError:
                # already-mapped, clean client error (the jnv contract) — expected, surface
                # unchanged. Still outcome=error for the dqw.3 invocation metric, but NOT a
                # boundary error: no second log/span/count.
                _outcome = "error"
                raise
            except Exception as exc:
                # genuine unhandled error: observe (log + span ERROR + error counter) and surface
                # as a clean ToolError so the client never sees a raw traceback.
                _outcome = "error"
                _observe_mcp_error(tool_name, exc)
                raise ToolError(f"{tool_name} failed: {type(exc).__name__}: {exc}") from exc
            finally:
                otel.record_mcp_invocation(tool_name, _outcome, time.monotonic() - _start)

        return mcp.tool(_wrapper)

    @_measured_tool
    def plan_check(spec: dict) -> dict:
        """Validate a molecule spec passed as a structured object (no temp YAML file).

        Runs the same schema + closed-dimension checks as `ws plan check`; returns the
        validation problems as JSON. `valid` is true iff `problems` is empty.
        """
        problems = molecule.validate_spec(spec, config.load())
        return {"valid": not problems, "problems": problems}

    @_measured_tool
    def plan_file(spec: dict, rig: str = "", dry_run: bool = False) -> dict:
        """File a molecule spec (structured object, no temp YAML) into a beads swarm.

        Validates first (invalid → ToolError carrying the problems); then, unless
        `dry_run`, creates the epic + child issues (deps + identity-triplet labels) in
        dependency order, builds the swarm, and opens the kickoff gate — returning the
        new epic id + counts. `dry_run` returns a structured preview and files nothing.
        """
        cfg = config.load()
        cwd = plan._rig_dir(cfg, rig)
        try:
            molecule.validate_or_raise(spec, cfg)
        except molecule.MoleculeError as exc:
            raise ToolError("invalid molecule spec: " + "; ".join(exc.problems)) from exc

        if dry_run:
            return _preview_payload(spec, cwd)

        try:
            result = plan.file_molecule(spec, cwd, resolve_actor("", "", cwd=cwd))
        except plan.PlanError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "epic_id": result.epic_id,
            "issue_count": result.issue_count,
            "root_count": result.root_count,
        }

    @_measured_tool
    def work_refine(
        bead: str,
        squash_plan: dict | None = None,
        autosquash: bool = False,
        since: str = "",
        rig: str = "",
        dry_run: bool = False,
    ) -> dict:
        """Squash a bead branch's local checkpoint noise into conventional digests.

        Exactly one input mode: `squash_plan` (a structured {groups:[{keep,fold,…}]}
        plan), `autosquash` (fold fixup!/squash! markers), or `since` (fold <ref>..tip).
        Safe rewrite — backup branch + byte-identical gate; on any failure the branch is
        restored and a ToolError carries the messages + backup branch. `dry_run` returns
        the would-be subjects without touching git.
        """
        cfg = config.load()
        tmp_path = ""
        try:
            if squash_plan is not None:
                # refine_branch reads the plan from a path/stdin; serialize the structured
                # plan to a short-lived temp JSON so we reuse that seam (vs. reimplementing).
                fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="ws-refine-")
                with os.fdopen(fd, "w") as fh:
                    json.dump(squash_plan, fh)
            try:
                result = work.refine_branch(
                    cfg,
                    rig=rig,
                    bead=bead,
                    plan=tmp_path,
                    autosquash=autosquash,
                    since=since,
                    dry_run=dry_run,
                )
            except work.WorkError as exc:
                msg = "; ".join(exc.messages)
                if exc.backup:
                    msg += f" (restored from backup branch {exc.backup})"
                raise ToolError(msg) from exc
        finally:
            if tmp_path:
                os.unlink(tmp_path)
        return {
            "base": result.base,
            "dry_run": result.dry_run,
            "subjects": result.subjects,
            "backup": result.backup,
            "branch": result.branch,
            "log": result.log,
        }

    @_measured_tool
    def bd_create(issues: list[dict], rig: str = "") -> dict:
        """Batch-create beads from structured items (identity triplet auto-applied).

        Each item: {title (required), type, priority, description, acceptance, design,
        parent, labels[], deps[]}. Forwards to `bd.create` per item (which appends the
        provider/org/repo triplet + enforces label validity). Any failure aborts with a
        ToolError naming the offending item(s); reports the created titles on success.
        """
        cfg = config.load()
        cwd = plan._rig_dir(cfg, rig)
        created: list[str] = []
        failures: list[str] = []
        for idx, item in enumerate(issues):
            if not str(item.get("title") or "").strip():
                failures.append(f"#{idx}: missing 'title'")
                continue
            code, error = bd.create(_bd_create_args(item), cwd)
            if code != 0:
                failures.append(f"#{idx} {item['title']!r}: {error or f'bd exit {code}'}")
            else:
                created.append(str(item["title"]))
        if failures:
            raise ToolError("bd_create failed for: " + "; ".join(failures))
        return {"created": created, "count": len(created)}

    return mcp


def serve() -> None:
    """Run the ws MCP server over stdio (blocking). Raises `MCPUnavailable` if absent."""
    build_server().run()


def main() -> int:
    """`ws-mcp` console-script entrypoint. Returns an exit code (0 ok, 1 unavailable)."""
    try:
        serve()
    except MCPUnavailable as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
