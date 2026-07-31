#!/usr/bin/env python3
"""Resolved-state probe for bh-8i03 (cross-platform config portability spike).

Spike artifact for bh-8i03.1 — NOT product code, does not live under src/beadhive/.

Dumps one host's RESOLVED Claude Code harness + bh state as one stable-ordered JSON
document on stdout, so two hosts (or two runs on the same host) can be compared on
*outcomes* rather than file contents. Diagnostics/log noise goes to stderr; stdout is
reserved for the comparable JSON only (no timestamps, PIDs, or other run-to-run noise
are included in it — see "Design notes" in the accompanying spike doc for why that
matters).

Usage:
    python3 bh-8i03.1-resolved-state-probe.py > host-a.json

Requirements: python3 (3.8+), stdlib only. No third-party packages. Designed to run
unmodified on macOS and Linux — see the "Portability" section of the companion doc
(docs/spikes/bh-8i03.1-resolved-state-probe.md) for what was and wasn't verified.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[probe] {msg}", file=sys.stderr)


def run(cmd: list[str], timeout: int = 15) -> tuple[int | None, str, str]:
    """Run a subprocess, never raising. Returns (returncode|None, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return None, "", "not-found"
    except subprocess.TimeoutExpired:
        return None, "", "timeout"
    except OSError as exc:  # pragma: no cover - defensive
        return None, "", f"os-error: {exc}"


# ---------------------------------------------------------------------------
# Secret redaction — best-effort, NOT a guarantee. See the doc's "Cannot resolve /
# limitations" section: this exists because `claude mcp list` and env vars can carry
# live credentials (observed empirically while building this probe), and the probe's
# own output must be safe to paste into a report or commit as a worked example.
# ---------------------------------------------------------------------------

_SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|_AUTH$|AUTH_)", re.IGNORECASE)
# Common benign env vars that would otherwise false-positive on the pattern above
# (PWD/OLDPWD are plain directory paths, not secrets — redacting them destroys
# useful host-comparison signal for no security benefit).
_SECRET_NAME_ALLOWLIST = {"PWD", "OLDPWD"}
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}"
    r"|ctx7sk-[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"  # JWT-shaped
    r")"
)
REDACTED = "<redacted>"


def redact_value(value: str) -> str:
    if not value:
        return value
    return _SECRET_VALUE_RE.sub(REDACTED, value)


def redact_env_value(name: str, value: str) -> str:
    if name not in _SECRET_NAME_ALLOWLIST and _SECRET_NAME_RE.search(name):
        return f"<redacted:{len(value)} chars>"
    return redact_value(value)


# ---------------------------------------------------------------------------
# Binary resolution — the one thing every section below funnels through so that
# "resolved absolute path of every binary referenced anywhere in config" (bh-8i03.1
# acceptance) is answered the same way no matter which config surface it came from.
# ---------------------------------------------------------------------------


def resolve_binary(token: str | None) -> dict:
    if not token:
        return {"token": token, "resolved_path": None, "found": False, "method": "empty"}
    if token.startswith("/"):
        exists = os.path.exists(token)
        executable = exists and os.access(token, os.X_OK)
        return {
            "token": token,
            "resolved_path": token if exists else None,
            "found": bool(executable),
            "method": "absolute-path",
        }
    which = shutil.which(token)
    return {
        "token": token,
        "resolved_path": which,
        "found": which is not None,
        "method": "PATH-which",
    }


_SHELL_META_RE = re.compile(r"(;|&&|\|\||\$\(|`)")


def first_command_token(
    command: str | None, plugin_root: str | None = None
) -> tuple[str, str | None]:
    """Heuristic split of a hook `command` string into (kind, executable-token).

    kind is one of:
      - "empty"             — no command
      - "shell-conditional"  — a multi-statement/conditional one-liner; not resolved
                               to a single executable (see doc limitations)
      - "simple"             — a single invocation; token is the program to resolve
    """
    if not command:
        return ("empty", None)
    cmd = command.strip()
    if plugin_root:
        cmd = cmd.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)
    if re.match(r"^\s*(if|for|while)\b", cmd) or _SHELL_META_RE.search(cmd):
        return ("shell-conditional", cmd)
    m = re.match(r'^"([^"]+)"', cmd) or re.match(r"^'([^']+)'", cmd)
    if m:
        return ("simple", m.group(1))
    parts = cmd.split()
    return ("simple", parts[0] if parts else None)


class BinaryRegistry:
    """Dedupe + track every (token -> resolution) this probe encountered, plus which
    config surface(s) referenced each one, so the aggregate "binaries" section of the
    output answers the acceptance criterion once instead of scattering it."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    def add(self, token: str | None, referenced_by: str) -> None:
        if not token:
            return
        entry = self._entries.setdefault(token, {**resolve_binary(token), "referenced_by": set()})
        entry["referenced_by"].add(referenced_by)

    def as_list(self) -> list[dict]:
        out = []
        for entry in self._entries.values():
            out.append({**entry, "referenced_by": sorted(entry["referenced_by"])})
        return sorted(out, key=lambda e: e["token"])


# ---------------------------------------------------------------------------
# Claude Code harness introspection
# ---------------------------------------------------------------------------

_DOCTOR_FIELD_RE = re.compile(
    r"^(Running|Commit|Platform|Path|Config install method|Search|Auto-updates|"
    r"Auto-update channel|Last update attempt):\s*(.*)$"
)


def get_claude_doctor() -> dict:
    rc, out, err = run(["claude", "doctor"])
    if rc is None:
        return {"available": False, "reason": err}
    fields: dict[str, str] = {}
    for line in out.splitlines():
        m = _DOCTOR_FIELD_RE.match(line.strip())
        if m:
            key = m.group(1).lower().replace(" ", "_").replace("-", "_")
            fields[key] = m.group(2).strip()
    return {"available": rc == 0, "returncode": rc, "fields": fields}


def get_claude_version() -> dict:
    rc, out, err = run(["claude", "--version"])
    if rc is None:
        return {"available": False, "reason": err}
    return {"available": rc == 0, "raw": out.strip()}


_PLUGIN_DETAIL_LIST_RE = re.compile(
    r"^\s*(Skills|Agents|Hooks|MCP servers|LSP servers)\s*\((\d+)\)\s*(.*)$"
)


def get_plugin_details(plugin_id: str, install_path: str | None) -> dict:
    """Parse `claude plugin details <id>` — the per-plugin resolved component
    inventory (skills/agents/hooks/mcp/lsp COUNTED AND NAMED, not just declared in a
    manifest we'd have to open ourselves)."""
    rc, out, err = run(["claude", "plugin", "details", plugin_id])
    if rc is None or rc != 0:
        return {"available": False, "reason": err or f"exit {rc}"}
    skills: list[str] = []
    agents: list[str] = []
    hook_events: list[str] = []
    mcp_count = 0
    lsp_count = 0
    for line in out.splitlines():
        m = _PLUGIN_DETAIL_LIST_RE.match(line)
        if not m:
            continue
        kind, _count, names = m.groups()
        items = [n.strip() for n in names.split(",") if n.strip()]
        if kind == "Skills":
            skills = items
        elif kind == "Agents":
            agents = items
        elif kind == "Hooks":
            # trailing annotation like "(harness-only — no model context cost)" can
            # ride along with the last item; strip parenthetical suffixes.
            hook_events = [re.sub(r"\s*\(.*$", "", n) for n in items]
        elif kind == "MCP servers":
            mcp_count = len(items) if items and items != [""] else int(_count)
        elif kind == "LSP servers":
            lsp_count = len(items) if items and items != [""] else int(_count)

    hooks_json_path = None
    hooks_detail: list[dict] = []
    if install_path:
        candidate = Path(install_path) / "hooks" / "hooks.json"
        if candidate.is_file():
            hooks_json_path = str(candidate)
            try:
                manifest = json.loads(candidate.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                hooks_detail = [{"error": f"unreadable hooks.json: {exc}"}]
            else:
                hooks_detail = flatten_hooks(
                    manifest.get("hooks", {}),
                    source=f"plugin:{plugin_id}",
                    plugin_root=install_path,
                )

    return {
        "available": True,
        "skills": sorted(skills),
        "agents": sorted(agents),
        "hook_events_declared": sorted(hook_events),
        "hooks_json_path": hooks_json_path,
        "hooks": hooks_detail,
        "mcp_server_count": mcp_count,
        "lsp_server_count": lsp_count,
    }


def get_claude_plugins(binaries: BinaryRegistry) -> dict:
    rc, out, err = run(["claude", "plugin", "list", "--json"])
    if rc is None or rc != 0:
        return {"available": False, "reason": err or f"exit {rc}"}
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"available": False, "reason": f"unparseable JSON: {exc}"}

    plugins = []
    for p in raw:
        entry = {
            "id": p.get("id"),
            "version": p.get("version"),
            "scope": p.get("scope"),
            "enabled": p.get("enabled"),
            "install_path": p.get("installPath"),
        }
        if entry["enabled"]:
            # Only expand components for ENABLED plugins — that is precisely the
            # "resolved, not declared" distinction the bead asks for: a disabled
            # plugin's skills/agents are not actually available in a session.
            details = get_plugin_details(entry["id"], entry["install_path"])
            entry["resolved"] = details
            for h in details.get("hooks", []):
                binary_token = h.get("binary_token")
                if binary_token:
                    binaries.add(binary_token, f"plugin-hook:{entry['id']}:{h['event']}")
        else:
            entry["resolved"] = {"available": False, "reason": "plugin disabled"}
        plugins.append(entry)

    plugins.sort(key=lambda e: e["id"] or "")
    return {"available": True, "plugins": plugins}


_MCP_LINE_RE = re.compile(r"^(?P<command>.*) - (?P<icon>[✔✘⏸])\s*(?P<status_rest>.*)$")
_MCP_ICON_CONNECTED = {"✔": True, "✘": False, "⏸": None}


def get_mcp_servers(binaries: BinaryRegistry) -> dict:
    rc, out, err = run(["claude", "mcp", "list"])
    if rc is None:
        return {"available": False, "reason": err}
    servers = []
    for line in out.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, rest = line.partition(": ")
        m = _MCP_LINE_RE.match(rest)
        if not m:
            continue
        command = m.group("command").strip()
        icon = m.group("icon")
        status_rest = m.group("status_rest").strip()
        error = None
        status_text = status_rest
        if " — " in status_rest:
            status_text, _, error = status_rest.partition(" — ")
        kind, token = first_command_token(command)
        if kind == "simple":
            binaries.add(token, f"mcp:{name}")
        servers.append(
            {
                "name": name,
                "declared_command": redact_value(command),
                "connected": _MCP_ICON_CONNECTED.get(icon),
                "status_text": status_text.strip(),
                "error": redact_value(error) if error else None,
                "binary": resolve_binary(token)
                if kind == "simple"
                else {
                    "token": token,
                    "resolved_path": None,
                    "found": False,
                    "method": kind,
                },
            }
        )
    servers.sort(key=lambda e: e["name"])
    return {"available": rc == 0, "raw_available": True, "servers": servers}


# ---------------------------------------------------------------------------
# Hooks — settings.json layers (project / project-local / user / user-local) plus
# each enabled plugin's own hooks.json (handled above, inline with plugin details).
# ---------------------------------------------------------------------------


def flatten_hooks(hooks_obj: dict, source: str, plugin_root: str | None = None) -> list[dict]:
    out = []
    for event, matcher_groups in sorted(hooks_obj.items()):
        if not isinstance(matcher_groups, list):
            continue
        for group_idx, group in enumerate(matcher_groups):
            matcher = group.get("matcher") if isinstance(group, dict) else None
            hook_list = group.get("hooks", []) if isinstance(group, dict) else []
            for hook_idx, hook in enumerate(hook_list):
                command = hook.get("command") if isinstance(hook, dict) else None
                kind, token = first_command_token(command, plugin_root=plugin_root)
                out.append(
                    {
                        "source": source,
                        "event": event,
                        "matcher": matcher,
                        "index": f"{group_idx}.{hook_idx}",
                        "command": redact_value(
                            command.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)
                            if command and plugin_root
                            else command
                        )
                        if command
                        else None,
                        "command_kind": kind,
                        "binary_token": token if kind == "simple" else None,
                        "binary": resolve_binary(token) if kind == "simple" else None,
                        "timeout": hook.get("timeout") if isinstance(hook, dict) else None,
                    }
                )
    return out


def settings_layers(repo_root: Path | None) -> list[tuple[str, Path | None]]:
    layers: list[tuple[str, Path | None]] = [
        ("user_settings", Path.home() / ".claude" / "settings.json"),
        ("user_settings_local", Path.home() / ".claude" / "settings.local.json"),
        # `None` (rather than omitting the key) when not run inside a git working
        # tree — keeps the output SCHEMA stable across invocations even though the
        # value legitimately differs; see get_settings_hooks.
        ("project_settings", (repo_root / ".claude" / "settings.json") if repo_root else None),
        (
            "project_settings_local",
            (repo_root / ".claude" / "settings.local.json") if repo_root else None,
        ),
    ]
    # Documented (per Anthropic's published settings-precedence docs) enterprise/
    # managed layer. NOT verified via any CLI introspection surface on this host —
    # `claude` has no `config`/`policy` subcommand to ask for it (see doc "Cannot
    # resolve" list) — so this is a best-effort conventional path, existence-checked
    # only, never asserted as authoritative.
    if platform.system() == "Darwin":
        layers.append(
            (
                "managed_settings_convention",
                Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
            )
        )
    else:
        layers.append(
            ("managed_settings_convention", Path("/etc/claude-code/managed-settings.json"))
        )
    return layers


def get_settings_hooks(repo_root: Path | None, binaries: BinaryRegistry) -> dict:
    result = {}
    for name, path in settings_layers(repo_root):
        if path is None:
            result[name] = {
                "path": None,
                "present": False,
                "reason": "not inside a git working tree",
            }
            continue
        if not path.is_file():
            result[name] = {"path": str(path), "present": False}
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            result[name] = {"path": str(path), "present": True, "error": str(exc)}
            continue
        hooks = flatten_hooks(data.get("hooks", {}), source=name)
        for h in hooks:
            if h.get("binary_token"):
                binaries.add(h["binary_token"], f"hook:{name}:{h['event']}")
        result[name] = {"path": str(path), "present": True, "hooks": hooks}
    return result


# ---------------------------------------------------------------------------
# bh's own resolved state
# ---------------------------------------------------------------------------

_BH_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]*?):\s*(.+?)\s*$")


def get_bh_state() -> dict:
    rc, out, err = run(["bh", "--version"])
    if rc is None:
        return {"available": False, "reason": err}
    version = out.strip()

    rc2, config_show, err2 = run(["bh", "config", "show"])
    if rc2 is None:
        return {"available": True, "version": version, "config_show_error": err2}

    section = None
    workspace_root = None
    worktree_shadow_root = None
    worktrees_ephemeral = None
    for raw_line in config_show.splitlines():
        line = raw_line.rstrip()
        if line.startswith("# "):
            section = line[2:].split(" (")[0].strip()
            continue
        m = _BH_LINE_RE.match(line)
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if section == "Config" and key == "workspace root":
            workspace_root = value
        elif section == "Worktrees":
            if key == "root":
                worktree_shadow_root = value.split("  (")[0].strip()
            elif key == "ephemeral":
                worktrees_ephemeral = value.lower() == "true"

    rc3, hive_out, _err3 = run(["bh", "hive", "list"])
    hives = []
    if rc3 == 0:
        for line in hive_out.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            hives.append(line)
    hives.sort()

    return {
        "available": True,
        "version": version,
        "config_path": run(["bh", "config", "path"])[1].strip() or None,
        "workspace_root": workspace_root,
        "worktree_shadow_root": worktree_shadow_root,
        "worktrees_ephemeral": worktrees_ephemeral,
        "registered_hives": hives,
    }


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

# Discovered empirically while verifying this probe's own zero-self-delta property:
# W3C trace-context propagation stamps a fresh per-PROCESS span id into TRACEPARENT
# (the trace id stays constant for the shell session; the span id does not), which
# manufactured a real self-delta between two back-to-back runs. This is regenerated
# per invocation, not resolved host/harness CONFIG, so it is excluded from the
# comparable "values" map — but its exclusion is recorded, not silent, per this
# bead's own design constraint.
_VOLATILE_ENV_NAMES = {"TRACEPARENT", "TRACESTATE"}


def get_env() -> dict:
    values = {}
    excluded = []
    for k, v in sorted(os.environ.items()):
        if k in _VOLATILE_ENV_NAMES:
            excluded.append(k)
            continue
        values[k] = redact_env_value(k, v)
    return {"values": values, "excluded_volatile_names": sorted(excluded)}


# ---------------------------------------------------------------------------
# Filesystem case-sensitivity of every path root the probe otherwise touches
# ---------------------------------------------------------------------------


def check_case_sensitivity(root: Path) -> dict:
    if not root.exists():
        return {
            "path": str(root),
            "testable": False,
            "case_sensitive": None,
            "error": "root does not exist",
        }
    marker = f"CaseProbe-{uuid.uuid4().hex}"
    probe_path = root / marker
    try:
        probe_path.write_text("bh-8i03.1 case-sensitivity probe\n")
    except OSError as exc:
        return {"path": str(root), "testable": False, "case_sensitive": None, "error": str(exc)}
    try:
        alt_path = root / marker.lower()
        case_sensitive = not alt_path.exists() if marker.lower() != marker else None
    finally:
        try:
            probe_path.unlink()
        except OSError:
            pass
    return {"path": str(root), "testable": True, "case_sensitive": case_sensitive, "error": None}


def get_case_sensitivity(roots: dict[str, Path | None]) -> dict:
    out = {}
    for name, path in roots.items():
        if path is None:
            out[name] = {
                "path": None,
                "testable": False,
                "case_sensitive": None,
                "error": "root unresolved",
            }
            continue
        out[name] = check_case_sensitivity(path)
    return out


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


def get_host() -> dict:
    return {
        "hostname": socket.gethostname(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "python_version": platform.python_version(),
    }


# ---------------------------------------------------------------------------
# Explicit "cannot resolve" ledger — bh-8i03.1's acceptance criteria require this
# to be a first-class, explicit list rather than a silent gap. Every entry here is
# a limitation discovered *while building this probe*, not a hypothetical.
# ---------------------------------------------------------------------------

UNRESOLVABLE = [
    "Effective settings.json precedence: the `claude` CLI (v2.1.220, checked "
    "2026-07-31) has no `config`/`config list`/`config get` subcommand and no other "
    "documented way to ask it for its own MERGED settings view. This probe reads "
    "each settings.json layer (user, user-local, project, project-local, and the "
    "conventional enterprise-managed path) as SEPARATE, UNMERGED documents. It does "
    "not attempt to reimplement Claude Code's own precedence/merge rules, per this "
    "bead's design constraint, and therefore cannot report a single 'effective "
    "value' for any setting that is overridden across layers — only the raw layers.",
    "`claude mcp list` and `claude plugin details <name>` have no `--json` output "
    "in this CLI version (`claude plugin list --json` and `claude plugin marketplace "
    "list --json` do). Those two surfaces are parsed from human-formatted text "
    "(including Unicode status glyphs); a future CLI release could silently change "
    "that format and break this probe's parsing without changing anything this "
    "probe's own logic is responsible for.",
    "The enterprise/managed-settings.json path (macOS: '/Library/Application "
    "Support/ClaudeCode/managed-settings.json'; Linux: '/etc/claude-code/"
    "managed-settings.json') is a documented convention, not something this probe "
    "confirmed via introspection — there is no CLI surface that names its own "
    "managed-settings path. Neither file exists on the host this probe was built "
    "and run on, so the convention itself is unverified against a real instance.",
    "Hook commands that are multi-statement shell one-liners (e.g. the "
    "'if [ -f ... ]; then ...; fi' pattern used by several installed plugins/tools "
    "on this host) are NOT resolved to a single executable — this probe does not "
    "parse shell grammar. They are flagged 'command_kind: shell-conditional' with "
    "the raw (redacted) command preserved, rather than guessing at one binary.",
    "MCP 'connected' status is a LIVE health check at the moment the probe runs, "
    "not a static config fact — if a locally-run MCP server process/daemon changes "
    "state between two probe invocations (observed on this host: a local memory-"
    "server MCP flipped between connected/failed across unrelated sessions), the "
    "probe's output for that one field can legitimately differ run-to-run. This is "
    "real instrument variance, not a bug in the probe, and should not be treated as "
    "a false self-delta if it happens — see the accompanying doc's Evidence section "
    "for how this was distinguished from an actual reproducibility failure.",
    "macOS Keychain-backed credential storage has no filesystem-visible surface "
    "this probe can enumerate (by design — that's the whole point of a keychain). "
    "This probe does not attempt to read it, and does not check whether an "
    "equivalent exists on Linux; that comparison is explicitly out of scope for "
    "this bead (see bh-8i03.6).",
    "This probe was only RUN on macOS (host A) as part of this bead. Its Linux "
    "behavior is unverified by this bead — bh-8i03.2 performs that capture on a "
    "real Linux host (not a container, per this molecule's own method). "
    "Portability claims here rest on the implementation being Python-3-stdlib-only "
    "with no shell-out to GNU/BSD-specific coreutils flags, not on an actual run.",
    "Secret redaction (env values whose NAME looks sensitive, and known API-key/"
    "token PATTERNS inside MCP command strings) is a best-effort heuristic, not a "
    "guarantee. A secret in an unrecognized shape could still appear in this "
    "probe's output; treat any captured JSON as sensitive until reviewed, the same "
    "way you would treat `claude mcp list`'s own raw output.",
    "TRACEPARENT/TRACESTATE (W3C trace-context env vars) are deliberately excluded "
    "from `env.values` and only named in `env.excluded_volatile_names`: they carry "
    "a per-PROCESS span id that is regenerated on every invocation even when "
    "nothing about host/harness config changed, and a first draft of this probe "
    "confirmed that empirically — it produced a real one-line self-delta across "
    "two back-to-back runs before this exclusion was added. This is host/session "
    "telemetry plumbing, not resolved config, so it does not belong in a config-"
    "comparison instrument; excluding it is a probe-design decision, not something "
    "unresolvable, but it is recorded here because it is the concrete reason the "
    "zero-self-delta property required active work rather than falling out for "
    "free.",
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def find_repo_root() -> Path | None:
    rc, out, _err = run(["git", "rev-parse", "--show-toplevel"])
    if rc == 0 and out.strip():
        return Path(out.strip())
    return None


def main() -> int:
    binaries = BinaryRegistry()

    log("host + versions")
    host = get_host()
    claude_version = get_claude_version()
    bh_state = get_bh_state()

    log("claude doctor")
    doctor = get_claude_doctor()

    log("claude plugin list --json (+ per-plugin details for enabled plugins)")
    plugins = get_claude_plugins(binaries)

    log("claude mcp list (live health check)")
    mcp = get_mcp_servers(binaries)

    log("settings.json hook layers")
    repo_root = find_repo_root()
    hooks = get_settings_hooks(repo_root, binaries)

    log("filesystem case-sensitivity of touched roots")
    claude_data_dir = None
    doctor_path = doctor.get("fields", {}).get("path") if doctor.get("available") else None
    if doctor_path and "versions" in doctor_path:
        claude_data_dir = Path(doctor_path).parent.parent

    roots = {
        "home": Path.home(),
        "repo_root": repo_root,
        "bh_workspace_root": Path(bh_state["workspace_root"])
        if bh_state.get("workspace_root")
        else None,
        "bh_worktree_shadow_root": Path(bh_state["worktree_shadow_root"])
        if bh_state.get("worktree_shadow_root")
        else None,
        "claude_user_config_dir": Path.home() / ".claude",
        "claude_data_dir": claude_data_dir,
    }
    case_sensitivity = get_case_sensitivity(roots)

    log("environment")
    env = get_env()

    result = {
        "schema_version": SCHEMA_VERSION,
        "bead": "bh-8i03.1",
        "host": host,
        "harness_claude": {
            "version": claude_version,
            "doctor": doctor,
            "plugins": plugins,
            "mcp_servers": mcp,
            "hooks": hooks,
        },
        "bh": bh_state,
        "env": env,
        "binaries": binaries.as_list(),
        "case_sensitivity": case_sensitivity,
        "unresolvable": UNRESOLVABLE,
    }

    print(json.dumps(result, indent=2, sort_keys=True))
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
