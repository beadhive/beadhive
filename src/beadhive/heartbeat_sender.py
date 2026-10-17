"""In-tree frame heartbeat sender and its installable units (bh-i6ggn, ADR F7 / bh-32379 P-F7).

Replaces the out-of-tree ``~/.beadhive/factory-local-heartbeat.py`` (and its
``beadhive-factory-heartbeat.service``) with two independent jobs from the installed release:

* ``conformance`` — measure the slow host checks (``hive_ready`` …) into the local cache
  (:mod:`beadhive.heartbeat_conformance`). Runs every
  :data:`~beadhive.heartbeat_conformance.CONFORMANCE_INTERVAL_SECONDS`.
* ``beat`` — sign and publish one ``HeartbeatLease`` carrying the newest cached conformance.
  Never measures, so a slow or stalled conformance run cannot lapse the frame. Runs every
  :data:`~beadhive.heartbeat_conformance.INTERVAL_SECONDS` with the in-tree TTL. A
  session-only incarnation has no signed inbox: the beat is then skipped (exit 0).
* ``renew`` — on a data-switched ``dolt-server`` frame (its ``_session``/``_evidence`` tables
  exist; bh-owqdg, ADR §5), renew the server-stamped session row: **one UPDATE per tick**, on its
  own timer (:data:`~beadhive.heartbeat_conformance.RENEW_INTERVAL_SECONDS`), never waiting on
  the conformance job. Elsewhere it is a no-op (exit 0). ``renew --loop`` keeps ticking in one
  process instead of a timer.

On a switched frame the ``conformance`` job also writes its evidence row after each run, and
during Φ3 the ``beat`` keeps dual-writing signed inbox beats so 0.22.x readers and the receiver
keep working.

It is a module entrypoint (``python -m beadhive.heartbeat_sender``), not a new ``bh`` leaf, so
the published operation catalog and every wire schema stay byte-identical for 0.22.x.

Usage (run with the interpreter of the attested ``bh`` install)::

    python -m beadhive.heartbeat_sender conformance      # one conformance-job run
    python -m beadhive.heartbeat_sender beat             # one signed beat (cached conformance)
    python -m beadhive.heartbeat_sender renew            # one session-row renewal (switched)
    python -m beadhive.heartbeat_sender status           # cache age vs. the documented bound
    python -m beadhive.heartbeat_sender units            # print the units for this platform
    python -m beadhive.heartbeat_sender units --install  # write them (does not start them)
    python -m beadhive.heartbeat_sender units --install --replace  # drop operator edits
    python -m beadhive.heartbeat_sender units --remove   # delete them

Start / stop / verify, systemd (Linux)::

    systemctl --user daemon-reload
    systemctl --user enable --now beadhive-heartbeat-conformance.timer beadhive-heartbeat.timer
    systemctl --user enable --now beadhive-session-renew.timer   # switched dolt-server frames
    systemctl --user list-timers 'beadhive-heartbeat*'
    journalctl --user -u beadhive-heartbeat.service -n 20
    python -m beadhive.heartbeat_sender status
    bh host list                                         # the frame's BEAT_AGE stays < 60-ish s
    systemctl --user disable --now beadhive-heartbeat.timer beadhive-heartbeat-conformance.timer
    systemctl --user disable --now beadhive-session-renew.timer

Start / stop / verify, launchd (macOS)::

    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.beadhive.heartbeat-conformance.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.beadhive.heartbeat.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.beadhive.session-renew.plist
    launchctl print gui/$(id -u)/dev.beadhive.heartbeat
    python -m beadhive.heartbeat_sender status
    launchctl bootout gui/$(id -u)/dev.beadhive.heartbeat
    launchctl bootout gui/$(id -u)/dev.beadhive.heartbeat-conformance
    launchctl bootout gui/$(id -u)/dev.beadhive.session-renew

Every unit carries ``HOME``, ``BH_HOME``, ``PATH`` (and ``GIT_WORKSPACE`` when set) from the
installing environment and a ``WorkingDirectory`` (default: that ``HOME``;
``--working-directory`` overrides). Reinstalling over existing units keeps the operator's
environment, ``WorkingDirectory`` and extra LaunchAgent keys; a ``systemd`` directive bh does not
generate refuses with a diff until ``--replace`` (bh-atdi0, :mod:`beadhive.service_units`).

Retiring the factory shim: stop and disable ``beadhive-factory-heartbeat.timer`` /
``.service`` *before* enabling these timers, so only one sender advances ``seq``.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import heartbeat_conformance as hc
from . import service_units

SYSTEMD_BEAT = "beadhive-heartbeat"
SYSTEMD_CONFORMANCE = "beadhive-heartbeat-conformance"
LAUNCHD_BEAT = "dev.beadhive.heartbeat"
LAUNCHD_CONFORMANCE = "dev.beadhive.heartbeat-conformance"
SYSTEMD_RENEW = "beadhive-session-renew"
LAUNCHD_RENEW = "dev.beadhive.session-renew"
# A beat only reads authority, reads the cache and signs; it must finish well inside one
# interval. A conformance run is killed at the staleness bound: past it the cache already fails.
BEAT_TIMEOUT_SECONDS = hc.INTERVAL_SECONDS
CONFORMANCE_TIMEOUT_SECONDS = hc.CONFORMANCE_MAX_AGE_SECONDS
# A renewal is one UPDATE; it must finish inside its own interval.
RENEW_TIMEOUT_SECONDS = hc.RENEW_INTERVAL_SECONDS


@dataclass(frozen=True)
class UnitFile:
    name: str
    content: bytes


def _environment() -> dict[str, str]:
    """The installing environment a unit carries: launchd jobs inherit no login shell, so
    without ``HOME`` they resolve the wrong bh config and credential store (bh-atdi0)."""
    from .heartbeat_report import bh_home

    values = {
        "HOME": service_units.install_home(),
        "BH_HOME": str(bh_home()),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    workspace = os.environ.get("GIT_WORKSPACE")
    if workspace:
        values["GIT_WORKSPACE"] = workspace
    for name, value in values.items():
        service_units.safe_value(name, value)
    return values


def _argv(python: str, verb: str) -> list[str]:
    return [python, "-m", "beadhive.heartbeat_sender", verb]


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_UNSET: object = object()


def systemd_units(
    python: str | None = None,
    env: dict[str, str] | None = None,
    working_directory: str | None | object = _UNSET,
) -> list[UnitFile]:
    """The six user units. ``working_directory`` defaults to the unit's ``HOME``."""
    python = python or sys.executable
    env = _environment() if env is None else env
    if working_directory is _UNSET:
        working_directory = service_units.working_directory(env)
    environment = service_units.systemd_environment(env) + (
        service_units.systemd_working_directory(working_directory)
    )

    def service(description: str, verb: str, timeout: int, nice: int) -> bytes:
        exec_start = " ".join(_quote(part) for part in _argv(python, verb))
        return (
            "[Unit]\n"
            f"Description={description}\n"
            "Documentation=https://github.com/beadhive/beadhive\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"{environment}"
            f"ExecStart={exec_start}\n"
            f"TimeoutStartSec={timeout}\n"
            f"Nice={nice}\n"
        ).encode()

    def timer(description: str, unit: str, every: int) -> bytes:
        return (
            "[Unit]\n"
            f"Description={description}\n\n"
            "[Timer]\n"
            "OnBootSec=30\n"
            f"OnUnitActiveSec={every}\n"
            "AccuracySec=1s\n"
            f"Unit={unit}.service\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        ).encode()

    return [
        UnitFile(
            f"{SYSTEMD_BEAT}.service",
            service(
                "Beadhive frame heartbeat beat (signs cached conformance; never measures)",
                "beat",
                BEAT_TIMEOUT_SECONDS,
                0,
            ),
        ),
        UnitFile(
            f"{SYSTEMD_BEAT}.timer",
            timer("Beadhive frame heartbeat beat timer", SYSTEMD_BEAT, hc.INTERVAL_SECONDS),
        ),
        UnitFile(
            f"{SYSTEMD_CONFORMANCE}.service",
            service(
                "Beadhive frame conformance job (writes the local conformance cache)",
                "conformance",
                CONFORMANCE_TIMEOUT_SECONDS,
                10,
            ),
        ),
        UnitFile(
            f"{SYSTEMD_CONFORMANCE}.timer",
            timer(
                "Beadhive frame conformance timer",
                SYSTEMD_CONFORMANCE,
                hc.CONFORMANCE_INTERVAL_SECONDS,
            ),
        ),
        UnitFile(
            f"{SYSTEMD_RENEW}.service",
            service(
                "Beadhive session-row renewal (one UPDATE; switched dolt-server frames only)",
                "renew",
                RENEW_TIMEOUT_SECONDS,
                0,
            ),
        ),
        UnitFile(
            f"{SYSTEMD_RENEW}.timer",
            timer("Beadhive session-row renewal timer", SYSTEMD_RENEW, hc.RENEW_INTERVAL_SECONDS),
        ),
    ]


def launchd_payloads(
    python: str | None = None,
    env: dict[str, str] | None = None,
    working_directory: str | None | object = _UNSET,
) -> dict[str, dict]:
    """The three LaunchAgent payloads by file name (``launchd_units`` serialises them)."""
    python = python or sys.executable
    env = _environment() if env is None else env
    if working_directory is _UNSET:
        working_directory = service_units.working_directory(env)
    logs = Path(env["BH_HOME"]) / "heartbeat"

    def agent(label: str, verb: str, every: int, timeout: int) -> tuple[str, dict]:
        payload = {
            "Label": label,
            "ProgramArguments": _argv(python, verb),
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "StartInterval": every,
            "ExitTimeOut": timeout,
            "ProcessType": "Background",
            "StandardOutPath": str(logs / f"{verb}.out.log"),
            "StandardErrorPath": str(logs / f"{verb}.err.log"),
        }
        if working_directory is not None:
            payload["WorkingDirectory"] = service_units.safe_value("cwd", working_directory)
        return f"{label}.plist", payload

    return dict(
        [
            agent(LAUNCHD_BEAT, "beat", hc.INTERVAL_SECONDS, BEAT_TIMEOUT_SECONDS),
            agent(
                LAUNCHD_CONFORMANCE,
                "conformance",
                hc.CONFORMANCE_INTERVAL_SECONDS,
                CONFORMANCE_TIMEOUT_SECONDS,
            ),
            agent(LAUNCHD_RENEW, "renew", hc.RENEW_INTERVAL_SECONDS, RENEW_TIMEOUT_SECONDS),
        ]
    )


def launchd_units(
    python: str | None = None,
    env: dict[str, str] | None = None,
    working_directory: str | None | object = _UNSET,
) -> list[UnitFile]:
    return [
        UnitFile(name, plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))
        for name, payload in launchd_payloads(python, env, working_directory).items()
    ]


def default_platform() -> str:
    return "launchd" if sys.platform == "darwin" else "systemd"


def unit_dir(platform: str) -> Path:
    if platform == "launchd":
        return Path.home() / "Library" / "LaunchAgents"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user"


def units(
    platform: str,
    python: str | None = None,
    env: dict[str, str] | None = None,
    working_directory: str | None | object = _UNSET,
):
    return (launchd_units if platform == "launchd" else systemd_units)(
        python, env, working_directory
    )


def install(
    platform: str,
    directory: Path | None = None,
    *,
    replace: bool = False,
    working_directory: str | None | object = _UNSET,
    kept: list[str] | None = None,
) -> list[Path]:
    """Write the units, regenerating over installed ones without dropping operator values.

    An installed unit's environment and ``WorkingDirectory`` win over the generated ones (and
    extra LaunchAgent keys are kept); a ``systemd`` directive bh does not generate refuses with
    a diff (:class:`~beadhive.service_units.UnitConflict`) before anything is written.
    ``replace=True`` writes the generated units as is. ``kept`` collects ``"<unit>: <name>"``
    for each operator value kept.
    """
    directory = directory or unit_dir(platform)
    env = _environment()
    if working_directory is _UNSET:
        working_directory = service_units.working_directory(env)
    merged = {}
    for unit in units(platform, None, env, working_directory):
        path = directory / unit.name
        existing = path.read_bytes() if path.is_file() else None
        if platform == "launchd":
            merge = service_units.merge_launchd(
                unit.name,
                existing,
                lambda e, d, name=unit.name: launchd_payloads(None, e, d)[name],
                env,
                working_directory,
                replace=replace,
            )
        else:
            merge = service_units.merge_systemd(
                unit.name,
                existing,
                lambda e, d, name=unit.name: {
                    item.name: item.content for item in systemd_units(None, e, d)
                }[name],
                env,
                working_directory,
                replace=replace,
            )
        merged[path] = merge
    directory.mkdir(parents=True, exist_ok=True)
    for path, merge in merged.items():
        path.write_bytes(merge.content)
        if kept is not None:
            kept.extend(f"{path.name}: {name}" for name in merge.kept)
    return list(merged)


def remove(platform: str, directory: Path | None = None) -> list[Path]:
    directory = directory or unit_dir(platform)
    removed = []
    for unit in units(platform):
        path = directory / unit.name
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def status(now: float | None = None) -> dict:
    from .heartbeat_report import conformance_cache_path

    now = time.time() if now is None else now
    path = conformance_cache_path()
    cached = hc.read(path)
    checks = hc.beat_checks(cached, now=now)
    return {
        "cache": str(path),
        "measured_at": None if cached is None else hc.stamp(cached.measured_at),
        "age_seconds": None if cached is None else round(now - cached.measured_at, 1),
        "bound_seconds": hc.CONFORMANCE_MAX_AGE_SECONDS,
        "fresh": checks[hc.AGE_CHECK_ID]["status"] == "pass",
        "checks": {name: checks[name]["status"] for name in hc.CACHED_CHECK_IDS},
        "lease_duration_seconds": hc.LEASE_DURATION_SECONDS,
        "interval_seconds": hc.INTERVAL_SECONDS,
    }


def _beat(free_sessions: int) -> str:
    from .heartbeat_report import send_cached_beat

    return send_cached_beat(free_sessions=free_sessions)


def _renew(*, loop: bool, interval: float) -> int:
    """One renewal (or ``--loop``); a frame whose data has not switched is a no-op."""
    import threading

    from .heartbeat_report import session_renewer
    from .hq_sql_session import NotSwitched

    renewer = session_renewer()
    if renewer is None:
        print("session renewal skipped: no dolt-server frame runtime", file=sys.stderr)
        return 0
    try:
        if not loop:
            route = renewer.tick()
            print(json.dumps({"renewed": route.session}))
            return 0
        stop = threading.Event()

        def report(exc: BaseException) -> None:
            # Transport exceptions may carry credential references: print the class only.
            print(f"session renewal failed ({type(exc).__name__}); retrying", file=sys.stderr)

        renewer.run(interval_s=interval, stop=stop.wait, on_error=report)
        return 0
    except NotSwitched:
        print(
            "session renewal skipped: incarnation not switched (no session tables)", file=sys.stderr
        )
        return 0


def _publish_evidence() -> None:
    from .heartbeat_report import publish_session_evidence
    from .hq_sql_session import NotSwitched

    try:
        publish_session_evidence()
    except NotSwitched:
        return


def _free_sessions(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 1024:
        raise argparse.ArgumentTypeError("free sessions must be between zero and 1024")
    return number


def main(argv: list[str] | None = None) -> int:
    from . import fence_data

    fence_data.register()  # the product in-data fence resolver, before any guard path (bh-uz46l)
    parser = argparse.ArgumentParser(
        prog="python -m beadhive.heartbeat_sender",
        description="in-tree frame heartbeat sender: conformance job, beat, units",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("conformance", help="measure host conformance into the local cache")
    beat = sub.add_parser("beat", help="sign and publish one beat from cached conformance")
    beat.add_argument("--free-sessions", type=_free_sessions, default=0)
    renew = sub.add_parser("renew", help="renew this frame's server-stamped session row")
    renew.add_argument("--loop", action="store_true", help="keep renewing every --interval")
    renew.add_argument(
        "--interval", type=float, default=float(hc.RENEW_INTERVAL_SECONDS), help="seconds"
    )
    sub.add_parser("status", help="report the conformance cache age against its bound")
    unit = sub.add_parser("units", help="print, install or remove the sender units")
    unit.add_argument("--platform", choices=("systemd", "launchd"), default=default_platform())
    action = unit.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true")
    action.add_argument("--remove", action="store_true")
    unit.add_argument("--dir", type=Path, default=None, help="override the unit directory")
    unit.add_argument(
        "--working-directory",
        default=None,
        help="the units' WorkingDirectory (default: the installing HOME)",
    )
    unit.add_argument(
        "--replace",
        action="store_true",
        help="with --install: overwrite installed units, dropping operator-set values",
    )
    args = parser.parse_args(argv)

    if args.verb == "conformance":
        from .heartbeat_report import refresh_conformance

        cached = refresh_conformance()
        if cached is None:
            print("conformance run already in progress; skipped", file=sys.stderr)
            return 0
        print(json.dumps(status(), sort_keys=True))
        try:
            _publish_evidence()
        except Exception:
            print(
                "session evidence refused; verify grant, configuration and runtime", file=sys.stderr
            )
            return 1
        return 0
    if args.verb == "renew":
        try:
            return _renew(loop=args.loop, interval=args.interval)
        except Exception:
            print(
                "session renewal refused; verify grant, configuration and runtime", file=sys.stderr
            )
            return 1
    if args.verb == "beat":
        from .hq_sql_runtime import SessionOnlyIncarnation

        try:
            print(json.dumps({"digest": _beat(args.free_sessions)}))
        except SessionOnlyIncarnation:
            print("beat skipped: session-only incarnation (no signed inbox)", file=sys.stderr)
            return 0
        except Exception as exc:
            # Transport, signing and config exceptions may contain credential references:
            # print only a fixed cause category, never the exception text.
            from .heartbeat_report import refusal_category

            print(
                f"heartbeat refused ({refusal_category(exc)}); verify release, grant, "
                "configuration, signer and transport",
                file=sys.stderr,
            )
            return 1
        return 0
    if args.verb == "status":
        report = status()
        print(json.dumps(report, sort_keys=True, indent=2))
        return 0 if report["fresh"] else 1
    directory = _UNSET if args.working_directory is None else args.working_directory
    if args.install:
        kept: list[str] = []
        try:
            written = install(
                args.platform,
                args.dir,
                replace=args.replace,
                working_directory=directory,
                kept=kept,
            )
        except service_units.UnitConflict as exc:
            print(f"{exc}\nnothing was written; rerun with --replace to overwrite", file=sys.stderr)
            return 1
        for path in written:
            print(f"wrote {path}")
        for item in kept:
            print(f"kept operator-set {item}", file=sys.stderr)
        return 0
    if args.remove:
        for path in remove(args.platform, args.dir):
            print(f"removed {path}")
        return 0
    for item in units(args.platform, working_directory=directory):
        print(f"# --- {item.name} ---")
        print(item.content.decode(), end="" if item.content.endswith(b"\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
