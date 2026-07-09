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

The control-plane verbs join the same surface — they earn their
slot by returning structured results the superintendent session can act on directly:

  * `config_set`  — delta-apply one dotted config key (value carries complex JSON via
                    the jpp4.1 `--json` path) → {ok, problems, old, new}.
  * `rig_add`     — register a provider/org/repo triplet (registry-only, no cwd / no
                    `bd init`) → {prefix, kind, registered}.
  * `rig_onboard` — the headline multi-step: clone-if-absent → rig.init → hub.sync →
                    {cloned, registered, prefix, synced, warnings[]}.
  * `rigs_status` — the richer status view → {candidates[], collisions[], violations[],
                    rigs[]} (reuses rig.available + the registry repos-sync internals).

Simple / bulk CLI-only commands are deliberately NOT exposed — they carry no
structured-I/O advantage over the shell.  Intentionally CLI-only even within the
control plane: `config get` (a single scalar read), `rig rm` (destructive), `ws sync`,
`ws doctor`.  Core exceptions (`MoleculeError`, `PlanError`, `WorkError`, and the
config/rig failure modes) map to FastMCP `ToolError`s so the client sees a clean,
actionable message instead of a stack trace.

`fastmcp` is imported lazily inside `build_server` so that `import beadhive.mcp` — and
therefore the `ws mcp serve` subcommand registration in the CLI — is always safe
even when the optional `[mcp]` extra isn't installed.

## Registering ws with Claude Code

Wire the ws stdio server into every Claude session at user scope with a single
command (run once, persists across projects and rigs):

    claude mcp add ws --scope user -- ws mcp serve

After registration, each Claude Code session sees the ws control-plane MCP tools:
`rig_onboard`, `rig_add`, `config_set`, `rigs_status`, `rigs_available`, `plan_check`.

The `ws mcp install` CLI verb automates this step and handles the `claude` binary
being absent with a clear error. Run `ws mcp install --help` for details.
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
    hub,
    log,
    molecule,
    otel,
    plan,
    registry,
    rig,
    survey,
    triage,
    validate,
    work,
    work_show,
    worktree,
)
from .identity import resolve_actor, workspace_root

# Hint shown when fastmcp can't be imported — a broken install, since fastmcp is a core
# dependency of ws. Kept as a module constant so both the console-script (`ws-mcp`) and the
# `ws mcp serve` subcommand surface the same text.
INSTALL_HINT = (
    "the ws MCP server needs 'fastmcp', a core dependency of ws that isn't importable —\n"
    "  your install looks broken. reinstall ws:  uv tool install --force 'ws[otel]'\n"
    "  (or: pip install --force-reinstall 'ws[otel]')"
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
    # `Context` binds the module global (declared here) so the stringified `ctx: Context` tool
    # annotations resolve against module globals when FastMCP introspects the schema; the other
    # imports stay local to build_server (still lazy).
    global Context
    try:
        from fastmcp import Context, FastMCP
        from fastmcp.exceptions import ResourceError, ToolError
        from mcp.types import (
            ResourceUpdatedNotification,
            ResourceUpdatedNotificationParams,
        )
    except ImportError as exc:  # ModuleNotFoundError is a subclass
        raise MCPUnavailable(INSTALL_HINT) from exc

    mcp = FastMCP("ws")

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
        for uri in uris:
            await ctx.send_notification(
                ResourceUpdatedNotification(
                    params=ResourceUpdatedNotificationParams(uri=uri)
                )
            )

    def _measured_tool(fn):
        """Register *fn* as an mcp.tool with per-tool otel metrics (central seam).

        Times the call, tags ``ws.mcp.tool`` + ``ws.mcp.outcome`` (ok/error), and
        records both a counter and a latency histogram via ``otel.record_mcp_invocation``.
        The tool name is captured from ``fn.__name__`` at registration time.  ``functools.wraps``
        preserves the original signature so FastMCP still introspects the right parameter schema
        — including a ``ctx: Context`` param FastMCP injects, which threads through ``*args,
        **kwargs`` untouched. An **async** *fn* (a mutating tool that awaits ``_notify_updated``)
        gets an async wrapper so the notify is awaited inside the same timing/error envelope; a
        sync read/compute *fn* keeps the sync wrapper. No-op + zero overhead when otel is off —
        the recording call delegates to ``_instrument`` which returns a shared no-op shim on the
        off-path."""
        tool_name = fn.__name__

        def _map_error(exc):
            """Genuine unhandled error → observe (log + span ERROR + counter), return a clean
            ToolError so the client never sees a raw traceback. The execute_tool span is current
            here so ``_observe_mcp_error``'s ``record_exception`` has a recording span to mark
            ERROR."""
            _observe_mcp_error(tool_name, exc)
            return ToolError(f"{tool_name} failed: {type(exc).__name__}: {exc}")

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def _wrapper(*args, **kwargs):
                _start = time.monotonic()
                _outcome = "ok"
                with otel.span(f"{otel.GEN_AI_OP_EXECUTE_TOOL} {tool_name}"):
                    try:
                        return await fn(*args, **kwargs)
                    except ToolError:
                        # already-mapped, clean client error — surface unchanged, still
                        # outcome=error (metric) but not a boundary error (no second observe).
                        _outcome = "error"
                        raise
                    except Exception as exc:
                        _outcome = "error"
                        raise _map_error(exc) from exc
                    finally:
                        otel.record_mcp_invocation(tool_name, _outcome, time.monotonic() - _start)

            return mcp.tool(_wrapper)

        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            _start = time.monotonic()
            _outcome = "ok"
            with otel.span(f"{otel.GEN_AI_OP_EXECUTE_TOOL} {tool_name}"):
                try:
                    return fn(*args, **kwargs)
                except ToolError:
                    # already-mapped, clean client error (the jnv contract) — expected, surface
                    # unchanged. Still outcome=error for the dqw.3 invocation metric, but NOT a
                    # boundary error: no second log/span/count.
                    _outcome = "error"
                    raise
                except Exception as exc:
                    _outcome = "error"
                    raise _map_error(exc) from exc
                finally:
                    otel.record_mcp_invocation(tool_name, _outcome, time.monotonic() - _start)

        return mcp.tool(_wrapper)

    def _measured_resource(uri, **kw):
        """Register *fn* as a mcp.resource with per-resource otel metrics (central seam).

        Defaults ``mime_type="application/json"`` and ``annotations={readOnlyHint, idempotentHint}``
        (both True — resources are read-only + idempotent).  Times the call, tags
        ``ws.mcp.resource`` + ``ws.mcp.outcome`` (ok/error), and records both a counter and a
        latency histogram via ``otel.record_mcp_resource_invocation``.  The span uses
        ``GEN_AI_OP_READ_RESOURCE`` so resource spans nest cleanly under any parent.
        ``functools.wraps`` preserves the original signature so FastMCP introspects the right
        schema.  No-op + zero overhead when otel is off.  Keep resource handler fns sync."""
        kw.setdefault("mime_type", "application/json")
        kw.setdefault("annotations", {"readOnlyHint": True, "idempotentHint": True})

        def _decorator(fn):
            resource_name = fn.__name__

            @functools.wraps(fn)
            def _wrapper(*args, **kwargs):
                _start = time.monotonic()
                _outcome = "ok"
                with otel.span(f"{otel.GEN_AI_OP_READ_RESOURCE} {uri}"):
                    try:
                        return fn(*args, **kwargs)
                    except ResourceError:
                        # already-mapped clean client error — surface unchanged, still outcome=error
                        _outcome = "error"
                        raise
                    except Exception as exc:
                        # genuine unhandled error: observe and surface as a clean ResourceError
                        _outcome = "error"
                        _observe_mcp_error(resource_name, exc)
                        raise ResourceError(
                            f"{resource_name} failed: {type(exc).__name__}: {exc}"
                        ) from exc
                    finally:
                        otel.record_mcp_resource_invocation(
                            uri, _outcome, time.monotonic() - _start
                        )

            return mcp.resource(uri, **kw)(_wrapper)

        return _decorator

    @_measured_resource("beadhive://probe/health")
    def probe_health():
        """Probe resource: returns service health. Proves registration; exercised in tests."""
        return {"status": "ok", "service": "ws"}

    @_measured_resource("beadhive://config")
    def config_resource():
        """Config resource: returns the resolved config dict via config.load()."""
        return config.load()

    @_measured_resource("beadhive://config/{key}")
    def config_key_resource(key: str):
        """Config key resource: returns the value of a dotted config key via config.get_value().

        Returns {ok, problems, value}. The key may contain dots (dotted config path) and is
        passed straight through to config.get_value without modification.
        """
        return config.get_value(key)

    # ---- doctor plane: structured workspace diagnostics ----------------------
    @_measured_resource("beadhive://doctor")
    def doctor_resource():
        """Resource: structured `ws doctor` diagnostics (same data the text render consumes).

        Returns doctor.doctor_payload() as JSON — the config/providers/orgs/rigs overview plus
        the inventory, disk_usage, fleet_health, worktrees, molecules, mcp, observability, and
        warnings sections. Read-only; `ws doctor` renders from the same data builders, so this
        payload never drifts from the human output. Zero mutation.
        """
        return doctor.doctor_payload()

    def _require_triplet(tool: str, provider: str, org: str, repo: str) -> None:
        """Map an empty triplet field to a clean ToolError (the rig cores echo + `typer.Exit`
        on a bad triplet, which would otherwise surface as an opaque boundary error)."""
        for name, val in (("provider", provider), ("org", org), ("repo", repo)):
            if not str(val).strip():
                raise ToolError(f"{tool}: '{name}' is required")

    @_measured_tool
    def plan_check(spec: dict) -> dict:
        """Validate a molecule spec passed as a structured object (no temp YAML file).

        Runs the same schema + closed-dimension checks as `ws plan check`; returns the
        validation problems as JSON. `valid` is true iff `problems` is empty.
        """
        problems = molecule.validate_spec(spec, config.load())
        return {"valid": not problems, "problems": problems}

    @_measured_tool
    async def plan_file(
        spec: dict, rig: str = "", dry_run: bool = False, ctx: Context = None
    ) -> dict:
        """File a molecule spec (structured object, no temp YAML) into a beads swarm.

        Validates first (invalid → ToolError carrying the problems); then, unless
        `dry_run`, creates the epic + child issues (deps + identity-triplet labels) in
        dependency order, builds the swarm, and opens the kickoff gate — returning the
        new epic id + counts. `dry_run` returns a structured preview and files nothing.
        On a real file it emits `resources/updated` for `beadhive://work/ready` + `beadhive://plans`.
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
        await _notify_updated(ctx, ["beadhive://work/ready", "beadhive://plans"])
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
    async def bd_create(issues: list[dict], rig: str = "", ctx: Context = None) -> dict:
        """Batch-create beads from structured items (identity triplet auto-applied).

        Each item: {title (required), type, priority, description, acceptance, design,
        parent, labels[], deps[]}. Forwards to `bd.create` per item (which appends the
        provider/org/repo triplet + enforces label validity). Any failure aborts with a
        ToolError naming the offending item(s); reports the created titles on success and
        emits `resources/updated` for `beadhive://work/ready` + `beadhive://work/intake`.
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
        await _notify_updated(ctx, ["beadhive://work/ready", "beadhive://work/intake"])
        return {"created": created, "count": len(created)}

    @_measured_tool
    def rigs_available() -> dict:
        """List discoverable-but-unregistered repos under the known providers/orgs.

        Diffs git-workspace's tracked repos (read from `workspace-lock.toml` — already
        fetched, ZERO API calls) against the registered rigs, returning
        `{candidates:[...], registered:[...]}` as `provider/org/repo` triplets. `candidates`
        are repos you could `ws rig add`; `registered` are the rigs already in the registry.
        """
        return rig.available(config.load())

    @_measured_resource("beadhive://rigs/available")
    def rigs_available_resource():
        """Resource: discoverable-but-unregistered repos (same payload as rigs_available tool).

        Returns {candidates:[...], registered:[...]} as provider/org/repo triplets — a
        lock-file diff against the registered rigs, zero API calls. Dual-exposed so
        tool-only clients remain unaffected.
        """
        return rig.available(config.load())

    @_measured_tool
    async def config_set(
        key: str,
        value: str | int | float | bool | list | dict,
        type: str = "",
        ctx: Context = None,
    ) -> dict:
        """Delta-apply one dotted config key to ~/.ws/config.yaml (the jpp4.1 core).

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

    @_measured_tool
    async def rig_add(
        provider: str,
        org: str,
        repo: str,
        prefix: str = "",
        kind: str = "",
        upstream: str = "",
        ctx: Context = None,
    ) -> dict:
        """Register a `provider/org/repo` triplet as a rig — registry-only (jpp4.2 `rig.add`).

        No cwd required and no `bd init` (the repo may be uncloned); when `prefix` is blank it is
        derived from the org code + repo. Returns the effective `{prefix, kind, registered}` read
        back from the registry. Use `ws rig rm` (CLI-only, destructive) to unregister. Emits
        `resources/updated` for `beadhive://rigs/status`, `beadhive://rigs/available`, `beadhive://rigs/survey`.
        """
        _require_triplet("rig_add", provider, org, repo)
        rig.add(f"{provider}/{org}/{repo}", prefix=prefix, kind=kind, upstream=upstream)
        entry = registry.find_entry(config.load(), provider, org, repo)
        if entry is None:
            raise ToolError(f"rig_add: {provider}/{org}/{repo} was not registered")
        await _notify_updated(
            ctx, ["beadhive://rigs/status", "beadhive://rigs/available", "beadhive://rigs/survey"]
        )
        return {"prefix": str(entry["prefix"]), "kind": str(entry["kind"]), "registered": True}

    @_measured_tool
    async def rig_onboard(
        provider: str,
        org: str,
        repo: str,
        clone_url: str = "",
        prime: bool = False,
        claude: bool = False,
        skills: bool = False,
        observaloop: bool = False,
        ctx: Context = None,
    ) -> dict:
        """Onboard a rig end-to-end (the headline multi-step — jpp4.3 `rig.onboard`).

        Resolves `target = $GIT_WORKSPACE/provider/org/repo`; clones it down when absent and a
        `clone_url` is given (absent + no url → ToolError), runs the full `rig init` against the
        target, then syncs the hub. Optional `prime`/`claude`/`skills`/`observaloop` install the
        matching agent integrations. Returns `{cloned, registered, prefix, synced, warnings[]}`
        and emits `resources/updated` for `beadhive://rigs/status`, `beadhive://rigs/available`,
        `beadhive://rigs/survey`.
        """
        _require_triplet("rig_onboard", provider, org, repo)
        target = Path(workspace_root()) / provider / org / repo
        pre_exists = target.exists()
        if not pre_exists and not clone_url:
            raise ToolError(
                f"rig_onboard: {target} does not exist — pass clone_url to clone it down first"
            )
        # The prefix-derivation warnings onboard would surface, computed read-only up front.
        _, warnings = registry.derive_prefix(provider, org, repo, "", config.load())
        rig.onboard(
            f"{provider}/{org}/{repo}",
            clone_url=clone_url,
            prime=prime,
            claude=claude,
            skills=skills,
            observaloop=observaloop,
        )
        entry = registry.find_entry(config.load(), provider, org, repo)
        await _notify_updated(
            ctx, ["beadhive://rigs/status", "beadhive://rigs/available", "beadhive://rigs/survey"]
        )
        return {
            "cloned": not pre_exists,
            "registered": entry is not None,
            "prefix": str(entry["prefix"]) if entry else "",
            "synced": True,
            "warnings": warnings,
        }

    @_measured_tool
    def rigs_status() -> dict:
        """Richer workspace status view (reuses the registry repos-sync internals).

        Returns `{candidates[], collisions[], violations[], rigs[]}`: `candidates` are tracked-
        but-unregistered repos (zero-API lock-file diff, via `rig.available`); `collisions` are
        prefixes claimed by more than one rig; `violations` are required-org rigs whose prefix
        breaks the `<code>-` convention; `rigs` are the registered rigs. The structured superset
        of `rigs_available` — call that for just the add candidates.
        """
        cfg = config.load()
        rigs = [
            {
                "provider": str(e["provider"]),
                "org": str(e["org"]),
                "repo": str(e["repo"]),
                "prefix": str(e["prefix"]),
                "kind": str(e.get("kind", "")),
                **({"upstream": str(e["upstream"])} if e.get("upstream") else {}),
            }
            for e in cfg.get("managed_repos", [])
        ]
        return {
            "candidates": rig.available(cfg)["candidates"],
            "collisions": registry.prefix_collisions(cfg),
            "violations": registry.required_violations(cfg),
            "rigs": rigs,
        }

    @_measured_resource("beadhive://rigs/status")
    def rigs_status_resource():
        """Resource: richer workspace status view (same payload as rigs_status tool).

        Returns {candidates[], collisions[], violations[], rigs[]}: candidates are
        tracked-but-unregistered repos; collisions are prefixes claimed by more than one
        rig; violations are required-org rigs whose prefix breaks the `<code>-` convention;
        rigs are the registered rigs. Dual-exposed so tool-only clients remain unaffected.
        """
        cfg = config.load()
        rigs = [
            {
                "provider": str(e["provider"]),
                "org": str(e["org"]),
                "repo": str(e["repo"]),
                "prefix": str(e["prefix"]),
                "kind": str(e.get("kind", "")),
                **({"upstream": str(e["upstream"])} if e.get("upstream") else {}),
            }
            for e in cfg.get("managed_repos", [])
        ]
        return {
            "candidates": rig.available(cfg)["candidates"],
            "collisions": registry.prefix_collisions(cfg),
            "violations": registry.required_violations(cfg),
            "rigs": rigs,
        }

    @_measured_resource("beadhive://rigs/survey")
    def rigs_survey_resource():
        """Resource: fleet onboarding table, one row per on-disk repo.

        Returns survey.collect_rows(cfg) as JSON — the same payload the survey
        command renders, but structured for MCP clients. Read-only; zero mutation.
        """
        return survey.collect_rows(config.load())

    # ---- labels plane -----------------------------------------------------------

    @_measured_resource("beadhive://labels/validation")
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
        cwd = plan._rig_dir(cfg, rig="")
        issue_problems, db_ok = validate._issue_checks(cfg, cwd)
        rv = registry.required_violations(cfg)
        return {
            "has_violations": validate.has_violations(cfg, cwd),
            "required_violations": rv,
            "issue_problems": issue_problems,
            "db_ok": db_ok,
        }

    # ---- worktrees plane --------------------------------------------------------

    @_measured_resource("beadhive://worktrees")
    def worktrees_resource():
        """Resource: per-worktree classification status for all managed rigs.

        Returns the same ``WtStatus`` list that ``ws worktree status --json`` emits,
        via the Typer-free ``worktree.status_rows()`` core — SAFE / ACTIVE / DIRTY /
        REVIEW / UNMERGED / LANDED_REBASED / DETACHED / MERGED_ORPHAN / ABANDONED.
        Hub-scoped (all managed rigs); zero mutation, read-only.
        """
        return [s.as_dict() for s in worktree.status_rows()]

    # ---- work plane -------------------------------------------------------------

    @_measured_resource("beadhive://work/ready")
    def work_ready_resource():
        """Resource: ready (unblocked, dependency-ordered) beads for the current rig.

        Returns the same JSON as `ws work ready --json` via bd.json(["ready"], cwd) — the
        coordinator's most re-read dashboard. Resolves the rig cwd via plan._rig_dir so it
        targets the same directory the work.ready verb does. Returns an empty list when bd
        reports no ready beads or exits non-zero.
        """
        cfg = config.load()
        cwd = plan._rig_dir(cfg, rig="")
        return bd.json(["ready"], cwd) or []

    @_measured_resource("beadhive://work/intake")
    def work_intake_resource():
        """Resource: untriaged intake inbox payload (same as `ws work intake --json`).

        Returns {rows, dupes}: rows are the open untriaged intake beads; dupes are the
        likely-duplicate pairs (mechanical dedup via `bd find-duplicates`). Changes as
        reports arrive — high pull, high signal.
        """
        cwd = plan._rig_dir(config.load(), "")
        return triage.intake_payload(cwd)

    @_measured_resource("beadhive://work/intake/dupes")
    def work_intake_dupes_resource():
        """Resource: duplicate-pair candidates scoped to the current rig's intake queue.

        Returns the subset of mechanical-dedup pairs (via triage.find_dupes /
        triage.dupes_touching) where at least one side is an open intake bead — the
        same data the beadhive://work/intake 'dupes' field carries, exposed separately so
        clients can poll it cheaply without re-fetching the full intake rows. Returns
        an empty list when bd reports no pairs or exits non-zero.
        """
        cfg = config.load()
        cwd = plan._rig_dir(cfg, rig="")
        pairs = triage.find_dupes(cwd)
        rows = triage.list_intake(cwd)
        ids = [r.get("id") for r in rows]
        return triage.dupes_touching(pairs, ids)

    @_measured_resource("beadhive://work/issue/{id}")
    def work_issue_resource(id: str):
        """Resource: single-bead lookup by id (template resource).

        Returns the normalized bead dict via work._show — resolves bd's object-or-1-list
        shape. Returns None when the bead is not found. Resolves cwd via plan._rig_dir
        so it targets the same rig as the sibling work resources.
        """
        cwd = plan._rig_dir(config.load(), rig="")
        return work._show(id, cwd)

    @_measured_resource("beadhive://work/show/{id}")
    def work_show_resource(id: str):
        """Resource: bead branch local history payload (template resource).

        Returns the same ``{base, max_commits, commits}`` payload as ``ws work show --json``
        via ``work_show.show_payload`` — the base commit SHA (7-char abbreviated), the
        configured commit limit, and the flagged commit rows for ``base..branch`` of the
        named bead.  Resolves the rig via ``worktree.locate`` (rig="" → cwd default).
        Returns an empty commits list when the branch or integration base cannot be resolved.
        """
        cfg = config.load()
        entry, _main, _target, branch = worktree.locate(cfg, "", id)
        return work_show.show_payload(cfg, entry, id, branch)

    @_measured_resource("beadhive://work/schedule/{epic}")
    def work_schedule_resource(epic: str):
        """Resource: cost-model dispatch plan for a molecule (template resource).

        Returns the same ``{groups, singletons, coordinators, max_depth}`` payload as
        ``ws work schedule --json`` via ``work.schedule_payload`` — groups are enriched
        with ``model`` (max tier across members) and coordinators carry their
        ``dispatch`` string.  Resolves the rig via ``worktree.locate`` (rig="" → cwd
        default).  Raises a ``ResourceError`` when the epic is not found in this rig.
        """
        cfg = config.load()
        entry, main, _target, _branch = worktree.locate(cfg, "", epic)
        try:
            return work.schedule_payload(epic, cfg, entry, main)
        except ValueError as exc:
            raise ResourceError(str(exc)) from exc

    # ---- plans plane ---------------------------------------------------------

    @_measured_resource("beadhive://plans")
    def plans_resource():
        """Resource: swarm list for the current rig (planning-plane molecule list).

        Returns the same JSON as `bd swarm list --json` via bd.json(["swarm", "list"], cwd)
        — the coordinator's molecule dashboard. Resolves cwd via plan._rig_dir so it
        targets the same rig directory the plan verbs use. Returns None when bd exits
        non-zero or the output is not valid JSON.
        """
        cwd = plan._rig_dir(config.load(), rig="")
        return bd.json(["swarm", "list"], cwd)

    @_measured_resource("beadhive://plan/{ref}")
    def plan_resource(ref: str):
        """Resource: single molecule status by swarm ref (template resource).

        Returns the same JSON as `bd swarm status <ref> --json` via
        bd.json(["swarm", "status", ref], cwd). Resolves cwd via plan._rig_dir so it
        targets the same rig directory the plan verbs use. Returns None when the swarm
        ref is not found or bd exits non-zero.
        """
        cwd = plan._rig_dir(config.load(), rig="")
        return bd.json(["swarm", "status", ref], cwd)

    # ---- hq plane ---------------------------------------------------------------

    @_measured_resource("beadhive://hq/intake")
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
