"""Ratified OS/container lifecycle contract for the unified host daemon."""

from __future__ import annotations

import errno
import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from beadhive import daemon_platform, daemon_supervisor, host_daemon


class CommandFixture:
    """Deterministic platform command adapter; no host service manager is touched."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str, str]] | None = None):
        self.answers = answers or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: list[str], *, timeout: float = 5.0
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        command = tuple(argv)
        self.calls.append(command)
        returncode, stdout, stderr = self.answers.get(command, (0, "", ""))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _key(tmp_path: Path) -> host_daemon.DaemonKey:
    return host_daemon.DaemonKey("uid:1234", str(tmp_path.resolve()), "host-1")


def test_systemd_user_install_is_keyed_persistent_and_bounded(tmp_path):
    key = _key(tmp_path)
    unit_dir = tmp_path / "systemd" / "user"
    unit = f"{daemon_supervisor.service_name(key)}.service"
    commands = CommandFixture(
        {
            ("loginctl", "show-user", "1234", "-p", "Linger", "--value"): (0, "yes\n", ""),
            ("systemctl", "--user", "show-environment"): (0, "HOME=/tmp\n", ""),
            ("systemctl", "--user", "is-active", unit): (0, "active\n", ""),
            ("systemctl", "--user", "is-enabled", unit): (0, "enabled\n", ""),
        }
    )
    backend = daemon_platform.SystemdUserSupervisorBackend(
        unit_dir=unit_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        shutdown_budget=7.2,
        run=commands,
    )

    state = backend.install(key)
    text = (unit_dir / unit).read_text()

    assert state.supported and state.installed and state.running and state.persisted
    assert "ExecStart=/opt/beadhive/bin/bh-host-daemon" in text
    assert f'Environment="BH_HOME={key.bh_home}"' in text
    assert "Restart=on-failure" in text
    assert "KillMode=control-group" in text
    assert "TimeoutStopSec=8" in text
    unit_section, service_section = text.split("[Service]", 1)
    assert "StartLimitIntervalSec=600" in unit_section
    assert "StartLimitBurst=20" in unit_section
    assert "StartLimit" not in service_section
    assert ("systemctl", "--user", "daemon-reload") in commands.calls
    assert ("systemctl", "--user", "try-restart", unit) not in commands.calls
    assert ("systemctl", "--user", "enable", "--now", unit) in commands.calls


def test_systemd_user_changed_active_unit_restarts_after_reload_before_status(
    tmp_path, monkeypatch
):
    key = _key(tmp_path)
    unit_dir = tmp_path / "systemd" / "user"
    unit = f"{daemon_supervisor.service_name(key)}.service"
    path = unit_dir / unit
    path.parent.mkdir(parents=True)
    path.write_text("stale live unit\n")
    monkeypatch.setenv("GIT_WORKSPACE", "/srv/current-workspace")
    commands = CommandFixture(
        {
            ("loginctl", "show-user", "1234", "-p", "Linger", "--value"): (
                0,
                "yes\n",
                "",
            ),
            ("systemctl", "--user", "show-environment"): (0, "HOME=/tmp\n", ""),
            ("systemctl", "--user", "is-active", unit): (0, "active\n", ""),
            ("systemctl", "--user", "is-enabled", unit): (0, "enabled\n", ""),
        }
    )
    backend = daemon_platform.SystemdUserSupervisorBackend(
        unit_dir=unit_dir,
        daemon_binary="/opt/beadhive/current/bh-host-daemon",
        uid=1234,
        shutdown_budget=12.1,
        run=commands,
    )

    state = backend.install(key)

    text = path.read_text()
    assert state.running and state.persisted
    assert "ExecStart=/opt/beadhive/current/bh-host-daemon" in text
    assert f'Environment="BH_HOME={key.bh_home}"' in text
    assert 'Environment="GIT_WORKSPACE=/srv/current-workspace"' in text
    assert "TimeoutStopSec=13" in text
    reload_call = ("systemctl", "--user", "daemon-reload")
    restart_call = ("systemctl", "--user", "try-restart", unit)
    enable_call = ("systemctl", "--user", "enable", "--now", unit)
    assert commands.calls.index(reload_call) < commands.calls.index(restart_call)
    assert commands.calls.index(restart_call) < commands.calls.index(enable_call)


def test_systemd_user_identical_unit_install_does_not_reload_or_restart(tmp_path):
    key = _key(tmp_path)
    unit_dir = tmp_path / "systemd" / "user"
    unit = f"{daemon_supervisor.service_name(key)}.service"
    commands = CommandFixture(
        {
            ("loginctl", "show-user", "1234", "-p", "Linger", "--value"): (
                0,
                "yes\n",
                "",
            ),
            ("systemctl", "--user", "show-environment"): (0, "HOME=/tmp\n", ""),
            ("systemctl", "--user", "is-active", unit): (0, "active\n", ""),
            ("systemctl", "--user", "is-enabled", unit): (0, "enabled\n", ""),
        }
    )
    backend = daemon_platform.SystemdUserSupervisorBackend(
        unit_dir=unit_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        shutdown_budget=7.2,
        run=commands,
    )
    path = unit_dir / unit
    path.parent.mkdir(parents=True)
    path.write_text(backend._unit_text(key))

    state = backend.install(key)

    assert state.running and state.persisted
    assert ("systemctl", "--user", "daemon-reload") not in commands.calls
    assert ("systemctl", "--user", "try-restart", unit) not in commands.calls
    assert ("systemctl", "--user", "enable", "--now", unit) in commands.calls


def test_systemd_user_changed_inactive_unit_is_enabled_and_started_without_restart(tmp_path):
    key = _key(tmp_path)
    unit_dir = tmp_path / "systemd" / "user"
    unit = f"{daemon_supervisor.service_name(key)}.service"
    path = unit_dir / unit
    path.parent.mkdir(parents=True)
    path.write_text("stale inactive unit\n")

    commands = CommandFixture(
        {
            ("loginctl", "show-user", "1234", "-p", "Linger", "--value"): (
                0,
                "yes\n",
                "",
            ),
            ("systemctl", "--user", "show-environment"): (0, "HOME=/tmp\n", ""),
            # systemd's try-restart is successful but leaves an inactive unit stopped;
            # enable --now below is what starts it from the newly loaded definition.
            ("systemctl", "--user", "try-restart", unit): (0, "inactive\n", ""),
            ("systemctl", "--user", "is-active", unit): (0, "active\n", ""),
            ("systemctl", "--user", "is-enabled", unit): (0, "enabled\n", ""),
        }
    )
    backend = daemon_platform.SystemdUserSupervisorBackend(
        unit_dir=unit_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=commands,
    )

    state = backend.install(key)

    assert state.running and state.persisted
    assert ("systemctl", "--user", "daemon-reload") in commands.calls
    try_restart = ("systemctl", "--user", "try-restart", unit)
    enable = ("systemctl", "--user", "enable", "--now", unit)
    assert ("systemctl", "--user", "restart", unit) not in commands.calls
    assert commands.calls.index(try_restart) < commands.calls.index(enable)


def test_systemd_user_changed_active_unit_fails_closed_when_restart_fails(tmp_path):
    key = _key(tmp_path)
    unit_dir = tmp_path / "systemd" / "user"
    unit = f"{daemon_supervisor.service_name(key)}.service"
    path = unit_dir / unit
    path.parent.mkdir(parents=True)
    path.write_text("stale live unit\n")
    commands = CommandFixture(
        {
            ("loginctl", "show-user", "1234", "-p", "Linger", "--value"): (
                0,
                "yes\n",
                "",
            ),
            ("systemctl", "--user", "show-environment"): (0, "HOME=/tmp\n", ""),
            ("systemctl", "--user", "is-active", unit): (0, "active\n", ""),
            ("systemctl", "--user", "try-restart", unit): (1, "", "restart refused"),
        }
    )
    backend = daemon_platform.SystemdUserSupervisorBackend(
        unit_dir=unit_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=commands,
    )

    with pytest.raises(daemon_supervisor.SupervisorError, match="restart refused"):
        backend.install(key)

    assert ("systemctl", "--user", "daemon-reload") in commands.calls
    assert ("systemctl", "--user", "try-restart", unit) in commands.calls
    assert ("systemctl", "--user", "enable", "--now", unit) not in commands.calls


def test_systemd_user_refuses_install_when_linger_is_not_persistent(tmp_path):
    key = _key(tmp_path)
    commands = CommandFixture(
        {
            ("loginctl", "show-user", "1234", "-p", "Linger", "--value"): (0, "no\n", ""),
            ("systemctl", "--user", "show-environment"): (0, "HOME=/tmp\n", ""),
        }
    )
    backend = daemon_platform.SystemdUserSupervisorBackend(
        unit_dir=tmp_path / "systemd",
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=commands,
    )

    with pytest.raises(daemon_supervisor.SupervisorUnavailableError, match="enable-linger"):
        backend.install(key)

    assert not list((tmp_path / "systemd").glob("*.service"))


@pytest.mark.parametrize("enabled_state", ["enabled-runtime", "static"])
def test_systemd_user_status_does_not_claim_reboot_persistence_without_durable_enablement(
    tmp_path, enabled_state
):
    key = _key(tmp_path)
    unit_dir = tmp_path / "systemd"
    unit = f"{daemon_supervisor.service_name(key)}.service"
    (unit_dir / unit).parent.mkdir(parents=True)
    (unit_dir / unit).write_text("installed unit")
    commands = CommandFixture(
        {
            ("loginctl", "show-user", "1234", "-p", "Linger", "--value"): (0, "yes\n", ""),
            ("systemctl", "--user", "show-environment"): (0, "HOME=/tmp\n", ""),
            ("systemctl", "--user", "is-active", unit): (0, "active\n", ""),
            ("systemctl", "--user", "is-enabled", unit): (0, f"{enabled_state}\n", ""),
        }
    )
    backend = daemon_platform.SystemdUserSupervisorBackend(
        unit_dir=unit_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=commands,
    )

    state = backend.status(key)

    assert state.supported and state.installed and state.running
    assert not state.persisted
    assert f"is-enabled={enabled_state}" in state.detail


def test_systemd_user_can_remove_an_installed_unit_after_linger_is_disabled(tmp_path):
    key = _key(tmp_path)
    unit_dir = tmp_path / "systemd"
    unit = f"{daemon_supervisor.service_name(key)}.service"
    (unit_dir / unit).parent.mkdir(parents=True)
    (unit_dir / unit).write_text("old unit")
    commands = CommandFixture(
        {
            ("loginctl", "show-user", "1234", "-p", "Linger", "--value"): (0, "no\n", ""),
            ("systemctl", "--user", "show-environment"): (0, "HOME=/tmp\n", ""),
            ("systemctl", "--user", "is-active", unit): (3, "inactive\n", ""),
            ("systemctl", "--user", "is-enabled", unit): (1, "disabled\n", ""),
        }
    )
    backend = daemon_platform.SystemdUserSupervisorBackend(
        unit_dir=unit_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=commands,
    )

    state = backend.remove(key)

    assert not state.installed
    assert ("systemctl", "--user", "disable", "--now", unit) in commands.calls


def test_launchagent_install_is_per_user_keyed_and_bounded(tmp_path):
    key = _key(tmp_path)
    agent_dir = tmp_path / "Library" / "LaunchAgents"
    label = f"dev.beadhive.{daemon_supervisor.service_name(key)}"
    target = f"gui/1234/{label}"
    commands = CommandFixture(
        {
            ("launchctl", "print", target): (0, "state = running\n", ""),
        }
    )
    backend = daemon_platform.LaunchAgentSupervisorBackend(
        agent_dir=agent_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        shutdown_budget=7.2,
        run=commands,
    )

    state = backend.install(key)
    payload = plistlib.loads((agent_dir / f"{label}.plist").read_bytes())

    assert state.supported and state.installed and state.running and state.persisted
    assert payload["Label"] == label
    assert payload["ProgramArguments"] == ["/opt/beadhive/bin/bh-host-daemon"]
    assert payload["EnvironmentVariables"]["BH_HOME"] == key.bh_home
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["RunAtLoad"] is True
    assert payload["ExitTimeOut"] == 8
    assert payload["StandardOutPath"].startswith(key.bh_home)
    assert payload["StandardErrorPath"].startswith(key.bh_home)
    assert (
        "launchctl",
        "bootstrap",
        "gui/1234",
        str(agent_dir / f"{label}.plist"),
    ) in commands.calls
    assert ("launchctl", "kickstart", "-k", target) in commands.calls


def test_launchagent_install_reloads_a_changed_loaded_plist(tmp_path):
    key = _key(tmp_path)
    agent_dir = tmp_path / "Library" / "LaunchAgents"
    label = f"dev.beadhive.{daemon_supervisor.service_name(key)}"
    path = agent_dir / f"{label}.plist"
    target = f"gui/1234/{label}"
    agent_dir.mkdir(parents=True)
    path.write_bytes(b"stale plist")
    commands = CommandFixture(
        {
            ("launchctl", "print", target): (0, "state = running\n", ""),
        }
    )
    backend = daemon_platform.LaunchAgentSupervisorBackend(
        agent_dir=agent_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=commands,
    )

    state = backend.install(key)

    bootout = ("launchctl", "bootout", "gui/1234", str(path))
    bootstrap = ("launchctl", "bootstrap", "gui/1234", str(path))
    assert state.running and path.read_bytes() == backend._plist(key)
    assert commands.calls.index(bootout) < commands.calls.index(bootstrap)


def test_launchagent_install_is_a_noop_when_loaded_plist_is_identical(tmp_path):
    key = _key(tmp_path)
    agent_dir = tmp_path / "Library" / "LaunchAgents"
    label = f"dev.beadhive.{daemon_supervisor.service_name(key)}"
    target = f"gui/1234/{label}"
    commands = CommandFixture(
        {
            ("launchctl", "print", target): (0, "state = running\n", ""),
        }
    )
    backend = daemon_platform.LaunchAgentSupervisorBackend(
        agent_dir=agent_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=commands,
    )
    agent_dir.mkdir(parents=True)
    backend._path(key).write_bytes(backend._plist(key))

    state = backend.install(key)

    assert state.running and state.persisted
    assert not any(call[1] in {"bootout", "bootstrap", "kickstart"} for call in commands.calls)


def test_launchagent_install_does_not_treat_human_already_text_as_success(tmp_path):
    key = _key(tmp_path)
    agent_dir = tmp_path / "Library" / "LaunchAgents"
    label = f"dev.beadhive.{daemon_supervisor.service_name(key)}"
    path = agent_dir / f"{label}.plist"
    commands = CommandFixture(
        {
            ("launchctl", "bootstrap", "gui/1234", str(path)): (
                5,
                "service already loaded",
                "",
            ),
        }
    )
    backend = daemon_platform.LaunchAgentSupervisorBackend(
        agent_dir=agent_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=commands,
    )

    with pytest.raises(daemon_supervisor.SupervisorError):
        backend.install(key)


def test_launchagent_install_keeps_old_plist_when_loaded_job_cannot_bootout(tmp_path):
    key = _key(tmp_path)
    agent_dir = tmp_path / "Library" / "LaunchAgents"
    label = f"dev.beadhive.{daemon_supervisor.service_name(key)}"
    path = agent_dir / f"{label}.plist"
    target = f"gui/1234/{label}"
    agent_dir.mkdir(parents=True)
    path.write_bytes(b"stale plist")
    commands = CommandFixture(
        {
            ("launchctl", "print", target): (0, "state = running\n", ""),
            ("launchctl", "bootout", "gui/1234", str(path)): (5, "", "permission denied"),
        }
    )
    backend = daemon_platform.LaunchAgentSupervisorBackend(
        agent_dir=agent_dir,
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=commands,
    )

    with pytest.raises(daemon_supervisor.SupervisorError):
        backend.install(key)

    assert path.read_bytes() == b"stale plist"
    assert not any(call[1] == "bootstrap" for call in commands.calls)


def test_launchagent_refuses_a_key_for_another_os_account(tmp_path):
    key = host_daemon.DaemonKey("uid:9999", str(tmp_path.resolve()), "host-1")
    backend = daemon_platform.LaunchAgentSupervisorBackend(
        agent_dir=tmp_path / "agents",
        daemon_binary="/opt/beadhive/bin/bh-host-daemon",
        uid=1234,
        run=CommandFixture(),
    )

    with pytest.raises(daemon_supervisor.SupervisorIdentityError, match="OS account"):
        backend.install(key)


def test_release_matrix_requires_every_ratified_cell_and_never_skips_unavailable():
    required = daemon_platform.REQUIRED_RELEASE_CELLS
    now = datetime(2026, 9, 4, tzinfo=UTC)

    def evidence(platform_name, *, result="passed", observed_at=now, revision="abc123"):
        return {
            cell: {
                "result": result,
                "execution": "real",
                "platform": platform_name,
                "target": f"release-{platform_name}-01",
                "observedAt": observed_at.isoformat(),
                "sourceRevision": revision,
                "detail": f"observed {cell} on the real target",
            }
            for cell in required[platform_name]
        }

    assert set(required["darwin"]) == {
        "crash-restart",
        "bounded-stop",
        "login-logout",
        "sleep-wake",
        "reboot",
        "log-status-recovery",
        "duplicate-refusal",
        "wrong-identity",
    }
    assert set(required["linux"]) == set(required["darwin"])
    assert set(required["container"]) == {
        "restart-policy",
        "health",
        "stop-grace",
        "persistent-bh-home",
        "credentials",
        "enospc",
        "descendant-reaping",
    }

    with pytest.raises(daemon_platform.ReleaseCellError, match="UNAVAILABLE"):
        daemon_platform.validate_release_cells(
            "darwin", {}, available=False, expected_revision="abc123", now=now
        )
    with pytest.raises(daemon_platform.ReleaseCellError, match="missing"):
        partial = evidence("linux")
        partial.pop("reboot")
        daemon_platform.validate_release_cells(
            "linux", partial, expected_revision="abc123", now=now
        )
    with pytest.raises(daemon_platform.ReleaseCellError, match="failed"):
        failed = evidence("container")
        failed["enospc"]["result"] = "failed"
        daemon_platform.validate_release_cells(
            "container",
            failed,
            expected_revision="abc123",
            now=now,
        )


def test_release_cells_require_real_current_target_and_revision_provenance():
    now = datetime(2026, 9, 4, tzinfo=UTC)

    def evidence():
        return {
            cell: {
                "result": "passed",
                "execution": "real",
                "platform": "linux",
                "target": "linux-release-host-01",
                "observedAt": now.isoformat(),
                "sourceRevision": "abc123",
                "detail": f"observed {cell}",
            }
            for cell in daemon_platform.REQUIRED_RELEASE_CELLS["linux"]
        }

    daemon_platform.validate_release_cells("linux", evidence(), expected_revision="abc123", now=now)

    simulated = evidence()
    simulated["crash-restart"]["execution"] = "recording-fixture"
    with pytest.raises(daemon_platform.ReleaseCellError, match="real execution"):
        daemon_platform.validate_release_cells(
            "linux", simulated, expected_revision="abc123", now=now
        )

    stale = evidence()
    stale["reboot"]["observedAt"] = (now - timedelta(days=8)).isoformat()
    with pytest.raises(daemon_platform.ReleaseCellError, match="stale"):
        daemon_platform.validate_release_cells("linux", stale, expected_revision="abc123", now=now)

    wrong_revision = evidence()
    wrong_revision["sleep-wake"]["sourceRevision"] = "old-revision"
    with pytest.raises(daemon_platform.ReleaseCellError, match="revision"):
        daemon_platform.validate_release_cells(
            "linux", wrong_revision, expected_revision="abc123", now=now
        )


def test_release_document_requires_all_three_real_platforms():
    now = datetime(2026, 9, 4, tzinfo=UTC)

    def platform_evidence(platform_name):
        return {
            "available": True,
            "cells": {
                cell: {
                    "result": "passed",
                    "execution": "real",
                    "platform": platform_name,
                    "target": f"release-{platform_name}-01",
                    "observedAt": now.isoformat(),
                    "sourceRevision": "abc123",
                    "detail": f"observed {cell}",
                }
                for cell in daemon_platform.REQUIRED_RELEASE_CELLS[platform_name]
            },
        }

    document = {
        "schemaVersion": 1,
        "platforms": {
            name: platform_evidence(name) for name in daemon_platform.REQUIRED_RELEASE_CELLS
        },
    }
    daemon_platform.validate_release_document(document, expected_revision="abc123", now=now)

    document["platforms"].pop("darwin")
    with pytest.raises(daemon_platform.ReleaseCellError, match="missing platforms: darwin"):
        daemon_platform.validate_release_document(document, expected_revision="abc123", now=now)


def test_compose_daemon_is_the_supervised_main_workload():
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    service = compose["services"]["daemon"]

    assert compose["services"]["bh"]["image"] == "${BH_IMAGE:-beadhive/agent:dev}"
    assert service["image"] == "${BH_DAEMON_IMAGE:-beadhive/core:dev}"
    assert service["command"] == ["bh-host-daemon"]
    assert service["init"] is True
    assert service["restart"] == "unless-stopped"
    assert service["stop_grace_period"] == "305s"
    assert service["healthcheck"]["test"][0] == "CMD"
    assert "http://127.0.0.1:8737/health" in " ".join(service["healthcheck"]["test"])
    assert service["environment"]["BH_HOME"] == compose["services"]["bh"]["environment"]["BH_HOME"]
    assert any(volume.endswith(":/workspace") for volume in service["volumes"])
    assert any(volume.endswith(":/worktrees") for volume in service["volumes"])
    assert not any("bh-harness" in volume for volume in service["volumes"])
    assert not {
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "GH_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
    } & set(service["environment"])
    assert "supervisord" not in str(service).lower()


def test_compose_daemon_environment_reaches_the_real_typed_startup_boundary(tmp_path, monkeypatch):
    from beadhive.config_schema import BeadhiveConfig
    from beadhive.daemon_config import validate_for_listener_startup

    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    daemon_environment = compose["services"]["daemon"]["environment"]
    concrete_environment = {
        "BH_HOME": str(tmp_path / "home"),
        "BH_HQ": str(tmp_path / "home" / "hq"),
        "BH_WORKTREES": str(tmp_path / "worktrees"),
    }
    for name in concrete_environment:
        monkeypatch.delenv(name, raising=False)
    for name in daemon_environment:
        if name in concrete_environment:
            monkeypatch.setenv(name, concrete_environment[name])

    credential = tmp_path / "daemon-credentials.json"
    credential.write_text("{}")
    credential.chmod(0o600)
    settings = BeadhiveConfig.model_validate(
        {
            "host": {
                "daemon": {
                    "enabled": True,
                    "auth": {"credential_file": str(credential)},
                }
            }
        }
    ).host.daemon

    assert validate_for_listener_startup(settings) is settings
    assert "BH_HQ" not in daemon_environment
    assert "BH_HQ" not in compose["services"]["bh"]["environment"]


def test_container_release_contract_rejects_short_stop_grace_and_ephemeral_home():
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    service = compose["services"]["daemon"]

    daemon_platform.validate_container_contract(service, minimum_stop_seconds=30.0)

    short = dict(service, stop_grace_period="5s")
    with pytest.raises(daemon_platform.ContainerContractError, match="stop grace"):
        daemon_platform.validate_container_contract(short, minimum_stop_seconds=30.0)

    ephemeral = dict(service, volumes=[])
    with pytest.raises(daemon_platform.ContainerContractError, match="BH_HOME"):
        daemon_platform.validate_container_contract(ephemeral, minimum_stop_seconds=30.0)


def test_enospc_during_control_record_write_releases_lock_and_partial_file(tmp_path, monkeypatch):
    key = _key(tmp_path)
    paths = host_daemon.DaemonPaths.for_key(key)
    real_fsync = host_daemon.os.fsync
    fsync_calls = 0

    def fail_second_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_fsync(fd)

    monkeypatch.setattr(host_daemon.os, "fsync", fail_second_fsync)

    with pytest.raises(OSError, match="No space left"):
        host_daemon.DaemonSingleton.acquire(key, listener_host="127.0.0.1", listener_port=8737)

    assert not host_daemon._lock_held(paths.lock)
    assert not paths.control.exists()
    assert not list(paths.directory.glob("*.tmp"))
