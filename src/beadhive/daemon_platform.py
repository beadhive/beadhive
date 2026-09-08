"""OS-native and container lifecycle contract for the unified host daemon.

The daemon remains one process per designated OS account, canonical ``BH_HOME``, and
``host_id``.  macOS and Linux install into that account's native service manager.  A
container is different: Compose (or another orchestrator) runs ``bh-host-daemon`` as the
main workload under an init/reaper; this module never installs a supervisor in the image.

Unit tests drive the service-manager implementations through an injected command adapter.
Release evidence is stricter: every ratified real-machine cell must be reported ``passed``;
an unavailable target is a release failure, never a pytest skip or an inferred pass.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .kernel.daemon.contracts import supervision as daemon_supervisor
from .kernel.daemon.contracts.identity import DaemonKey, DaemonPaths

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

REQUIRED_RELEASE_CELLS: dict[str, tuple[str, ...]] = {
    "darwin": (
        "crash-restart",
        "bounded-stop",
        "login-logout",
        "sleep-wake",
        "reboot",
        "log-status-recovery",
        "duplicate-refusal",
        "wrong-identity",
    ),
    "linux": (
        "crash-restart",
        "bounded-stop",
        "login-logout",
        "sleep-wake",
        "reboot",
        "log-status-recovery",
        "duplicate-refusal",
        "wrong-identity",
    ),
    "container": (
        "restart-policy",
        "health",
        "stop-grace",
        "persistent-bh-home",
        "credentials",
        "enospc",
        "descendant-reaping",
    ),
}


class ReleaseCellError(RuntimeError):
    """A required platform release cell is unavailable, missing, or red."""


class ContainerContractError(ValueError):
    """The container service does not honor the daemon lifecycle boundary."""


def validate_release_cells(
    platform_name: str,
    results: Mapping[str, Mapping[str, Any]],
    *,
    available: bool = True,
    expected_revision: str,
    now: datetime | None = None,
    maximum_age: timedelta = timedelta(days=7),
) -> None:
    """Fail closed unless every required real-platform observation is present and green.

    Unit command fixtures deliberately cannot satisfy this boundary.  Each cell must name a
    real target, the exact tested source revision, and a recent timezone-aware observation.
    """
    try:
        required = REQUIRED_RELEASE_CELLS[platform_name]
    except KeyError as exc:
        raise ReleaseCellError(f"unknown host-daemon release platform: {platform_name}") from exc
    if not available:
        raise ReleaseCellError(
            f"{platform_name} host-daemon release platform is UNAVAILABLE; "
            "required lifecycle cells cannot be skipped"
        )
    unexpected = sorted(set(results) - set(required))
    if unexpected:
        raise ReleaseCellError(
            f"{platform_name} release evidence has unknown cells: {', '.join(unexpected)}"
        )
    missing = [cell for cell in required if cell not in results]
    if missing:
        raise ReleaseCellError(
            f"{platform_name} release evidence is missing required cells: {', '.join(missing)}"
        )
    failed = [cell for cell in required if results[cell].get("result") != "passed"]
    if failed:
        raise ReleaseCellError(
            f"{platform_name} release evidence has failed cells: {', '.join(failed)}"
        )
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("release evidence comparison time must be timezone-aware")
    for cell in required:
        evidence = results[cell]
        if evidence.get("execution") != "real":
            raise ReleaseCellError(
                f"{platform_name}/{cell} lacks real execution provenance; fixtures do not release"
            )
        if evidence.get("platform") != platform_name:
            raise ReleaseCellError(f"{platform_name}/{cell} has wrong platform provenance")
        target = evidence.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ReleaseCellError(f"{platform_name}/{cell} has no target provenance")
        if evidence.get("sourceRevision") != expected_revision:
            raise ReleaseCellError(
                f"{platform_name}/{cell} revision does not match {expected_revision}"
            )
        detail = evidence.get("detail")
        if not isinstance(detail, str) or not detail.strip():
            raise ReleaseCellError(f"{platform_name}/{cell} has no observation detail")
        raw_observed = evidence.get("observedAt")
        try:
            observed = datetime.fromisoformat(str(raw_observed))
        except ValueError as exc:
            raise ReleaseCellError(
                f"{platform_name}/{cell} observedAt is not an ISO timestamp"
            ) from exc
        if observed.tzinfo is None:
            raise ReleaseCellError(f"{platform_name}/{cell} observedAt is not timezone-aware")
        age = now - observed.astimezone(UTC)
        if age < -timedelta(minutes=5):
            raise ReleaseCellError(f"{platform_name}/{cell} observation is in the future")
        if age > maximum_age:
            raise ReleaseCellError(f"{platform_name}/{cell} observation is stale")


def validate_release_document(
    document: Mapping[str, Any],
    *,
    expected_revision: str,
    now: datetime | None = None,
) -> None:
    """Validate one three-plane release evidence document against the closed v1 matrix."""
    if document.get("schemaVersion") != 1:
        raise ReleaseCellError("host-daemon release evidence schemaVersion must be 1")
    platforms = document.get("platforms")
    if not isinstance(platforms, Mapping):
        raise ReleaseCellError("host-daemon release evidence has no platforms object")
    missing_platforms = [name for name in REQUIRED_RELEASE_CELLS if name not in platforms]
    if missing_platforms:
        raise ReleaseCellError(
            f"host-daemon release evidence is missing platforms: {', '.join(missing_platforms)}"
        )
    unexpected_platforms = sorted(set(platforms) - set(REQUIRED_RELEASE_CELLS))
    if unexpected_platforms:
        raise ReleaseCellError(
            f"host-daemon release evidence has unknown platforms: {', '.join(unexpected_platforms)}"
        )
    for platform_name in REQUIRED_RELEASE_CELLS:
        platform_evidence = platforms[platform_name]
        if not isinstance(platform_evidence, Mapping):
            raise ReleaseCellError(f"{platform_name} release evidence must be an object")
        cells = platform_evidence.get("cells")
        if not isinstance(cells, Mapping):
            raise ReleaseCellError(f"{platform_name} release evidence has no cells object")
        validate_release_cells(
            platform_name,
            cells,
            available=platform_evidence.get("available") is True,
            expected_revision=expected_revision,
            now=now,
        )


def main(argv: list[str] | None = None) -> None:
    """Release-only evidence gate; it never executes lifecycle mutations itself."""
    parser = argparse.ArgumentParser(
        description="validate real host-daemon platform lifecycle release evidence"
    )
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.evidence.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ReleaseCellError("host-daemon release evidence root must be an object")
        validate_release_document(document, expected_revision=args.revision)
    except (OSError, json.JSONDecodeError, ReleaseCellError) as exc:
        parser.exit(1, f"host-daemon platform release FAILED: {exc}\n")
    print("host-daemon platform release evidence: all required real cells passed")


def _duration_seconds(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        raise ContainerContractError("container stop grace must be a duration")
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m)\s*", value)
    if match is None:
        raise ContainerContractError("container stop grace has an unsupported duration")
    amount = float(match.group(1))
    return amount * {"ms": 0.001, "s": 1.0, "m": 60.0}[match.group(2)]


def validate_container_contract(
    service: Mapping[str, object], *, minimum_stop_seconds: float
) -> None:
    """Validate the portable Compose contract without invoking a container runtime."""
    if service.get("command") != ["bh-host-daemon"]:
        raise ContainerContractError("daemon must be the container main workload")
    if service.get("init") is not True:
        raise ContainerContractError("container must use an init/reaper for descendants")
    if service.get("restart") not in {"always", "unless-stopped"}:
        raise ContainerContractError("container restart policy is missing")
    if _duration_seconds(service.get("stop_grace_period")) < minimum_stop_seconds:
        raise ContainerContractError("container stop grace is shorter than daemon shutdown")

    health = service.get("healthcheck")
    test = health.get("test") if isinstance(health, Mapping) else None
    if (
        not isinstance(test, list)
        or not test
        or test[0] != "CMD"
        or "/health" not in " ".join(test)
    ):
        raise ContainerContractError("container healthcheck must use the public /health route")

    environment = service.get("environment")
    bh_home = environment.get("BH_HOME") if isinstance(environment, Mapping) else None
    volumes = service.get("volumes")
    if not isinstance(bh_home, str) or not bh_home.startswith("/"):
        raise ContainerContractError("container BH_HOME must be an absolute persistent path")
    if not isinstance(volumes, list) or not any(
        isinstance(volume, str) and volume.endswith(f":{bh_home}") for volume in volumes
    ):
        raise ContainerContractError("container BH_HOME must be backed by a persistent volume")


def _run_command(argv: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _default_daemon_binary() -> str:
    discovered = shutil.which("bh-host-daemon")
    if discovered:
        return str(Path(discovered).resolve())
    return str(Path(sys.executable).resolve().with_name("bh-host-daemon"))


def _default_shutdown_budget() -> float:
    try:
        from .config_consumer_ports import daemon_settings as config
        from .config_schema import BeadhiveConfig

        parsed = BeadhiveConfig.model_validate(config.load())
        return float(parsed.host.daemon.shutdown.graceful_seconds)
    except (AttributeError, FileNotFoundError, TypeError, ValueError):
        return 30.0


def _account_uid(key: DaemonKey, expected_uid: int) -> None:
    expected = f"uid:{expected_uid}"
    if key.account_id != expected:
        raise daemon_supervisor.SupervisorIdentityError(
            "supervisor target belongs to another OS account "
            f"({key.account_id}, expected {expected})"
        )


def _result_detail(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    return (result.stderr or result.stdout or fallback).strip()


def _environment_lines(key: DaemonKey) -> tuple[str, ...]:
    values = {"BH_HOME": key.bh_home}
    workspace = os.environ.get("GIT_WORKSPACE")
    if workspace:
        values["GIT_WORKSPACE"] = workspace
    lines = []
    for name, value in values.items():
        if "\n" in value or "\0" in value:
            raise daemon_supervisor.SupervisorError(f"{name} contains an unsafe service value")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'Environment="{name}={escaped}"')
    return tuple(lines)


class SystemdUserSupervisorBackend:
    """One persistent ``systemd --user`` service for one exact daemon singleton key."""

    name = "systemd-user"

    def __init__(
        self,
        *,
        unit_dir: Path | None = None,
        daemon_binary: str | None = None,
        uid: int | None = None,
        shutdown_budget: float | None = None,
        run: CommandRunner = _run_command,
    ) -> None:
        self.unit_dir = unit_dir or _systemd_user_dir()
        self.daemon_binary = daemon_binary or _default_daemon_binary()
        self.uid = uid if uid is not None else os.getuid()
        self.shutdown_budget = (
            _default_shutdown_budget() if shutdown_budget is None else float(shutdown_budget)
        )
        self.run = run

    def _command(self, *argv: str) -> subprocess.CompletedProcess[str]:
        try:
            return self.run(list(argv), timeout=5.0)
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(list(argv), 127, "", str(exc))

    def _unit(self, key: DaemonKey) -> str:
        return f"{daemon_supervisor.service_name(key)}.service"

    def _path(self, key: DaemonKey) -> Path:
        return self.unit_dir / self._unit(key)

    def _prerequisites(self, key: DaemonKey) -> tuple[bool, bool, str]:
        _account_uid(key, self.uid)
        manager = self._command("systemctl", "--user", "show-environment")
        linger = self._command("loginctl", "show-user", str(self.uid), "-p", "Linger", "--value")
        manager_ready = manager.returncode == 0
        linger_ready = linger.returncode == 0 and linger.stdout.strip().lower() == "yes"
        if not manager_ready:
            return (
                False,
                False,
                (
                    "systemd user manager unavailable: "
                    f"{_result_detail(manager, 'systemctl --user failed')}"
                ),
            )
        if not linger_ready:
            return (
                True,
                False,
                (
                    "systemd user manager is not boot-persistent; an administrator must run "
                    f"loginctl enable-linger {self.uid}"
                ),
            )
        return True, True, "systemd user manager is available and linger is enabled"

    def _unit_text(self, key: DaemonKey) -> str:
        binary = shlex.quote(self.daemon_binary)
        environment = "\n".join(_environment_lines(key))
        timeout = max(1, math.ceil(self.shutdown_budget))
        return (
            "[Unit]\n"
            "Description=Beadhive host daemon\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "StartLimitIntervalSec=600\n"
            "StartLimitBurst=20\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"{environment}\n"
            f"ExecStart={binary}\n"
            "Restart=on-failure\n"
            "RestartSec=2\n"
            "KillSignal=SIGTERM\n"
            "KillMode=control-group\n"
            f"TimeoutStopSec={timeout}\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def _require_manageable(self, key: DaemonKey) -> None:
        manager, persistent, detail = self._prerequisites(key)
        if not manager or not persistent:
            raise daemon_supervisor.SupervisorUnavailableError(detail)

    def _require_manager(self, key: DaemonKey) -> None:
        manager, _persistent, detail = self._prerequisites(key)
        if not manager:
            raise daemon_supervisor.SupervisorUnavailableError(detail)

    def install(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        self._require_manageable(key)
        self.unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(key)
        text = self._unit_text(key)
        previously_installed = path.exists()
        if not previously_installed or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
            reload_result = self._command("systemctl", "--user", "daemon-reload")
            if reload_result.returncode != 0:
                raise daemon_supervisor.SupervisorError(
                    _result_detail(reload_result, "systemctl --user daemon-reload failed")
                )
            # ``enable --now`` starts an inactive unit but deliberately does not restart one
            # that is already active.  After changing an installed definition, that would
            # leave the live process on its old ExecStart/environment/TimeoutStopSec.  The
            # systemd-native atomic operation restarts an active unit and is a no-op for an
            # inactive one, preserving the install/start path below without a status race.
            if previously_installed:
                restarted = self._command("systemctl", "--user", "try-restart", self._unit(key))
                if restarted.returncode != 0:
                    raise daemon_supervisor.SupervisorError(
                        _result_detail(restarted, f"could not restart changed {self._unit(key)}")
                    )
        enabled = self._command("systemctl", "--user", "enable", "--now", self._unit(key))
        if enabled.returncode != 0:
            raise daemon_supervisor.SupervisorError(
                _result_detail(enabled, f"could not enable {self._unit(key)}")
            )
        return self.status(key)

    def start(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        self._require_manageable(key)
        if not self._path(key).exists():
            raise daemon_supervisor.SupervisorUnavailableError(
                f"{self._unit(key)} is not installed; run bh host daemon install"
            )
        result = self._command("systemctl", "--user", "start", self._unit(key))
        if result.returncode != 0:
            raise daemon_supervisor.SupervisorError(
                _result_detail(result, f"could not start {self._unit(key)}")
            )
        return self.status(key)

    def stop(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        # Cleanup remains possible if linger was disabled after installation, provided the
        # current login session still has a reachable user manager.
        self._require_manager(key)
        if self._path(key).exists():
            result = self._command("systemctl", "--user", "stop", self._unit(key))
            if result.returncode != 0:
                raise daemon_supervisor.SupervisorError(
                    _result_detail(result, f"could not stop {self._unit(key)}")
                )
        return self.status(key)

    def remove(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        self._require_manager(key)
        path = self._path(key)
        if path.exists():
            disabled = self._command("systemctl", "--user", "disable", "--now", self._unit(key))
            if disabled.returncode != 0:
                raise daemon_supervisor.SupervisorError(
                    _result_detail(disabled, f"could not disable {self._unit(key)}")
                )
            path.unlink()
            reload_result = self._command("systemctl", "--user", "daemon-reload")
            if reload_result.returncode != 0:
                raise daemon_supervisor.SupervisorError(
                    _result_detail(reload_result, "systemctl --user daemon-reload failed")
                )
        return self.status(key)

    def status(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        manager, persistent_manager, detail = self._prerequisites(key)
        path = self._path(key)
        if not manager:
            return daemon_supervisor._state(
                key,
                backend=self.name,
                detectable=False,
                supported=False,
                installed=path.exists(),
                running=None,
                persisted=False,
                detail=detail,
            )
        active = self._command("systemctl", "--user", "is-active", self._unit(key))
        enabled = self._command("systemctl", "--user", "is-enabled", self._unit(key))
        running = active.returncode == 0 and active.stdout.strip() == "active"
        enabled_state = enabled.stdout.strip()
        # Only ``enabled`` means systemd installed durable wants symlinks. Runtime enablement
        # disappears at reboot, while a static unit has no enablement links at all.
        enabled_unit = enabled.returncode == 0 and enabled_state == "enabled"
        installed = path.exists()
        return daemon_supervisor._state(
            key,
            backend=self.name,
            detectable=True,
            supported=persistent_manager,
            installed=installed,
            running=running,
            persisted=installed and enabled_unit and persistent_manager,
            detail=(
                f"{detail}; is-active={active.stdout.strip() or 'unknown'} "
                f"is-enabled={enabled_state or 'unknown'}"
            ),
        )


def _systemd_user_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base.expanduser() / "systemd" / "user"


class LaunchAgentSupervisorBackend:
    """A per-user LaunchAgent for one exact daemon singleton key."""

    name = "launchagent"

    def __init__(
        self,
        *,
        agent_dir: Path | None = None,
        daemon_binary: str | None = None,
        uid: int | None = None,
        shutdown_budget: float | None = None,
        run: CommandRunner = _run_command,
    ) -> None:
        self.agent_dir = agent_dir or Path.home() / "Library" / "LaunchAgents"
        self.daemon_binary = daemon_binary or _default_daemon_binary()
        self.uid = uid if uid is not None else os.getuid()
        self.shutdown_budget = (
            _default_shutdown_budget() if shutdown_budget is None else float(shutdown_budget)
        )
        self.run = run

    def _command(self, *argv: str) -> subprocess.CompletedProcess[str]:
        try:
            return self.run(list(argv), timeout=5.0)
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(list(argv), 127, "", str(exc))

    def _label(self, key: DaemonKey) -> str:
        return f"dev.beadhive.{daemon_supervisor.service_name(key)}"

    def _path(self, key: DaemonKey) -> Path:
        return self.agent_dir / f"{self._label(key)}.plist"

    def _domain(self) -> str:
        return f"gui/{self.uid}"

    def _target(self, key: DaemonKey) -> str:
        return f"{self._domain()}/{self._label(key)}"

    def _plist(self, key: DaemonKey) -> bytes:
        paths = DaemonPaths.for_key(key)
        environment = {"BH_HOME": key.bh_home}
        workspace = os.environ.get("GIT_WORKSPACE")
        if workspace:
            environment["GIT_WORKSPACE"] = workspace
        payload = {
            "Label": self._label(key),
            "ProgramArguments": [self.daemon_binary],
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
            "ExitTimeOut": max(1, math.ceil(self.shutdown_budget)),
            "StandardOutPath": str(paths.directory / "stdout.log"),
            "StandardErrorPath": str(paths.directory / "stderr.log"),
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)

    def install(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        _account_uid(key, self.uid)
        self.agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        DaemonPaths.for_key(key).directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(key)
        payload = self._plist(key)
        installed = path.exists()
        changed = not installed or path.read_bytes() != payload
        loaded = False
        running = False
        if installed:
            inspection = self._command("launchctl", "print", self._target(key))
            if inspection.returncode == 0:
                loaded = True
                running = "state = running" in inspection.stdout
            elif inspection.returncode not in {3, 64, 113}:
                raise daemon_supervisor.SupervisorError(
                    _result_detail(inspection, f"could not inspect {self._label(key)}")
                )

        # launchd retains the loaded job definition independently from the plist on disk.
        # Unload the old definition before replacing a changed file, then bootstrap exactly
        # the new bytes. An identical loaded job needs neither operation.
        if changed and loaded:
            bootout = self._command("launchctl", "bootout", self._domain(), str(path))
            if bootout.returncode != 0:
                raise daemon_supervisor.SupervisorError(
                    _result_detail(bootout, f"could not bootout {self._label(key)}")
                )
            loaded = False
            running = False
        if changed:
            path.write_bytes(payload)
            path.chmod(0o600)
        if not loaded:
            bootstrap = self._command("launchctl", "bootstrap", self._domain(), str(path))
            if bootstrap.returncode != 0:
                raise daemon_supervisor.SupervisorError(
                    _result_detail(bootstrap, f"could not bootstrap {self._label(key)}")
                )
            running = False
        if not running:
            kickstart = self._command("launchctl", "kickstart", "-k", self._target(key))
            if kickstart.returncode != 0:
                raise daemon_supervisor.SupervisorError(
                    _result_detail(kickstart, f"could not kickstart {self._label(key)}")
                )
        return self.status(key)

    def start(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        _account_uid(key, self.uid)
        if not self._path(key).exists():
            raise daemon_supervisor.SupervisorUnavailableError(
                f"{self._label(key)} is not installed; run bh host daemon install"
            )
        result = self._command("launchctl", "kickstart", "-k", self._target(key))
        if result.returncode != 0:
            raise daemon_supervisor.SupervisorError(
                _result_detail(result, f"could not kickstart {self._label(key)}")
            )
        return self.status(key)

    def stop(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        _account_uid(key, self.uid)
        if self._path(key).exists():
            result = self._command("launchctl", "kill", "SIGTERM", self._target(key))
            if result.returncode != 0 and "could not find service" not in result.stderr.lower():
                raise daemon_supervisor.SupervisorError(
                    _result_detail(result, f"could not stop {self._label(key)}")
                )
        return self.status(key)

    def remove(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        _account_uid(key, self.uid)
        path = self._path(key)
        if path.exists():
            result = self._command("launchctl", "bootout", self._domain(), str(path))
            if result.returncode != 0 and "could not find service" not in result.stderr.lower():
                raise daemon_supervisor.SupervisorError(
                    _result_detail(result, f"could not bootout {self._label(key)}")
                )
            path.unlink(missing_ok=True)
        return self.status(key)

    def status(self, key: DaemonKey) -> daemon_supervisor.SupervisorState:
        _account_uid(key, self.uid)
        installed = self._path(key).exists()
        result = self._command("launchctl", "print", self._target(key))
        detectable = result.returncode in {0, 3, 64, 113}
        running = result.returncode == 0 and "state = running" in result.stdout
        detail = (
            f"launchctl print {self._target(key)}: "
            f"{_result_detail(result, 'loaded' if result.returncode == 0 else 'not loaded')}"
        )
        return daemon_supervisor._state(
            key,
            backend=self.name,
            detectable=detectable,
            supported=True,
            installed=installed,
            running=running,
            persisted=installed,
            detail=detail,
        )


if __name__ == "__main__":  # pragma: no cover - exercised by the release recipe
    main()
