"""Deliver fnox-resolved credentials to a SERVICE-MANAGED shared Dolt server (bh-gaytj).

bh-pphik injects ``dolt.server_credentials`` only when bh itself runs ``bd dolt start``. A
server owned by systemd (the factory) or launchd (the Mac) is never started by bh, so its
start-up environment must be supplied by the service manager. This module is the bh half of
that contract:

* :func:`render` resolves the configured profile exactly like :func:`dolt_remote_credentials.
  server_env` and writes it to a 0600 file the unit consumes (``EnvironmentFile=`` for systemd,
  a sourced ``export`` file for a launchd wrapper). Run from ``ExecStartPre=`` it re-resolves on
  every (re)start, so a restart always picks up current credentials.
* :func:`inspect` reports, value-free, whether the RUNNING server's own environment carries the
  variables its profile needs (Linux ``/proc/<pid>/environ``; other hosts report ``unknown``).

Nothing here prints, logs or returns a secret value; only variable NAMES cross the boundary.
Operator entry: ``python -m beadhive.dolt_server_credentials render|status`` (not a ``bh`` verb,
so it stays out of the public command catalog).
Everything is opt-in: with ``dolt.server_credentials`` unset, both functions report
``not-configured`` and write nothing. Editing the unit / plist is operator-applied (docs/
TWO-STORES.md names the steps); this module never touches ``/etc`` or a service.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer

from . import dolt_health, dolt_remote_credentials

FORMATS = ("systemd", "shell")

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FIELD_VARS = (
    ("access_key_id", "AWS_ACCESS_KEY_ID", True),
    ("secret_access_key", "AWS_SECRET_ACCESS_KEY", True),
    ("session_token", "AWS_SESSION_TOKEN", False),
)


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    detail: str
    names: tuple[str, ...] = ()
    path: str = ""


def _label(cfg) -> str:
    return str(((cfg or {}).get("dolt") or {}).get("server_credentials") or "")


def _quote(value: str, fmt: str) -> str:
    if fmt == "shell":
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _body(env: dict[str, str], fmt: str) -> str:
    prefix = "export " if fmt == "shell" else ""
    return "".join(f"{prefix}{k}={_quote(v, fmt)}\n" for k, v in sorted(env.items()))


def _write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, mode 0600 from creation (no wider window)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def render(cfg, path: Path, *, fmt: str = "systemd", environ=None, home=None, broker=None):
    """Resolve ``dolt.server_credentials`` and write it to ``path`` (0600). Value-free result."""
    if fmt not in FORMATS:
        return RenderResult(False, f"unknown format '{fmt}' (choose {', '.join(FORMATS)})")
    label = _label(cfg)
    if not label:
        return RenderResult(False, "dolt.server_credentials is not set; nothing to deliver")
    creds = dolt_remote_credentials.server_env(cfg, environ=environ, home=home, broker=broker)
    if not creds.ok:
        return RenderResult(False, creds.missing)
    if not creds.env:
        return RenderResult(
            True,
            f"profile '{label}' ({creds.source}) needs no environment injection; nothing written",
        )
    if not all(_ENV_NAME.fullmatch(name) for name in creds.env):
        return RenderResult(False, "refusing to write an unsafe variable name")
    _write_private(path, _body(dict(creds.env), fmt))
    return RenderResult(
        True,
        f"wrote {len(creds.env)} variable(s) from '{label}'",
        tuple(sorted(creds.env)),
        str(path),
    )


def expected_names(cfg) -> tuple[str, ...] | None:
    """Variable NAMES the profile makes the server need, without resolving any secret.

    ``None`` = no label configured. ``()`` = the source needs no injected variables."""
    label = _label(cfg)
    if not label:
        return None
    profile = (((cfg or {}).get("dolt") or {}).get("remote_credentials") or {}).get(label)
    if not profile:
        return ()
    source = str(profile.get("source") or "ambient")
    extra = tuple(sorted(dolt_remote_credentials.checksum_env(cfg, label)))
    if source == "aws-profile":
        return ("AWS_PROFILE", *extra)
    if source != "fnox":
        return extra
    return (
        *(
            var
            for name, var, required in _FIELD_VARS
            if required or (profile.get(name) or {}).get("key")
        ),
        *extra,
    )


@dataclass(frozen=True)
class ServerStatus:
    pid: int
    state: str  # has-credentials | missing | unknown
    missing: tuple[str, ...] = ()
    stale: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "pid": self.pid,
            "state": self.state,
            "missing": list(self.missing),
            "stale": self.stale,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Inspection:
    configured: bool
    label: str = ""
    expected: tuple[str, ...] = ()
    servers: list[ServerStatus] = field(default_factory=list)
    detail: str = ""
    s3_checksums: bool = False

    def as_dict(self) -> dict:
        return {
            "s3_checksums": self.s3_checksums,
            "configured": self.configured,
            "label": self.label,
            "expected": list(self.expected),
            "servers": [s.as_dict() for s in self.servers],
            "detail": self.detail,
        }


def read_proc_environ(pid: int, proc_root: Path = Path("/proc")) -> dict[str, str] | None:
    """The running process's environment, or None when unreadable. Callers must not print it."""
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    out = {}
    for item in raw.split(b"\0"):
        name, sep, value = item.partition(b"=")
        if sep:
            out[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _parse_env_file(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.removeprefix("export ").partition("=")
        if not sep:
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("'\"'\"'", "'")
        out[name] = value
    return out


def inspect(cfg, *, servers=None, environ_reader=None, env_file: Path | None = None):
    """Does each RUNNING shared dolt server hold the credentials its profile needs?

    A running server keeps its start-up environment, so ``missing`` means a restart is required.
    With ``env_file`` (the rendered file), ``stale`` flags a server whose values differ from the
    file (rotated since start) — compared in memory, never displayed."""
    environ_reader = environ_reader or read_proc_environ
    label = _label(cfg)
    names = expected_names(cfg)
    if names is None:
        return Inspection(False, detail="dolt.server_credentials not set")
    if not names:
        return Inspection(True, label, (), detail="profile needs no injected environment")
    relaxed = bool(dolt_remote_credentials.checksum_env(cfg, label))
    running = dolt_health.running_servers() if servers is None else servers
    running = [s for s in running if s.role != "cache"]
    if not running:
        return Inspection(True, label, names, detail="no shared dolt sql-server is running")
    wanted = _parse_env_file(env_file) if env_file and env_file.is_file() else {}
    rows = []
    for server in running:
        env = environ_reader(server.pid)
        if env is None:
            rows.append(
                ServerStatus(
                    server.pid, "unknown", detail="environment unreadable (other user, or no /proc)"
                )
            )
            continue
        missing = tuple(n for n in names if not env.get(n))
        stale = any(env.get(n) != v for n, v in wanted.items() if n in names and env.get(n))
        state = "missing" if missing else "has-credentials"
        rows.append(ServerStatus(server.pid, state, missing, stale))
    return Inspection(True, label, names, rows, s3_checksums=relaxed)


def render_inspection(result: Inspection) -> list[str]:
    """Human lines for doctor/status; names only, with the restart requirement spelled out."""
    if not result.configured:
        return []
    note = (
        [f"s3_checksums: when_required applies to '{result.label}' (multipart Garage reads)"]
        if result.s3_checksums
        else []
    )
    if not result.servers:
        return [f"server credentials ('{result.label}'): {result.detail}", *note]
    lines = list(note)
    for s in result.servers:
        if s.state == "has-credentials":
            note = "  (rotated since start: restart to pick it up)" if s.stale else ""
            mark = "⚠" if s.stale else "✓"
            lines.append(f"{mark} pid {s.pid}: has {', '.join(result.expected)}{note}")
        elif s.state == "missing":
            lines.append(
                f"✗ pid {s.pid}: missing {', '.join(s.missing)} — a running server keeps its "
                "start-up environment; restart the service after applying the unit change"
            )
        else:
            lines.append(f"? pid {s.pid}: {s.detail}")
    return lines


def _load_config():
    from . import config

    return config.load()


app = typer.Typer(
    no_args_is_help=True,
    help="credentials for a systemd/launchd-managed shared Dolt server (opt-in; value-free).",
)


@app.command("render")
def render_cmd(
    path: Annotated[Path, typer.Option("--path", help="env file to write (mode 0600)")],
    fmt: Annotated[
        str, typer.Option("--format", help="systemd (EnvironmentFile) or shell")
    ] = "systemd",
):
    """Resolve dolt.server_credentials from its source and write PATH for the service manager."""
    result = render(_load_config(), path, fmt=fmt)
    typer.echo(result.detail, err=not result.ok)
    if result.names:
        typer.echo(f"variables: {', '.join(result.names)}")
    if not result.ok:
        raise typer.Exit(1)


@app.command("status")
def status_cmd(
    env_file: Annotated[Path | None, typer.Option("--env-file", help="file to compare")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
):
    """Report whether the running shared server has its credentials (names only)."""
    import json

    result = inspect(_load_config(), env_file=env_file)
    if as_json:
        typer.echo(json.dumps(result.as_dict(), indent=2))
    else:
        for line in render_inspection(result) or [result.detail]:
            typer.echo(line)
    if any(s.state == "missing" for s in result.servers):
        raise typer.Exit(1)


def main() -> None:
    """Module entry: kept out of the ``bh`` command catalog so a unit's ``ExecStartPre=`` can call
    it without growing the public CLI contract."""
    app()


if __name__ == "__main__":
    main()
