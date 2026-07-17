"""`bh-mcp` / `bh mcp serve` — a FastMCP stdio server exposing bh as MCP tools.

Scaffold built the `FastMCP("bh")` instance + the graceful
absent-`fastmcp` path. This bead wires the *complex-input*
commands as `@mcp.tool` wrappers over the existing Typer-free core fns — the ones
whose value over the CLI is structured I/O (typed specs / squash plans in, JSON
previews + validation problems out), so an MCP client never marshals YAML temp
files or scrapes CLI strings:

  * `plan_check` — validate a molecule spec (structured) → {valid, problems,
                   warnings, missing_acceptance, stubbed_acceptance,
                   acceptance_problems} (the acceptance block feeds the planner
                   skill's drafting modes; 'STUB:' acceptance ⇒ warning).
  * `plan_file`  — file a molecule spec (structured, no temp YAML) → epic/counts,
                   or a structured preview under `dry_run`.
  * `work_refine`— squash local checkpoint noise via a structured plan (or
                   autosquash / since) → the refine report.
  * `bd_create`  — batch-create beads (identity triplet auto-applied) → created ids.

The control-plane verbs join the same surface — they earn their
slot by returning structured results the superintendent session can act on directly:

  * `config_set`  — delta-apply one dotted config key (value carries complex JSON via
                    the jpp4.1 `--json` path) → {ok, problems, old, new}.
  * `hive_add`     — register a provider/org/repo triplet (registry-only, no cwd / no
                    `bd init`) → {prefix, kind, registered}.
  * `hive_onboard` — the headline multi-step: clone-if-absent → hive.init → hub.sync →
                    {cloned, registered, prefix, synced, warnings[]}.
  * `hive_status` — the richer status view → {candidates[], collisions[], violations[],
                    hives[]} (reuses hive.status_payload; backs `bh hive status`).

Simple / bulk CLI-only commands are deliberately NOT exposed — they carry no
structured-I/O advantage over the shell.  Intentionally CLI-only even within the
control plane: `config get` (a single scalar read), `hive rm` (destructive), `bh sync`,
`bh doctor`.  Core exceptions (`MoleculeError`, `PlanError`, `WorkError`, and the
config/hive failure modes) map to FastMCP `ToolError`s so the client sees a clean,
actionable message instead of a stack trace.

`fastmcp` is imported lazily inside `build_server` so that `import beadhive.mcp` — and
therefore the `bh mcp serve` subcommand registration in the CLI — is always safe
even when the optional `[mcp]` extra isn't installed.

## Registering bh with Claude Code

Wire the bh stdio server into every Claude session at user scope with a single
command (run once, persists across projects and hives):

    claude mcp add bh --scope user -- bh mcp serve

After registration, each Claude Code session sees the bh control-plane MCP tools:
`hive_onboard`, `hive_add`, `config_set`, `hive_status`, `hive_list`, `plan_check`.

The `bh mcp install` CLI verb automates this step and handles the `claude` binary
being absent with a clear error. Run `bh mcp install --help` for details.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from . import (
    bd,
    config,
    doctor,
    hive,
    hub,
    log,
    molecule,
    otel,
    plan,
    registry,
    survey,
    toolchain,
    triage,
    validate,
    work,
    work_show,
    worktree,
)
from .identity import resolve_actor, workspace_root

# Hint shown when fastmcp can't be imported — a broken install, since fastmcp is a core
# dependency of bh. Kept as a module constant so both the console-script (`bh-mcp`) and the
# `bh mcp serve` subcommand surface the same text.
INSTALL_HINT = (
    "the bh MCP server needs 'fastmcp', a core dependency of bh that isn't importable —\n"
    "  your install looks broken. reinstall bh:  uv tool install --force 'beadhive[otel]'\n"
    "  (or: pip install --force-reinstall 'beadhive[otel]')"
)


class MCPUnavailable(RuntimeError):
    """Raised when the MCP server is requested but `fastmcp` can't be imported.

    Carries the install hint as its message so callers can print it verbatim.
    """


# Populated by `build_server` on the (lazy) fastmcp import so the `ctx: Context` tool
# annotations — stringified by `from __future__ import annotations` — resolve against module
# globals when FastMCP introspects each tool's schema. Kept None until then so `import beadhive.mcp`
# stays safe even on a broken install missing fastmcp (the lazy-import contract in the docstring).
Context = None


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
    active span (ERROR status, no-op when off), and bumps the ``bh.errors`` counter (no-op when
    off). The clean ``ToolError`` surface is raised by the caller. Already-mapped ``ToolError``s
    (the jnv contract — invalid spec, PlanError, WorkError) are *expected* and never reach here, so
    they're surfaced unchanged and not counted as boundary errors."""
    log.get_logger(__name__).error(
        "mcp_tool_error", tool=tool, error_type=type(exc).__name__, error=str(exc)
    )
    otel.record_exception(exc)
    otel.count_error("mcp", type(exc).__name__)


# ---- server ------------------------------------------------------------------


# ---- MCP measured envelope + registrars (module-level; wired in build_server) ---------------

# Populated by build_server on the lazy fastmcp import (like Context) so the module-level tool /
# resource register functions can map genuine errors onto fastmcp's error types at call time.
ToolError = None
ResourceError = None


def _measured(fn, *, span_name, record, name, expected_exc, mapper, register):
    """Wrap *fn* in the shared timing / outcome / error envelope, then register it via *register*.

    Times the call, tags the outcome ok/error, records a counter + latency histogram via
    ``record(name, outcome, seconds)``, and maps a genuine unhandled error to a clean client error
    via ``mapper`` (an already-mapped ``expected_exc`` surfaces unchanged — still outcome=error, no
    second observe). ``record`` runs in a ``finally`` so it fires on every path. Handles an async
    *fn* (a mutating tool awaiting a notify) and a sync *fn* (reads / computes + every resource)
    with the SAME try/except/finally shape. No-op + zero overhead when otel is off. The single
    envelope behind ``_measured_tool`` / ``_measured_resource`` (was the near-identical sync+async
    wrapper bodies)."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _wrapper(*args, **kwargs):
            _start = time.monotonic()
            _outcome = "ok"
            with otel.span(span_name):
                try:
                    return await fn(*args, **kwargs)
                except expected_exc:
                    _outcome = "error"
                    raise
                except Exception as exc:
                    _outcome = "error"
                    raise mapper(exc) from exc
                finally:
                    record(name, _outcome, time.monotonic() - _start)

        return register(_wrapper)

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        _start = time.monotonic()
        _outcome = "ok"
        with otel.span(span_name):
            try:
                return fn(*args, **kwargs)
            except expected_exc:
                _outcome = "error"
                raise
            except Exception as exc:
                _outcome = "error"
                raise mapper(exc) from exc
            finally:
                record(name, _outcome, time.monotonic() - _start)

    return register(_wrapper)


def _measured_tool(mcp, fn):
    """Register *fn* as an ``mcp.tool`` wrapped in the shared measured envelope. Tool name is
    ``fn.__name__``; a genuine error is observed (log + span ERROR + counter) and mapped to a clean
    ``ToolError`` so the client never sees a traceback. An async *fn* keeps an async wrapper (the
    notify is awaited inside the same envelope)."""
    tool_name = fn.__name__

    def _mapper(exc):
        _observe_mcp_error(tool_name, exc)
        return ToolError(f"{tool_name} failed: {type(exc).__name__}: {exc}")

    return _measured(
        fn,
        span_name=f"{otel.GEN_AI_OP_EXECUTE_TOOL} {tool_name}",
        record=otel.record_mcp_invocation,
        name=tool_name,
        expected_exc=ToolError,
        mapper=_mapper,
        register=mcp.tool,
    )


def _measured_resource(mcp, uri, **kw):
    """Return a decorator registering *fn* as an ``mcp.resource(uri)`` wrapped in the shared
    measured envelope. Defaults ``mime_type="application/json"`` + read-only / idempotent
    annotations; a genuine error maps to a clean ``ResourceError``; the metric is tagged with the
    URI (distinct
    ``bh.mcp.resource`` namespace). Resource handlers stay sync."""
    kw.setdefault("mime_type", "application/json")
    kw.setdefault("annotations", {"readOnlyHint": True, "idempotentHint": True})

    def _decorator(fn):
        resource_name = fn.__name__

        def _mapper(exc):
            _observe_mcp_error(resource_name, exc)
            return ResourceError(f"{resource_name} failed: {type(exc).__name__}: {exc}")

        return _measured(
            fn,
            span_name=f"{otel.GEN_AI_OP_READ_RESOURCE} {uri}",
            record=otel.record_mcp_resource_invocation,
            name=uri,
            expected_exc=ResourceError,
            mapper=_mapper,
            register=lambda w: mcp.resource(uri, **kw)(w),
        )

    return _decorator


async def _notify_updated(ctx, uris) -> None:
    """Emit an MCP ``resources/updated`` notification for each URI in *uris*.

    The headline value beyond pull: a mutating tool calls this after it changes state
    so subscribed clients know to re-read the invalidated resources. *uris* is the
    hardcoded invalidation list for that mutation (a plain ``beadhive://…`` string per URI;
    pydantic's ``AnyUrl`` may normalize a host-only URI to a trailing slash on the wire).
    Uses FastMCP's ``Context.send_notification`` with ``ResourceUpdatedNotification``
    (verified against fastmcp 3.4.x). Notifications fire ONLY on MCP-driven mutations —
    out-of-process CLI changes don't notify (see docs/MCP.md). Sends nothing for an empty
    list.
    """
    from mcp.types import ResourceUpdatedNotification, ResourceUpdatedNotificationParams

    for uri in uris:
        await ctx.send_notification(
            ResourceUpdatedNotification(
                params=ResourceUpdatedNotificationParams(uri=uri)
            )
        )


def _require_triplet(tool: str, provider: str, org: str, repo: str) -> None:
    """Map an empty triplet field to a clean ToolError (the hive cores echo + `typer.Exit`
    on a bad triplet, which would otherwise surface as an opaque boundary error)."""
    for name, val in (("provider", provider), ("org", org), ("repo", repo)):
        if not str(val).strip():
            raise ToolError(f"{tool}: '{name}' is required")


def build_server():
    """Construct and return the bh `FastMCP` server with the complex-input tools wired.

    Raises `MCPUnavailable` (with an install hint) if the `fastmcp` extra is absent.
    Tools return structured (JSON-able) dicts; core exceptions map to `ToolError`s so
    the client gets a clean message rather than a stack trace.
    """
    # `Context` binds the module global (declared here) so the stringified `ctx: Context` tool
    # annotations resolve against module globals when FastMCP introspects the schema; the other
    # imports stay local to build_server (still lazy).
    global Context, ToolError, ResourceError
    try:
        from fastmcp import Context, FastMCP
        from fastmcp.exceptions import ResourceError, ToolError
    except ImportError as exc:  # ModuleNotFoundError is a subclass
        raise MCPUnavailable(INSTALL_HINT) from exc

    mcp = FastMCP(config.BINARY_ALIAS)

    def _tool(fn):
        return _measured_tool(mcp, fn)

    def _resource(uri, **kw):
        return _measured_resource(mcp, uri, **kw)

    _register_config_probes(mcp, _tool, _resource)
    _register_plan_tools(mcp, _tool, _resource)
    _register_hive_tools(mcp, _tool, _resource)
    _register_read_resources(mcp, _tool, _resource)
    _register_toolchain_surface(mcp, _tool, _resource)
    return mcp


def _register_config_probes(mcp, tool, resource):
    """Config / probe / doctor read-only resources."""
    @resource("beadhive://probe/health")
    def probe_health():
        """Probe resource: returns service health. Proves registration; exercised in tests."""
        return {"status": "ok", "service": "bh"}

    @resource("beadhive://config")
    def config_resource():
        """Config resource: returns the resolved config dict via config.load()."""
        return config.load()

    @resource("beadhive://config/{key}")
    def config_key_resource(key: str):
        """Config key resource: returns the value of a dotted config key via config.get_value().

        Returns {ok, problems, value}. The key may contain dots (dotted config path) and is
        passed straight through to config.get_value without modification.
        """
        return config.get_value(key)

    # ---- doctor plane: structured workspace diagnostics ----------------------
    @resource("beadhive://doctor")
    def doctor_resource():
        """Resource: structured `bh doctor` diagnostics (same data the text render consumes).

        Returns doctor.doctor_payload() as JSON — the config/providers/orgs/hives overview plus
        the inventory, disk_usage, fleet_health, worktrees, molecules, mcp, observability, and
        warnings sections. Read-only; `bh doctor` renders from the same data builders, so this
        payload never drifts from the human output. Zero mutation.
        """
        return doctor.doctor_payload()


def _register_plan_tools(mcp, tool, resource):
    """Planning + work tools: plan_check / plan_file / work_refine / bd_create."""
    @tool
    def plan_check(spec: dict) -> dict:
        """Validate a molecule spec passed as a structured object (no temp YAML file).

        Runs the same schema + closed-dimension checks as `bh plan check`; returns the
        validation problems as JSON. `valid` is true iff `problems` is empty. The payload
        also carries the structured acceptance block the planner skill's drafting modes
        consume: `missing_acceptance` / `stubbed_acceptance` id lists, per-record
        `acceptance_problems` ({id, field, severity, message}), and stub `warnings` —
        acceptance text starting 'STUB:' is visible debt (a warning, never an error).
        """
        problems = molecule.validate_spec(spec, config.load())
        summary = molecule.acceptance_summary(spec.get("issues"))
        return {"valid": not problems, "problems": problems, **summary}

    @tool
    async def plan_file(
        spec: dict, hive: str = "", dry_run: bool = False, ctx: Context = None
    ) -> dict:
        """File a molecule spec (structured object, no temp YAML) into a beads swarm.

        Validates first (invalid → ToolError carrying the problems); then, unless
        `dry_run`, creates the epic + child issues (deps + identity-triplet labels) in
        dependency order, builds the swarm, and opens the kickoff gate — returning the
        new epic id + counts. `dry_run` returns a structured preview and files nothing.
        On a real file it emits `resources/updated` for `beadhive://work/ready` + `beadhive://plan/list`.
        """
        cfg = config.load()
        cwd = registry.hive_dir_for(cfg, hive)
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
        await _notify_updated(ctx, ["beadhive://work/ready", "beadhive://plan/list"])
        return {
            "epic_id": result.epic_id,
            "issue_count": result.issue_count,
            "root_count": result.root_count,
        }

    @tool
    def work_refine(
        bead: str,
        squash_plan: dict | None = None,
        autosquash: bool = False,
        since: str = "",
        hive: str = "",
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
                fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="bh-refine-")
                with os.fdopen(fd, "w") as fh:
                    json.dump(squash_plan, fh)
            try:
                result = work.refine_branch(
                    cfg,
                    hive=hive,
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

    @tool
    async def bd_create(issues: list[dict], hive: str = "", ctx: Context = None) -> dict:
        """Batch-create beads from structured items (identity triplet auto-applied).

        Each item: {title (required), type, priority, description, acceptance, design,
        parent, labels[], deps[]}. Forwards to `bd.create` per item (which appends the
        provider/org/repo triplet + enforces label validity). Any failure aborts with a
        ToolError naming the offending item(s); reports the created titles on success and
        emits `resources/updated` for `beadhive://work/ready` + `beadhive://work/intake`.
        """
        cfg = config.load()
        cwd = registry.hive_dir_for(cfg, hive)
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
        await _notify_updated(ctx, ["beadhive://work/ready", "beadhive://work/intake"])
        return {"created": created, "count": len(created)}


def _register_hive_tools(mcp, tool, resource):
    """Hive lifecycle tools + hive status/survey resources."""
    @tool
    def hive_list() -> dict:
        """List discoverable-but-unregistered repos under the known providers/orgs.

        Diffs git-workspace's tracked repos (read from `workspace-lock.toml` — already
        fetched, ZERO API calls) against the registered hives, returning
        `{candidates:[...], registered:[...]}` as `provider/org/repo` triplets. `candidates`
        are repos you could `bh hive add`; `registered` are the hives already in the registry.
        Backs `bh hive list --available`.
        """
        return hive.available(config.load())

    @resource("beadhive://hive/list")
    def hive_list_resource():
        """Resource: discoverable-but-unregistered repos (same payload as hive_list tool).

        Returns {candidates:[...], registered:[...]} as provider/org/repo triplets — a
        lock-file diff against the registered hives, zero API calls. Dual-exposed so
        tool-only clients remain unaffected.
        """
        return hive.available(config.load())

    @tool
    async def config_set(
        key: str,
        value: str | int | float | bool | list | dict,
        type: str = "",
        ctx: Context = None,
    ) -> dict:
        """Delta-apply one dotted config key to ~/.beadhive/config.yaml (the jpp4.1 core).

        Sets a single `key` per call; `value` carries the new value (a scalar, or a full
        list/map for structured keys). `type` is an optional coercion hint: `"json"` treats a
        string `value` as JSON source, `"string"` forces a literal string (no true/int
        coercion); omitted, a string gets the CLI's friendly coercion (`true|false`→bool,
        digits→int) while a non-string `value` round-trips exactly. Returns the core's
        `{ok, problems, old, new}` — a validation error (e.g. a bad `otel.protocol`) comes back
        as `ok=false` with `problems`, writing nothing, rather than raising. On a successful
        write it emits `resources/updated` for `beadhive://config` + `beadhive://config/{key}`.
        """
        if type == "json":
            raw, as_json = (value if isinstance(value, str) else json.dumps(value)), True
        elif type == "string":
            raw, as_json = json.dumps(str(value)), True
        elif isinstance(value, str):
            raw, as_json = value, False
        else:
            raw, as_json = json.dumps(value), True
        result = config.set_value(key, raw, as_json=as_json)
        if result.get("ok"):
            await _notify_updated(ctx, ["beadhive://config", f"beadhive://config/{key}"])
        return result

    @tool
    async def hive_add(
        provider: str,
        org: str,
        repo: str,
        prefix: str = "",
        kind: str = "",
        upstream: str = "",
        ctx: Context = None,
    ) -> dict:
        """Register a `provider/org/repo` triplet as a hive — registry-only (jpp4.2 `hive.add`).

        No cwd required and no `bd init` (the repo may be uncloned); when `prefix` is blank it is
        derived from the org code + repo. Returns the effective `{prefix, kind, registered}` read
        back from the registry. Use `bh hive rm` (CLI-only, destructive) to unregister. Emits
        `resources/updated` for `beadhive://hive/status`, `beadhive://hive/list`, `beadhive://hive/survey`.
        """
        _require_triplet("hive_add", provider, org, repo)
        hive.add(f"{provider}/{org}/{repo}", prefix=prefix, kind=kind, upstream=upstream)
        entry = registry.find_entry(config.load(), provider, org, repo)
        if entry is None:
            raise ToolError(f"hive_add: {provider}/{org}/{repo} was not registered")
        await _notify_updated(
            ctx, ["beadhive://hive/status", "beadhive://hive/list", "beadhive://hive/survey"]
        )
        return {"prefix": str(entry["prefix"]), "kind": str(entry["kind"]), "registered": True}

    @tool
    async def hive_onboard(
        provider: str,
        org: str,
        repo: str,
        clone_url: str = "",
        furnish: bool | None = None,
        claude: bool = False,
        skills: bool = False,
        observaloop: bool = False,
        ctx: Context = None,
    ) -> dict:
        """Onboard a hive end-to-end (the headline multi-step — jpp4.3 `hive.onboard`).

        Resolves `target = $GIT_WORKSPACE/provider/org/repo`; clones it down when absent and a
        `clone_url` is given (absent + no url → ToolError), runs the full `hive init` against the
        target, then syncs the hub. Default is ZERO-footprint (nothing tracked, nothing
        committed); `furnish=true` declares tracked in-repo AGF furniture (ownership-gated),
        and `claude`/`skills` imply it. Returns `{cloned, registered, prefix, synced,
        warnings[]}` and emits `resources/updated` for `beadhive://hive/status`,
        `beadhive://hive/list`, `beadhive://hive/survey`.
        """
        _require_triplet("hive_onboard", provider, org, repo)
        target = Path(workspace_root()) / provider / org / repo
        pre_exists = target.exists()
        if not pre_exists and not clone_url:
            raise ToolError(
                f"hive_onboard: {target} does not exist — pass clone_url to clone it down first"
            )
        # The prefix-derivation warnings onboard would surface, computed read-only up front.
        _, warnings = registry.derive_prefix(provider, org, repo, "", config.load())
        hive.onboard(
            f"{provider}/{org}/{repo}",
            clone_url=clone_url,
            furnish=furnish,
            claude=claude,
            skills=skills,
            observaloop=observaloop,
        )
        entry = registry.find_entry(config.load(), provider, org, repo)
        await _notify_updated(
            ctx, ["beadhive://hive/status", "beadhive://hive/list", "beadhive://hive/survey"]
        )
        return {
            "cloned": not pre_exists,
            "registered": entry is not None,
            "prefix": str(entry["prefix"]) if entry else "",
            "synced": True,
            "warnings": warnings,
        }

    @tool
    def hive_status() -> dict:
        """Richer workspace status view — fleet health (backs `bh hive status`).

        Returns `{candidates[], collisions[], violations[], hives[]}`: `candidates` are tracked-
        but-unregistered repos (zero-API lock-file diff, via `hive.available`); `collisions` are
        prefixes claimed by more than one hive; `violations` are required-org hives whose prefix
        breaks the `<code>-` convention; `hives` are the registered hives. The structured superset
        of `hive_list` — call that for just the add candidates.
        """
        return hive.status_payload(config.load())

    @resource("beadhive://hive/status")
    def hive_status_resource():
        """Resource: richer workspace status view (same payload as hive_status tool).

        Returns {candidates[], collisions[], violations[], hives[]}: candidates are
        tracked-but-unregistered repos; collisions are prefixes claimed by more than one
        hive; violations are required-org hives whose prefix breaks the `<code>-` convention;
        hives are the registered hives. Dual-exposed so tool-only clients remain unaffected.
        """
        return hive.status_payload(config.load())

    @resource("beadhive://hive/survey")
    def hives_survey_resource():
        """Resource: fleet onboarding table, one row per on-disk repo.

        Returns survey.collect_rows(cfg) as JSON — the same payload the survey
        command renders, but structured for MCP clients. Read-only; zero mutation.
        """
        return survey.collect_rows(config.load())


def _register_read_resources(mcp, tool, resource):
    """Read-only resources: labels / worktrees / work / plans / hq planes."""
    # ---- labels plane -----------------------------------------------------------

    @resource("beadhive://label/validation")
    def labels_validation_resource():
        """Resource: label validation findings as structured data (labels plane).

        Returns {has_violations, required_violations, issue_problems, db_ok}:
        has_violations is True iff any finding (registry or per-issue);
        required_violations are required-org prefix violations (registry.required_violations);
        issue_problems are per-bead label problems (validate._issue_checks);
        db_ok is False when bd is unreachable (per-issue checks were skipped).
        Assembled from validate.* + registry.required_violations; no new lint logic.
        """
        cfg = config.load()
        cwd = registry.hive_dir_for(cfg, hive="")
        issue_problems, db_ok = validate._issue_checks(cfg, cwd)
        rv = registry.required_violations(cfg)
        return {
            "has_violations": validate.has_violations(cfg, cwd),
            "required_violations": rv,
            "issue_problems": issue_problems,
            "db_ok": db_ok,
        }

    # ---- worktrees plane --------------------------------------------------------

    @resource("beadhive://worktree/list")
    def worktrees_resource():
        """Resource: per-worktree classification status for all managed hives.

        Returns the same ``WtStatus`` list that ``bh worktree status --json`` emits,
        via the Typer-free ``worktree.status_rows()`` core — SAFE / ACTIVE / DIRTY /
        REVIEW / UNMERGED / LANDED_REBASED / DETACHED / MERGED_ORPHAN / ABANDONED.
        Hub-scoped (all managed hives); zero mutation, read-only.
        """
        return [s.as_dict() for s in worktree.status_rows()]

    # ---- work plane -------------------------------------------------------------

    @resource("beadhive://work/ready")
    def work_ready_resource():
        """Resource: ready (unblocked, dependency-ordered) beads for the current hive.

        Returns the same JSON as `bh work ready --json` via bd.json(["ready"], cwd) — the
        coordinator's most re-read dashboard. Resolves the hive cwd via registry.hive_dir_for so it
        targets the same directory the work.ready verb does. Returns an empty list when bd
        reports no ready beads or exits non-zero.
        """
        cfg = config.load()
        cwd = registry.hive_dir_for(cfg, hive="")
        return bd.json(["ready"], cwd) or []

    @resource("beadhive://work/intake")
    def work_intake_resource():
        """Resource: untriaged intake inbox payload (same as `bh work intake --json`).

        Returns {rows, dupes}: rows are the open untriaged intake beads; dupes are the
        likely-duplicate pairs (mechanical dedup via `bd find-duplicates`). Changes as
        reports arrive — high pull, high signal.
        """
        cwd = registry.hive_dir_for(config.load(), "")
        return triage.intake_payload(cwd)

    @resource("beadhive://work/intake/dupes")
    def work_intake_dupes_resource():
        """Resource: duplicate-pair candidates scoped to the current hive's intake queue.

        Returns the subset of mechanical-dedup pairs (via triage.find_dupes /
        triage.dupes_touching) where at least one side is an open intake bead — the
        same data the beadhive://work/intake 'dupes' field carries, exposed separately so
        clients can poll it cheaply without re-fetching the full intake rows. Returns
        an empty list when bd reports no pairs or exits non-zero.
        """
        cfg = config.load()
        cwd = registry.hive_dir_for(cfg, hive="")
        pairs = triage.find_dupes(cwd)
        rows = triage.list_intake(cwd)
        ids = [r.get("id") for r in rows]
        return triage.dupes_touching(pairs, ids)

    @resource("beadhive://work/issue/{id}")
    def work_issue_resource(id: str):
        """Resource: single-bead lookup by id (template resource).

        Returns the normalized bead dict via bd.show — resolves bd's object-or-1-list
        shape. Returns None when the bead is not found. Resolves cwd via registry.hive_dir_for
        so it targets the same hive as the sibling work resources.
        """
        cwd = registry.hive_dir_for(config.load(), hive="")
        return bd.show(id, cwd)

    @resource("beadhive://work/show/{id}")
    def work_show_resource(id: str):
        """Resource: bead branch local history payload (template resource).

        Returns the same ``{base, max_commits, commits, gates}`` payload as
        ``bh work show --json`` via ``work_show.show_payload`` — the base commit SHA
        (7-char abbreviated), the configured commit limit, the flagged commit rows for
        ``base..branch`` of the named bead, and every gate touching the bead (id, kind,
        open/resolved status, reason snippet — open first). Resolves the hive via
        ``worktree.locate`` (hive="" → cwd default). Returns an empty commits list when
        the branch or integration base cannot be resolved.
        """
        cfg = config.load()
        entry, main, _target, branch = worktree.locate(cfg, "", id)
        return work_show.show_payload(cfg, entry, id, branch, main)

    @resource("beadhive://work/schedule/{epic}")
    def work_schedule_resource(epic: str):
        """Resource: cost-model dispatch plan for a molecule (template resource).

        Returns the same ``{groups, singletons, coordinators, max_depth}`` payload as
        ``bh work schedule --json`` via ``work.schedule_payload`` — groups are enriched
        with ``model`` (max tier across members) and coordinators carry their
        ``dispatch`` string.  Resolves the hive via ``worktree.locate`` (hive="" → cwd
        default).  Raises a ``ResourceError`` when the epic is not found in this hive.
        """
        cfg = config.load()
        entry, main, _target, _branch = worktree.locate(cfg, "", epic)
        try:
            return work.schedule_payload(epic, cfg, entry, main)
        except ValueError as exc:
            raise ResourceError(str(exc)) from exc

    # ---- plans plane ---------------------------------------------------------

    @resource("beadhive://plan/list")
    def plans_resource():
        """Resource: swarm list for the current hive (planning-plane molecule list).

        Returns the same JSON as `bd swarm list --json` via bd.json(["swarm", "list"], cwd)
        — the coordinator's molecule dashboard. Resolves cwd via registry.hive_dir_for so it
        targets the same hive directory the plan verbs use. Returns None when bd exits
        non-zero or the output is not valid JSON.
        """
        cwd = registry.hive_dir_for(config.load(), hive="")
        return bd.json(["swarm", "list"], cwd)

    @resource("beadhive://plan/{ref}")
    def plan_resource(ref: str):
        """Resource: single molecule status by swarm ref (template resource).

        Returns the same JSON as `bd swarm status <ref> --json` via
        bd.json(["swarm", "status", ref], cwd). Resolves cwd via registry.hive_dir_for so it
        targets the same hive directory the plan verbs use. Returns None when the swarm
        ref is not found or bd exits non-zero.
        """
        cwd = registry.hive_dir_for(config.load(), hive="")
        return bd.json(["swarm", "status", ref], cwd)

    # ---- hq plane ---------------------------------------------------------------

    @resource("beadhive://hq/intake")
    def hq_intake_resource():
        """Resource: fleet-wide untriaged intake inbox, aggregated across the hub.

        Resolves the aggregation target via hub._aggregation_target() (durable HQ store
        when one is registered, else the legacy hub). Returns the open intake:untriaged
        beads as a list via bd.json. Returns an empty list when the hub is absent or
        unavailable rather than raising.
        """
        from .state import INTAKE_UNTRIAGED

        hub_dir, _prefix = hub._aggregation_target()
        if not (hub_dir / ".beads").is_dir():
            return []
        return bd.json(["list", "--label", INTAKE_UNTRIAGED, "--status", "open"], hub_dir) or []


def _register_toolchain_surface(mcp, tool, resource):
    """Toolchain plane (bh-d0kb, knowledge-only): list/show resources + the exec tool.

    The resources share the CLI's payload producers (toolchain.list_payload /
    show_payload) so `bh toolchain … --json` and the MCP shape never drift. They exist so
    an agent can DISCOVER a repo's entrypoints and SUGGEST config (validate_cmd, init
    rules) to the operator — bh never applies a template's suggestions automatically.
    """

    @resource("beadhive://toolchain/list")
    def toolchain_list_resource():
        """Resource: declared toolchains + the effective template registry.

        Returns the same {declared, registry} payload as `bh toolchain list --json` via
        toolchain.list_payload — declared names (per-hive entry for cwd's hive > global
        worktrees.toolchain) and the registry (shipped built-ins overlaid with
        worktrees.toolchains). Knowledge-only metadata; zero mutation.
        """
        cfg = config.load()
        return toolchain.list_payload(cfg, registry.current_hive(cfg) or {})

    @resource("beadhive://toolchain/show/{name}")
    def toolchain_show_resource(name: str):
        """Resource: one toolchain's entrypoint listing + suggestions (template resource).

        Returns the same payload as `bh toolchain show <name> --json` via
        toolchain.show_payload — runs the template's read-only entrypoints_cmd in the
        current hive's main clone and bundles {name, entrypoints_cmd, entrypoints,
        exit_code, suggestions:{init, validate_cmd}}. The suggestions are what an agent
        proposes to the operator, never applied by bh. Unknown name raises cleanly.
        """
        cfg = config.load()
        return toolchain.show_payload(cfg, name, registry.hive_dir_for(cfg, hive=""))

    @tool
    def toolchain_exec(argv: list[str], hive: str = "") -> dict:
        """Invoke an entrypoint in the hive's main clone (backs `bh toolchain exec -- …`).

        Runs `argv` through bh's run() seam with the hive's main clone as cwd (`hive`
        selects a hive; blank targets cwd's hive) and returns {exit_code, stdout,
        stderr}. Refuses an empty argv. The exec seam for entrypoints an agent discovered
        via beadhive://toolchain/show/{name}.
        """
        cfg = config.load()
        try:
            res = toolchain.exec_entrypoint(
                argv, registry.hive_dir_for(cfg, hive), capture=True
            )
        except toolchain.ToolchainError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "exit_code": res.returncode,
            "stdout": res.stdout or "",
            "stderr": res.stderr or "",
        }


def serve() -> None:
    """Run the bh MCP server over stdio (blocking). Raises `MCPUnavailable` if absent."""
    build_server().run()


def main() -> int:
    """`bh-mcp` console-script entrypoint. Returns an exit code (0 ok, 1 unavailable)."""
    try:
        serve()
    except MCPUnavailable as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
