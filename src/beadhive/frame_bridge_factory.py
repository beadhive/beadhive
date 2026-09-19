"""Factory-managed launcher for the private Frame Bridge upstream.

The aggregate Gateway reaches this process only over the Factory-owned Unix socket.
The public Development Frame Bridge remains separately owned by
``frame_bridge_runtime`` and continues to bind ``127.0.0.1:8787``.
"""

from __future__ import annotations

import os
import socket
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import daemon_auth
from .frame_bridge_upstream import (
    GatewayVerifierStore,
    HostDaemonFrameBridgeSource,
    PrivateFrameBridgeConfig,
    RegisteredInstance,
    build_private_frame_bridge_application,
    load_gateway_verifier_set,
)

_FACTORY_SOCKET = Path("/run/beadhive/frame-bridge/factory.sock")
_FACTORY_STATE_DIRECTORY = Path("/var/lib/beadhive/frame-bridge")
_FACTORY_VERIFIER_PATH = Path("/etc/beadhive/frame-bridge/gateway-verifiers.json")
_DAEMON_CREDENTIAL_NAME = "daemon-bearer"
_HOST_EPOCH_FILE = "host-epoch"
_SOCKET_MODE = 0o660
_SOCKET_BACKLOG = 128


class FactoryServiceError(RuntimeError):
    """The service-manager-controlled Factory profile is unsafe to start."""


@dataclass(frozen=True)
class FactoryServicePaths:
    """Fixed private paths, injectable only for service-profile tests."""

    socket_path: Path = _FACTORY_SOCKET
    state_directory: Path = _FACTORY_STATE_DIRECTORY
    verifier_path: Path = _FACTORY_VERIFIER_PATH
    credentials_directory: Path = Path("/run/credentials")

    @classmethod
    def from_environment(cls) -> FactoryServicePaths:
        """Accept only systemd's credential-directory location from the environment."""

        credential_directory = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/run/credentials"))
        if not credential_directory.is_absolute():
            raise FactoryServiceError("Factory credential directory must be absolute")
        return cls(credentials_directory=credential_directory)

    @property
    def daemon_bearer_path(self) -> Path:
        return self.credentials_directory / _DAEMON_CREDENTIAL_NAME

    @property
    def host_epoch_path(self) -> Path:
        return self.state_directory / _HOST_EPOCH_FILE


def _load_or_create_host_epoch(path: Path) -> str:
    """Keep registration stable across a managed restart without trusting a loose file."""

    if not path.is_absolute() or path.name != _HOST_EPOCH_FILE:
        raise FactoryServiceError("Factory host epoch path is incompatible")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except FileExistsError:
            return _load_or_create_host_epoch(path)
        except OSError as exc:
            raise FactoryServiceError("Factory host epoch is unavailable") from exc
        value = str(uuid.uuid4())
        try:
            if os.write(descriptor, (value + "\n").encode("ascii")) != len(value) + 1:
                raise FactoryServiceError("Factory host epoch is unavailable")
        finally:
            os.close(descriptor)
        return value
    except OSError as exc:
        raise FactoryServiceError("Factory host epoch is unavailable") from exc

    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
    ):
        raise FactoryServiceError("Factory host epoch is incompatible")
    try:
        value = path.read_text(encoding="ascii")
        if not value.endswith("\n") or value.count("\n") != 1:
            raise ValueError
        epoch = str(uuid.UUID(value[:-1]))
    except (OSError, UnicodeError, ValueError) as exc:
        raise FactoryServiceError("Factory host epoch is incompatible") from exc
    return epoch


def _remove_stale_socket(path: Path) -> None:
    """Remove only a socket owned by this service before the replacement binds it."""

    if not path.is_absolute() or path != _FACTORY_SOCKET:
        raise FactoryServiceError("Factory socket path is incompatible")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FactoryServiceError("Factory socket is unavailable") from exc
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_gid != os.getegid()
    ):
        raise FactoryServiceError("Factory socket is incompatible")
    try:
        path.unlink()
    except OSError as exc:
        raise FactoryServiceError("Factory socket is unavailable") from exc


def _bind_factory_socket(path: Path) -> socket.socket:
    """Bind the one protected AF_UNIX listener without Uvicorn broadening its mode."""

    if not path.is_absolute() or path != _FACTORY_SOCKET:
        raise FactoryServiceError("Factory socket path is incompatible")
    try:
        parent = path.parent.stat()
    except OSError as exc:
        raise FactoryServiceError("Factory socket directory is unavailable") from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_IMODE(parent.st_mode) != 0o750
        or parent.st_uid != os.geteuid()
        or parent.st_gid != os.getegid()
    ):
        raise FactoryServiceError("Factory socket directory is incompatible")

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound = False
    try:
        listener.bind(str(path))
        bound = True
        os.chmod(path, _SOCKET_MODE)
        listener.listen(_SOCKET_BACKLOG)
        listener.setblocking(False)
    except OSError as exc:
        listener.close()
        if bound:
            _remove_stale_socket(path)
        raise FactoryServiceError("Factory socket is unavailable") from exc
    return listener


def create_application(*, paths: FactoryServicePaths | None = None):
    """Construct the private app after every authority is safe to consume."""

    paths = paths or FactoryServicePaths.from_environment()
    verifier_store = GatewayVerifierStore(
        load_gateway_verifier_set(paths.verifier_path, require_root=True)
    )
    daemon_bearer = daemon_auth.load_bearer_file(paths.daemon_bearer_path)
    instance = RegisteredInstance()
    source = HostDaemonFrameBridgeSource(daemon_bearer=daemon_bearer, instance=instance)
    return build_private_frame_bridge_application(
        config=PrivateFrameBridgeConfig(
            verifier_store=verifier_store,
            instance=instance,
            host_epoch=_load_or_create_host_epoch(paths.host_epoch_path),
        ),
        source=source,
    )


def main() -> None:
    """Start the Factory profile with a protected AF_UNIX listener and no TCP listener."""

    import uvicorn

    paths = FactoryServicePaths.from_environment()
    _remove_stale_socket(paths.socket_path)
    listener = _bind_factory_socket(paths.socket_path)
    try:
        config = uvicorn.Config(
            create_application(paths=paths),
            access_log=False,
            server_header=False,
        )
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        listener.close()
        _remove_stale_socket(paths.socket_path)


if __name__ == "__main__":
    main()
