#!/usr/bin/env python3
"""Operator-run globals watchdog for a Dolt sql-server (bh-ijxif; ADR F4, conditions 15-16).

On Dolt 2.3.5 any login can ``SET GLOBAL`` / ``SET PERSIST`` any variable (bh-wtsrc E4). A global
``dolt_force_transaction_commit=1`` may defeat FK epoch retirement. This tool NARROWS and DETECTS
that; it does not prevent a session-level ``SET``. It is strictly read-only against the server's
variables: it alerts loudly on drift and never "fixes" a value.

Subcommands
-----------
``check``       read each watched global with ``SELECT @@GLOBAL.<name>`` and compare to the
                expected value. Exit 0 = all match, 2 = drift, 3 = unverifiable (cannot connect
                or a variable cannot be read as a global). Drift outranks unverifiable.
``persist``     probe that ``SET PERSIST`` cannot stick: re-persist ``max_connections`` at its
                CURRENT value (a no-op if it ever succeeds). A refusal is the PASS condition
                (read-only ``DOLT_ROOT_PATH``); acceptance is an alert (exit 2).
``root-check``  static check, run as the server's user: the Dolt root's ``.dolt`` directory and
                ``config_global.json`` must not be writable (only ``eventsData`` may be).

The watched list is data, not code: ``DEFAULT_WATCHED`` is condition 16's list; ``--config FILE``
(JSON ``{"expect": {name: value}}``) replaces it, ``--expect NAME=VALUE`` adds or overrides one
and ``--drop NAME`` removes one. Nothing is hard-wired.

The server password comes from ``DOLT_CLI_PASSWORD`` (never argv). The client ``dolt`` needs its
own writable ``DOLT_ROOT_PATH``; never point it at the server's read-only root.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# ADR condition 16 as amended by M13 (bh-uhx2r E1, bh-wtsrc E4). Expected values suit a WRITABLE
# primary hive server built from ``deploy/dolt/hive-server.yaml.example`` (``max_connections:
# 100``); a deliberately read-only replica overrides ``read_only`` to 1, and a server with another
# connection cap overrides ``max_connections``, in its config. ``dolt_allow_commit_conflicts`` is
# NOT watched: it is session-only on Dolt 2.3.5 and cannot be set globally. The watchdog covers
# availability; a forced global is not an integrity breach of the fence (bh-uhx2r E1).
# ``deploy/dolt/watched-globals.json`` and ``beadhive.hive_forward.WATCHED_GLOBALS`` carry the same
# list; a unit test keeps the three equal.
DEFAULT_WATCHED: dict[str, str] = {
    "dolt_force_transaction_commit": "0",
    "dolt_transaction_commit": "0",
    "read_only": "0",
    "max_connections": "100",
}

OK, DRIFT, UNVERIFIABLE = "ok", "drift", "unverifiable"
EXIT_OK, EXIT_DRIFT, EXIT_UNVERIFIABLE = 0, 2, 3

_TRUE = {"1", "on", "true", "yes"}
_FALSE = {"0", "off", "false", "no"}

# (argv, timeout) -> (returncode, stdout, stderr); injectable so unit tests need no dolt.
Runner = Callable[[Sequence[str], float], tuple[int, str, str]]


@dataclass(frozen=True)
class Result:
    name: str
    expected: str
    actual: str | None
    status: str
    detail: str = ""


def normalize(value: object) -> str:
    """Canonical comparison form: booleans collapse to 0/1, everything else is lowercased text."""
    text = str(value).strip().lower()
    if text in _TRUE:
        return "1"
    if text in _FALSE:
        return "0"
    return text


def resolve_watched(
    *,
    config: Mapping[str, object] | None = None,
    expect: Sequence[str] = (),
    drop: Sequence[str] = (),
    use_defaults: bool = True,
) -> dict[str, str]:
    """Build the watched list: defaults (or the config file) + ``--expect`` - ``--drop``."""
    if config is not None:
        raw = config.get("expect")
        if not isinstance(raw, Mapping):
            raise ValueError('config must be a JSON object with an "expect" object')
        watched = {str(k): str(v) for k, v in raw.items()}
    else:
        watched = dict(DEFAULT_WATCHED) if use_defaults else {}
    for item in expect:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise ValueError(f"--expect wants NAME=VALUE, got {item!r}")
        watched[name.strip()] = value.strip()
    for name in drop:
        watched.pop(name, None)
    return watched


def _run(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


def _client(host: str, port: int, user: str, tls: bool) -> list[str]:
    dolt = shutil.which("dolt") or "dolt"
    argv = [dolt, "--host", host, "--port", str(port), "--user", user]
    if not tls:
        argv.append("--no-tls")
    return [*argv, "sql", "-r", "json", "-q"]


def read_global(
    client: Sequence[str], name: str, runner: Runner = _run, timeout: float = 30.0
) -> str:
    """Return ``@@GLOBAL.<name>`` as text; raise ``RuntimeError`` with the server's words."""
    if not name.replace("_", "").isalnum():
        raise RuntimeError(f"refusing to query odd variable name {name!r}")
    rc, out, err = runner([*client, f"SELECT @@GLOBAL.{name} AS v"], timeout)
    if rc != 0:
        text = (err or out).strip()
        raise RuntimeError(text.splitlines()[-1] if text else "failed")
    try:
        return str(json.loads(out)["rows"][0]["v"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unparseable reply: {out.strip()[:200]!r}") from exc


def check_globals(
    client: Sequence[str],
    watched: Mapping[str, str],
    runner: Runner = _run,
) -> list[Result]:
    results: list[Result] = []
    for name, expected in watched.items():
        try:
            actual = read_global(client, name, runner)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            results.append(Result(name, expected, None, UNVERIFIABLE, str(exc)))
            continue
        status = OK if normalize(actual) == normalize(expected) else DRIFT
        results.append(Result(name, expected, actual, status))
    return results


def exit_code(results: Sequence[Result]) -> int:
    if any(r.status == DRIFT for r in results):
        return EXIT_DRIFT
    if any(r.status == UNVERIFIABLE for r in results) or not results:
        return EXIT_UNVERIFIABLE
    return EXIT_OK


def render(results: Sequence[Result], target: str) -> str:
    lines = [f"globals watchdog: {target}"]
    for r in results:
        shown = "<unreadable>" if r.actual is None else r.actual
        lines.append(f"  {r.status.upper():<12} {r.name} expected={r.expected} actual={shown}")
        if r.detail:
            lines.append(f"               {r.detail}")
    code = exit_code(results)
    if code == EXIT_DRIFT:
        lines.insert(
            1,
            "ALERT ALERT ALERT: server globals DRIFTED from policy. NOT auto-fixed: "
            "investigate which login ran SET GLOBAL before resetting anything.",
        )
    elif code == EXIT_UNVERIFIABLE:
        lines.insert(
            1,
            "ALERT: could not verify the watched globals (server unreachable or a variable is "
            "not readable as a global). Treat as unverified; drop a variable via --drop/--config "
            "only as a deliberate policy decision.",
        )
    else:
        lines.append("  all watched globals match policy")
    return "\n".join(lines)


def persist_probe(client: Sequence[str], runner: Runner = _run) -> tuple[int, str]:
    """Return (exit code, message). PASS = the server refuses ``SET PERSIST``."""
    try:
        current = read_global(client, "max_connections", runner)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return EXIT_UNVERIFIABLE, f"ALERT: cannot read max_connections: {exc}"
    if not current.isdigit():
        return EXIT_UNVERIFIABLE, f"ALERT: unexpected max_connections value {current!r}"
    rc, out, err = runner([*client, f"SET PERSIST max_connections = {current}"], 30.0)
    if rc == 0:
        return EXIT_DRIFT, (
            "ALERT ALERT ALERT: SET PERSIST SUCCEEDED. The server's DOLT_ROOT_PATH is writable, "
            "so a persisted global would survive a restart. (The probe re-set max_connections "
            f"to its current value {current}; remove it from config_global.json and make the "
            "root read-only.)"
        )
    return EXIT_OK, f"ok: SET PERSIST refused ({(err or out).strip()[-160:]})"


def root_check(root: Path) -> tuple[int, list[str]]:
    """Static: the server's Dolt root must not be writable by the user running this check."""
    dot = root / ".dolt"
    cfg = dot / "config_global.json"
    problems: list[str] = []
    if not dot.is_dir():
        return EXIT_UNVERIFIABLE, [f"ALERT: {dot} does not exist"]
    if os.access(dot, os.W_OK):
        problems.append(f"{dot} is writable (SET PERSIST can create its temp file)")
    if cfg.exists() and os.access(cfg, os.W_OK):
        problems.append(f"{cfg} is writable")
    if not cfg.exists():
        problems.append(f"{cfg} is absent (provision it read-only before first start)")
    if problems:
        header = "ALERT: DOLT_ROOT_PATH is not read-only:"
        return EXIT_DRIFT, [header, *[f"  {p}" for p in problems]]
    return EXIT_OK, [f"ok: {dot} and config_global.json are read-only for this user"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dolt_globals_watchdog", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def conn(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--host", default=os.environ.get("DOLT_HOST", "127.0.0.1"))
        sp.add_argument("--port", type=int, default=int(os.environ.get("DOLT_PORT", "3306")))
        sp.add_argument("--user", default=os.environ.get("DOLT_USER", "root"))
        sp.add_argument("--tls", action="store_true", help="use TLS (default: --no-tls)")

    c = sub.add_parser("check", help="assert watched globals")
    conn(c)
    c.add_argument("--config", type=Path, help='JSON {"expect": {name: value}}; replaces defaults')
    c.add_argument("--expect", action="append", default=[], metavar="NAME=VALUE")
    c.add_argument("--drop", action="append", default=[], metavar="NAME")
    c.add_argument("--no-defaults", action="store_true")
    pr = sub.add_parser("persist", help="probe that SET PERSIST is refused")
    conn(pr)
    rc = sub.add_parser("root-check", help="assert DOLT_ROOT_PATH is read-only")
    rc.add_argument("--root", type=Path, default=os.environ.get("DOLT_ROOT_PATH"))
    return p


def main(argv: Sequence[str] | None = None, runner: Runner = _run) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "root-check":
        if not args.root:
            print("root-check needs --root or DOLT_ROOT_PATH", file=sys.stderr)
            return EXIT_UNVERIFIABLE
        code, lines = root_check(Path(args.root))
        print("\n".join(lines), file=sys.stderr if code else sys.stdout)
        return code
    client = _client(args.host, args.port, args.user, args.tls)
    target = f"{args.host}:{args.port}"
    if args.cmd == "persist":
        code, msg = persist_probe(client, runner)
        print(msg, file=sys.stderr if code else sys.stdout)
        return code
    config = json.loads(args.config.read_text()) if args.config else None
    try:
        watched = resolve_watched(
            config=config, expect=args.expect, drop=args.drop, use_defaults=not args.no_defaults
        )
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_UNVERIFIABLE
    results = check_globals(client, watched, runner)
    code = exit_code(results)
    print(render(results, target), file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
