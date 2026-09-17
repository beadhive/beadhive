"""Factory service-profile coverage for the private Frame Bridge upstream."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

import pytest

from beadhive import frame_bridge_factory


def _paths(tmp_path: Path) -> frame_bridge_factory.FactoryServicePaths:
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(mode=0o750)
    state_directory = tmp_path / "state"
    state_directory.mkdir(mode=0o700)
    return frame_bridge_factory.FactoryServicePaths(
        socket_path=runtime_directory / "factory.sock",
        state_directory=state_directory,
        verifier_path=tmp_path / "gateway-verifiers.json",
        credentials_directory=tmp_path / "credentials",
    )


def test_factory_paths_accept_only_systemd_credential_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/factory")

    paths = frame_bridge_factory.FactoryServicePaths.from_environment()

    assert paths.credentials_directory == Path("/run/credentials/factory")
    assert paths.socket_path == Path("/run/beadhive/frame-bridge/factory.sock")
    assert paths.verifier_path == Path("/etc/beadhive/frame-bridge/gateway-verifiers.json")
    assert paths.daemon_bearer_path == Path("/run/credentials/factory/daemon-bearer")


def test_factory_paths_reject_relative_credential_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "credentials")

    with pytest.raises(frame_bridge_factory.FactoryServiceError, match="must be absolute"):
        frame_bridge_factory.FactoryServicePaths.from_environment()


def test_host_epoch_is_private_and_stable_across_restart(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir(mode=0o700)
    epoch_path = state_directory / "host-epoch"

    first = frame_bridge_factory._load_or_create_host_epoch(epoch_path)
    second = frame_bridge_factory._load_or_create_host_epoch(epoch_path)

    assert second == first
    assert str(uuid.UUID(first)) == first
    assert stat.S_IMODE(epoch_path.stat().st_mode) == 0o600


def test_host_epoch_rejects_nonprivate_state(tmp_path: Path) -> None:
    epoch_path = tmp_path / "host-epoch"
    epoch_path.write_text(str(uuid.uuid4()) + "\n", encoding="ascii")
    epoch_path.chmod(0o644)

    with pytest.raises(frame_bridge_factory.FactoryServiceError, match="epoch is incompatible"):
        frame_bridge_factory._load_or_create_host_epoch(epoch_path)


def test_create_application_loads_root_verifier_and_service_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    observed: dict[str, object] = {}
    verifier_set = object()
    daemon_bearer = object()
    application = object()

    def load_verifier(path: Path, *, require_root: bool) -> object:
        observed["verifier"] = (path, require_root)
        return verifier_set

    class Source:
        def __init__(self, *, daemon_bearer: object, instance: object) -> None:
            observed["source"] = self
            observed["source_arguments"] = (daemon_bearer, instance)

    def build(*, config: object, source: object) -> object:
        observed["config"] = config
        observed["built_source"] = source
        return application

    monkeypatch.setattr(frame_bridge_factory, "load_gateway_verifier_set", load_verifier)
    monkeypatch.setattr(
        frame_bridge_factory.daemon_auth, "load_bearer_file", lambda path: daemon_bearer
    )
    monkeypatch.setattr(frame_bridge_factory, "HostDaemonFrameBridgeSource", Source)
    monkeypatch.setattr(frame_bridge_factory, "build_private_frame_bridge_application", build)

    assert frame_bridge_factory.create_application(paths=paths) is application
    assert observed["verifier"] == (paths.verifier_path, True)
    assert observed["source_arguments"][0] is daemon_bearer
    assert observed["built_source"] is observed["source"]
    assert (
        observed["config"].host_epoch == paths.host_epoch_path.read_text(encoding="ascii").strip()
    )


def test_main_binds_only_the_private_unix_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _paths(tmp_path)
    calls: dict[str, object] = {}

    class Listener:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Server:
        def __init__(self, config: object) -> None:
            calls["server_config"] = config

        def run(self, *, sockets: list[object]) -> None:
            calls["sockets"] = sockets

    listener = Listener()

    def remove_socket(path: Path) -> None:
        calls.setdefault("removed", []).append(path)

    def config(application: object, **kwargs: object) -> str:
        calls["application"] = application
        calls["config_kwargs"] = kwargs
        return "private-config"

    monkeypatch.setattr(frame_bridge_factory, "_FACTORY_SOCKET", paths.socket_path)
    monkeypatch.setattr(
        frame_bridge_factory.FactoryServicePaths, "from_environment", classmethod(lambda cls: paths)
    )
    monkeypatch.setattr(frame_bridge_factory, "create_application", lambda *, paths: "private-app")
    monkeypatch.setattr(frame_bridge_factory, "_remove_stale_socket", remove_socket)
    monkeypatch.setattr(frame_bridge_factory, "_bind_factory_socket", lambda path: listener)

    import uvicorn

    monkeypatch.setattr(uvicorn, "Config", config)
    monkeypatch.setattr(uvicorn, "Server", Server)

    frame_bridge_factory.main()

    assert calls["removed"] == [paths.socket_path, paths.socket_path]
    assert calls["application"] == "private-app"
    assert calls["config_kwargs"] == {"access_log": False, "server_header": False}
    assert calls["server_config"] == "private-config"
    assert calls["sockets"] == [listener]
    assert listener.closed is True


def test_factory_socket_has_group_only_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_directory = Path("/tmp") / f"bh-{uuid.uuid4().hex[:8]}"
    runtime_directory.mkdir(mode=0o750)
    socket_path = runtime_directory / "factory.sock"
    monkeypatch.setattr(frame_bridge_factory, "_FACTORY_SOCKET", socket_path)

    listener = frame_bridge_factory._bind_factory_socket(socket_path)
    try:
        info = socket_path.stat()
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o660
        assert info.st_uid == os.geteuid()
        assert info.st_gid == os.getegid()
    finally:
        listener.close()
        frame_bridge_factory._remove_stale_socket(socket_path)
        runtime_directory.rmdir()


def test_socket_cleanup_refuses_non_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_path = tmp_path / "factory.sock"
    monkeypatch.setattr(frame_bridge_factory, "_FACTORY_SOCKET", socket_path)
    socket_path.write_text("not a socket", encoding="utf-8")

    with pytest.raises(frame_bridge_factory.FactoryServiceError, match="socket is incompatible"):
        frame_bridge_factory._remove_stale_socket(socket_path)


def test_factory_systemd_profile_keeps_socket_private_and_has_no_tcp_listener() -> None:
    unit = (
        Path(__file__).parents[1] / "deploy/systemd/beadhive-frame-bridge@factory.service"
    ).read_text(encoding="utf-8")

    assert "User=beadhive-frame-bridge" in unit
    assert "Group=beadhive-gateway" in unit
    assert "RuntimeDirectory=beadhive/frame-bridge" in unit
    assert "RuntimeDirectoryMode=0750" in unit
    assert "UMask=0007" in unit
    assert "LoadCredential=daemon-bearer:" in unit
    assert "Restart=on-failure" in unit
    assert "python -m beadhive.frame_bridge_factory" in unit
    assert "8787" not in unit
    assert "ListenStream" not in unit
    assert "Environment=" not in unit
    manifest = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'beadhive-frame-bridge = "beadhive.bootstrap.frame_bridge:main"' in manifest
