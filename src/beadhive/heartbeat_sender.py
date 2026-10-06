"""In-tree frame heartbeat sender and its installable units (bh-i6ggn, ADR F7 / bh-32379 P-F7).

Replaces the out-of-tree ``~/.beadhive/factory-local-heartbeat.py`` (and its
``beadhive-factory-heartbeat.service``) with two independent jobs from the installed release:

* ``conformance`` — measure the slow host checks (``hive_ready`` …) into the local cache
  (:mod:`beadhive.heartbeat_conformance`). Runs every
  :data:`~beadhive.heartbeat_conformance.CONFORMANCE_INTERVAL_SECONDS`.
* ``beat`` — sign and publish one ``HeartbeatLease`` carrying the newest cached conformance.
  Never measures, so a slow or stalled conformance run cannot lapse the frame. Runs every
  :data:`~beadhive.heartbeat_conformance.INTERVAL_SECONDS` with the in-tree TTL.

It is a module entrypoint (``python -m beadhive.heartbeat_sender``), not a new ``bh`` leaf, so
the published operation catalog and every wire schema stay byte-identical for 0.22.x.

Usage (run with the interpreter of the attested ``bh`` install)::

    python -m beadhive.heartbeat_sender conformance      # one conformance-job run
    python -m beadhive.heartbeat_sender beat             # one signed beat (cached conformance)
    python -m beadhive.heartbeat_sender status           # cache age vs. the documented bound
    python -m beadhive.heartbeat_sender units            # print the units for this platform
    python -m beadhive.heartbeat_sender units --install  # write them (does not start them)
    python -m beadhive.heartbeat_sender units --remove   # delete them

Start / stop / verify, systemd (Linux)::

    systemctl --user daemon-reload
    systemctl --user enable --now beadhive-heartbeat-conformance.timer beadhive-heartbeat.timer
    systemctl --user list-timers 'beadhive-heartbeat*'
    journalctl --user -u beadhive-heartbeat.service -n 20
    python -m beadhive.heartbeat_sender status
    bh host list                                         # the frame's BEAT_AGE stays < 60-ish s
    systemctl --user disable --now beadhive-heartbeat.timer beadhive-heartbeat-conformance.timer

Start / stop / verify, launchd (macOS)::

    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.beadhive.heartbeat-conformance.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.beadhive.heartbeat.plist
    launchctl print gui/$(id -u)/dev.beadhive.heartbeat
    python -m beadhive.heartbeat_sender status
    launchctl bootout gui/$(id -u)/dev.beadhive.heartbeat
    launchctl bootout gui/$(id -u)/dev.beadhive.heartbeat-conformance

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

SYSTEMD_BEAT = "beadhive-heartbeat"
SYSTEMD_CONFORMANCE = "beadhive-heartbeat-conformance"
LAUNCHD_BEAT = "dev.beadhive.heartbeat"
LAUNCHD_CONFORMANCE = "dev.beadhive.heartbeat-conformance"
# A beat only reads authority, reads the cache and signs; it must finish well inside one
# interval. A conformance run is killed at the staleness bound: past it the cache already fails.
BEAT_TIMEOUT_SECONDS = hc.INTERVAL_SECONDS
CONFORMANCE_TIMEOUT_SECONDS = hc.CONFORMANCE_MAX_AGE_SECONDS


@dataclass(frozen=True)
class UnitFile:
    name: str
    content: bytes


def _environment() -> dict[str, str]:
    from .heartbeat_report import bh_home

    values = {"BH_HOME": str(bh_home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    workspace = os.environ.get("GIT_WORKSPACE")
    if workspace:
        values["GIT_WORKSPACE"] = workspace
    for name, value in values.items():
        if "\n" in value or "\0" in value:
            raise ValueError(f"{name} contains an unsafe service value")
    return values


def _argv(python: str, verb: str) -> list[str]:
    return [python, "-m", "beadhive.heartbeat_sender", verb]


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def systemd_units(python: str | None = None, env: dict[str, str] | None = None) -> list[UnitFile]:
    python = python or sys.executable
    env = _environment() if env is None else env
    environment = "".join(f"Environment={_quote(f'{k}={v}')}\n" for k, v in env.items())

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
    ]


def launchd_units(python: str | None = None, env: dict[str, str] | None = None) -> list[UnitFile]:
    python = python or sys.executable
    env = _environment() if env is None else env
    logs = Path(env["BH_HOME"]) / "heartbeat"

    def agent(label: str, verb: str, every: int, timeout: int) -> UnitFile:
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
        return UnitFile(
            f"{label}.plist", plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
        )

    return [
        agent(LAUNCHD_BEAT, "beat", hc.INTERVAL_SECONDS, BEAT_TIMEOUT_SECONDS),
        agent(
            LAUNCHD_CONFORMANCE,
            "conformance",
            hc.CONFORMANCE_INTERVAL_SECONDS,
            CONFORMANCE_TIMEOUT_SECONDS,
        ),
    ]


def default_platform() -> str:
    return "launchd" if sys.platform == "darwin" else "systemd"


def unit_dir(platform: str) -> Path:
    if platform == "launchd":
        return Path.home() / "Library" / "LaunchAgents"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user"


def units(platform: str, python: str | None = None, env: dict[str, str] | None = None):
    return (launchd_units if platform == "launchd" else systemd_units)(python, env)


def install(platform: str, directory: Path | None = None) -> list[Path]:
    directory = directory or unit_dir(platform)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for unit in units(platform):
        path = directory / unit.name
        path.write_bytes(unit.content)
        written.append(path)
    return written


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


def _free_sessions(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 1024:
        raise argparse.ArgumentTypeError("free sessions must be between zero and 1024")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m beadhive.heartbeat_sender",
        description="in-tree frame heartbeat sender: conformance job, beat, units",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("conformance", help="measure host conformance into the local cache")
    beat = sub.add_parser("beat", help="sign and publish one beat from cached conformance")
    beat.add_argument("--free-sessions", type=_free_sessions, default=0)
    sub.add_parser("status", help="report the conformance cache age against its bound")
    unit = sub.add_parser("units", help="print, install or remove the sender units")
    unit.add_argument("--platform", choices=("systemd", "launchd"), default=default_platform())
    action = unit.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true")
    action.add_argument("--remove", action="store_true")
    unit.add_argument("--dir", type=Path, default=None, help="override the unit directory")
    args = parser.parse_args(argv)

    if args.verb == "conformance":
        from .heartbeat_report import refresh_conformance

        cached = refresh_conformance()
        if cached is None:
            print("conformance run already in progress; skipped", file=sys.stderr)
            return 0
        print(json.dumps(status(), sort_keys=True))
        return 0
    if args.verb == "beat":
        try:
            print(json.dumps({"digest": _beat(args.free_sessions)}))
        except Exception:
            # Transport, signing and config exceptions may contain credential references.
            print(
                "heartbeat refused; verify release, grant, configuration and signer",
                file=sys.stderr,
            )
            return 1
        return 0
    if args.verb == "status":
        report = status()
        print(json.dumps(report, sort_keys=True, indent=2))
        return 0 if report["fresh"] else 1
    if args.install:
        for path in install(args.platform, args.dir):
            print(f"wrote {path}")
        return 0
    if args.remove:
        for path in remove(args.platform, args.dir):
            print(f"removed {path}")
        return 0
    for item in units(args.platform):
        print(f"# --- {item.name} ---")
        print(item.content.decode(), end="" if item.content.endswith(b"\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
