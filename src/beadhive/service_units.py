"""Regenerate OS service units without dropping what the operator set (bh-atdi0).

The heartbeat sender (:mod:`beadhive.heartbeat_sender`) and the host daemon
(:mod:`beadhive.daemon_platform`) write launchd LaunchAgents and ``systemd --user`` units from
the installing environment. Under launchd a job does not inherit the login shell, so a unit
must carry ``HOME`` (and ``BH_HOME``) and a ``WorkingDirectory`` itself, or it resolves the
wrong bh config and credential store.

Regenerating over an existing unit keeps the operator's values:

* every existing environment variable and ``WorkingDirectory`` wins over the generated one
  (a value bh no longer generates is kept as well);
* every other top-level LaunchAgent key bh does not generate is kept;
* a ``systemd`` directive bh does not generate cannot be merged faithfully, so the regeneration
  refuses with a unified diff of what it would drop (:class:`UnitConflict`), unless the caller
  explicitly replaces the unit;
* an unreadable plist holds no value launchd could load, so it is regenerated.

Only bh-authored text appears in a conflict message; the diff shows the unit file's own lines,
which the operator wrote and can already read.
"""

from __future__ import annotations

import difflib
import os
import plistlib
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: The variables a generated unit carries when the installing environment has them.
INSTALL_ENVIRONMENT = ("HOME", "BH_HOME", "PATH", "GIT_WORKSPACE")

_DIRECTIVE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=(.*)$")


class UnitConflict(ValueError):
    """Regenerating would silently drop operator-set unit content."""

    def __init__(self, name: str, diff: str):
        super().__init__(
            f"{name}: regenerating would drop operator-set content; keep it by hand or replace "
            f"the unit explicitly\n{diff}"
        )
        self.name = name
        self.diff = diff


@dataclass
class Merge:
    """One regenerated unit and the operator-set values it kept."""

    content: bytes
    kept: list[str] = field(default_factory=list)


def safe_value(name: str, value: str) -> str:
    if "\n" in value or "\0" in value:
        raise ValueError(f"{name} contains an unsafe service value")
    return value


def install_home() -> str:
    """The installing account's ``HOME`` (the environment first, then the passwd entry)."""
    return os.environ.get("HOME") or str(Path.home())


def working_directory(env: dict[str, str]) -> str | None:
    """The default ``WorkingDirectory``: the unit's own ``HOME``, when it carries one."""
    return env.get("HOME") or None


def systemd_environment(env: dict[str, str]) -> str:
    """``Environment="K=V"`` lines, quoted for systemd."""
    lines = []
    for name, value in env.items():
        escaped = safe_value(name, value).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'Environment="{name}={escaped}"\n')
    return "".join(lines)


def systemd_working_directory(directory: str | None) -> str:
    return "" if directory is None else f"WorkingDirectory={safe_value('cwd', directory)}\n"


def _systemd_settings(text: str) -> tuple[dict[str, str], str | None, list[tuple[str, str]]]:
    """``(environment, working directory, every directive)`` of an existing unit file."""
    env: dict[str, str] = {}
    directory = None
    directives = []
    for line in text.splitlines():
        match = _DIRECTIVE.match(line)
        if match is None or line.lstrip().startswith(("#", ";")):
            continue
        key, value = match.group(1), match.group(2).strip()
        directives.append((key, line.strip()))
        if key == "Environment":
            try:
                words = shlex.split(value)
            except ValueError:
                words = []
            for word in words:
                name, sep, setting = word.partition("=")
                if sep and name:
                    env[name] = setting
        elif key == "WorkingDirectory":
            directory = value
    return env, directory, directives


def _kept(generated: dict[str, str], existing: dict[str, str]) -> list[str]:
    return [name for name, value in existing.items() if generated.get(name) != value]


def merge_systemd(
    name: str,
    existing: bytes | None,
    render: Callable[[dict[str, str], str | None], bytes],
    env: dict[str, str],
    directory: str | None,
    *,
    replace: bool = False,
) -> Merge:
    """Regenerate one ``systemd`` unit over ``existing`` (see the module doc)."""
    if existing is None or replace:
        return Merge(render(env, directory))
    text = existing.decode(errors="replace")
    old_env, old_directory, old_directives = _systemd_settings(text)
    merged_env = {**env, **old_env}
    kept = _kept(env, old_env)
    merged_directory = directory
    if old_directory is not None:
        if old_directory != directory:
            kept.append("WorkingDirectory")
        merged_directory = old_directory
    content = render(merged_env, merged_directory)
    _, _, new_directives = _systemd_settings(content.decode())
    generated = {key for key, _ in new_directives}
    dropped = [line for key, line in old_directives if key not in generated]
    if dropped:
        diff = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                content.decode().splitlines(keepends=True),
                fromfile=f"{name} (installed)",
                tofile=f"{name} (regenerated)",
            )
        )
        raise UnitConflict(name, diff)
    return Merge(content, kept)


def merge_launchd(
    name: str,
    existing: bytes | None,
    render: Callable[[dict[str, str], str | None], dict],
    env: dict[str, str],
    directory: str | None,
    *,
    replace: bool = False,
) -> Merge:
    """Regenerate one LaunchAgent plist over ``existing`` (see the module doc)."""

    def dump(payload: dict) -> bytes:
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)

    if existing is None or replace:
        return Merge(dump(render(env, directory)))
    try:
        old = plistlib.loads(existing)
    except Exception:
        old = None
    if not isinstance(old, dict):
        # launchd cannot load an unreadable plist either: there is no operator value to keep.
        return Merge(dump(render(env, directory)))
    old_env = old.get("EnvironmentVariables")
    old_env = {str(k): str(v) for k, v in old_env.items()} if isinstance(old_env, dict) else {}
    kept = _kept(env, old_env)
    merged_directory = directory
    old_directory = old.get("WorkingDirectory")
    if isinstance(old_directory, str):
        if old_directory != directory:
            kept.append("WorkingDirectory")
        merged_directory = old_directory
    payload = render({**env, **old_env}, merged_directory)
    for key, value in old.items():
        if key not in payload:
            payload[key] = value
            kept.append(key)
    return Merge(dump(payload), kept)
