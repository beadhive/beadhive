"""Restricted Unix-socket Git transport; server filesystem stays operator-owned.

Only upload-pack and receive-pack against one fixed repository are available.
This stdlib-only file is copied into server custody during provisioning.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SERVICES = {"git-upload-pack": "upload-pack", "git-receive-pack": "receive-pack"}


@contextmanager
def receive_lock(repository: Path, timeout: float = 120):
    """Serialize authorization through ref commit across broker processes."""
    with (repository / "bh-receive.lock").open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("HQ receive lock deadline exceeded") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def bounded_pack(command, connection, environment, timeout: float = 120):
    """Reap the entire Git/hook process group before releasing the lock."""
    process = subprocess.Popen(
        command, stdin=connection, stdout=connection, env=environment, start_new_session=True
    )
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise TimeoutError("HQ Git transfer deadline exceeded") from None


def serve(repository: Path, endpoint: Path, socket_group: int | None = None) -> None:
    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
        raise ValueError("protected HQ broker requires Linux peer credentials and proc namespaces")
    if not Path("/proc/self/ns/user").exists():
        raise ValueError("protected HQ broker requires Linux proc user namespace visibility")
    repository = repository.resolve()
    if repository.stat().st_uid != os.geteuid():
        raise ValueError("broker must run under operator repository ownership")
    policy = json.loads((repository / "bh-authority-policy.json").read_text())
    if endpoint.exists() or endpoint.is_symlink():
        raise ValueError("refusing to replace existing broker endpoint")
    if endpoint.parent.stat().st_uid != os.geteuid() or endpoint.parent.stat().st_mode & 0o022:
        raise ValueError("broker endpoint directory requires operator custody")

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            name = bytearray()
            while len(name) < 64:
                byte = self.request.recv(1)
                if not byte or byte == b"\n":
                    break
                name.extend(byte)
            service = SERVICES.get(name.decode("ascii", errors="replace"))
            if service is None:
                return
            peer_pid, peer_uid, _peer_gid = struct.unpack(
                "3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            # The client cannot supply this role. A same-UID frame must have a
            # separate user namespace; same-UID processes outside isolation are unsupported.
            role = "frame"
            try:
                if peer_uid == os.geteuid() and os.readlink(
                    f"/proc/{peer_pid}/ns/user"
                ) == os.readlink("/proc/self/ns/user"):
                    role = "operator"
            except OSError:
                pass
            self.request.settimeout(None)
            command = [
                policy["executables"]["git"]["path"],
                "-c",
                f"core.hooksPath={repository / 'hooks'}",
                "-c",
                f"gpg.ssh.program={policy['executables']['ssh_keygen']['path']}",
                service,
                str(repository),
            ]
            environment = {
                "PATH": policy["server_path"],
                "BH_HQ_BROKER_PRINCIPAL": role,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            }
            if service == "receive-pack":
                with receive_lock(repository):
                    bounded_pack(command, self.request, environment)
            else:
                bounded_pack(command, self.request, environment)

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    os.umask(0o022)
    try:
        with Server(str(endpoint), Handler) as server:
            if socket_group is not None:
                os.chown(endpoint, -1, socket_group)
            endpoint.chmod(0o660)
            server.serve_forever(poll_interval=0.2)
    finally:
        endpoint.unlink(missing_ok=True)


def client(endpoint: Path, service: str) -> None:
    if service not in SERVICES:
        raise ValueError("unsupported Git broker service")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(endpoint))
        connection.sendall(service.encode() + b"\n")

        def send():
            try:
                while data := os.read(sys.stdin.fileno(), 65536):
                    connection.sendall(data)
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=send, daemon=True).start()
        while data := connection.recv(65536):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


def main() -> int:
    try:
        if len(sys.argv) >= 4 and sys.argv[1] == "serve":
            serve(
                Path(sys.argv[2]),
                Path(sys.argv[3]),
                int(sys.argv[4]) if len(sys.argv) == 5 else None,
            )
        elif len(sys.argv) == 4 and sys.argv[1] == "client":
            client(Path(sys.argv[2]), sys.argv[3])
        else:
            raise ValueError("expected serve REPOSITORY SOCKET [GROUP] or client SOCKET SERVICE")
        return 0
    except (OSError, ValueError) as exc:
        print(f"HQ Git broker refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
