"""Bounded, verified SQL transport and host-local fnox credential retrieval.

No connection is made at construction. Each operation owns a fresh connection; the
driver is deliberately barred from reconnecting a transaction after a lost response.
"""

from __future__ import annotations

import importlib.metadata
import ipaddress
import os
import re
import selectors
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol


class SqlTransportError(ValueError):
    """A credential, transport, or SQL operation failed without revealing secret bytes."""


class SecretBroker(Protocol):
    def get(self, reference: dict, *, deadline: float) -> str: ...


def _resolve_ipv4(host: str, *, deadline: float) -> str:
    """Resolve before credential retrieval with a killable, bounded Python resolver.

    PyMySQL's socket.create_connection calls getaddrinfo without a DNS deadline.
    Resolve once in a fixed local subprocess, then connect directly to its numeric
    result while retaining the original hostname for TLS SNI/certificate checks.
    """
    try:
        return str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError:
        pass
    if not sys.executable or time.monotonic() >= deadline:
        raise SqlTransportError("bounded SQL endpoint resolution unavailable")
    try:
        status, output = _capture_limited(
            [
                sys.executable,
                "-I",
                "-c",
                "import socket,sys\n"
                "addresses=socket.getaddrinfo(sys.argv[1],0,family=socket.AF_INET,"
                "type=socket.SOCK_STREAM)\n"
                "print(addresses[0][4][0])\n",
                host,
            ],
            deadline=deadline,
            environment={"PATH": "/run/current-system/sw/bin:/usr/bin:/bin"},
            limit=4096,
        )
        if status or not output:
            raise ValueError()
        address = output.splitlines()[0].split()[0].decode("ascii")
        return str(ipaddress.IPv4Address(address))
    except (OSError, TimeoutError, ValueError, UnicodeError, IndexError):
        raise SqlTransportError("bounded SQL endpoint resolution unavailable") from None


def _capture_limited(args, *, deadline, environment, limit):
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
    )
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError()
                chunk = process.stdout.read1(min(4096, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise SqlTransportError("fnox broker output exceeded bound")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        return process.wait(timeout=remaining), bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _fnox_binary() -> str | None:
    """Resolve fixed administrator paths, then a protected macOS mise installation.

    Never invoke mise or its shims: they can select repository-controlled tools.
    The user installation uses the account database, not HOME or mise overrides.
    """
    directories = "/run/current-system/sw/bin:/usr/local/bin:/usr/bin"
    if sys.platform == "darwin":
        directories += ":/opt/homebrew/bin"
    binary = shutil.which("fnox", path=directories)
    if binary or sys.platform != "darwin":
        return binary
    import pwd

    try:
        uid = os.getuid()
        home = Path(pwd.getpwuid(uid).pw_dir)
        if not home.is_absolute():
            return None
        candidate = home / ".local/share/mise/installs/fnox/1.36.0/fnox"
        # Protect every component below the account home, including the home itself.
        # Exact-version directories and the binary must not be alias/escape symlinks.
        directories = [home]
        for component in candidate.relative_to(home).parts[:-1]:
            directories.append(directories[-1] / component)
        for directory in directories:
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, uid)
                or info.st_mode & 0o022
            ):
                return None
        info = candidate.lstat()
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_uid in (0, uid)
            and not info.st_mode & 0o022
            and os.access(candidate, os.X_OK)
        ):
            return str(candidate)
    except (OSError, KeyError):
        pass
    return None


class FnoxBroker:
    """Fixed fnox 1.36.0 noninteractive get contract; no configured command."""

    def __init__(self, binary: str | None = None):
        # Injection is solely for a controlled test fixture. Production only resolves
        # the named binary from fixed administrator or protected user directories.
        self.binary = binary or _fnox_binary()

    def get(self, reference: dict, *, deadline: float) -> str:
        if not self.binary:
            raise SqlTransportError(
                "fnox credential broker unavailable; install fnox 1.36.0 in an "
                "administrator binary directory or, on macOS, run mise install fnox@1.36.0 "
                "in the default account-home installation with no symlinks or "
                "group/other-writable components"
            )
        path, profile, key = (
            reference.get("config_path"),
            reference.get("profile"),
            reference.get("key"),
        )
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or not isinstance(profile, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", profile)
            or not isinstance(key, str)
            or not key.isidentifier()
        ):
            raise SqlTransportError("invalid host credential reference")
        args = [
            self.binary,
            "--config",
            path,
            "--profile",
            profile,
            "--if-missing",
            "error",
            "--no-color",
            "--no-daemon",
            "--no-defaults",
            "--non-interactive",
            "get",
            key,
        ]
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            environment = {
                "HOME": str(Path.home()),
                "PATH": "/run/current-system/sw/bin:/usr/bin:/bin",
            }
            version_status, version = _capture_limited(
                [self.binary, "--version"],
                deadline=deadline,
                environment=environment,
                limit=128,
            )
            if version_status or not re.fullmatch(rb"fnox 1\.36\.0\r?\n?", version):
                raise SqlTransportError("unsupported fnox broker version")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            status, output = _capture_limited(
                args,
                deadline=deadline,
                environment=environment,
                limit=4096,
            )
            if status or not output:
                raise SqlTransportError("fnox credential unavailable")
            return output.decode("utf-8").removesuffix("\n")
        except (OSError, TimeoutError, UnicodeError, subprocess.TimeoutExpired):
            raise SqlTransportError("fnox credential unavailable") from None


def _strict_driver():
    try:
        if importlib.metadata.version("PyMySQL") != "1.1.2":
            raise SqlTransportError("unsupported PyMySQL distribution")
        from pymysql.connections import Connection
        from pymysql.constants import CLIENT
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise SqlTransportError("pinned SQL driver unavailable") from None

    class StrictConnection(Connection):
        def __init__(self, *, operation_deadline, resolved_ip=None, tls_mode="required", **kwargs):
            if tls_mode not in ("required", "disabled"):
                raise SqlTransportError("invalid SQL TLS mode")
            self._tls_mode = tls_mode
            self._attempts = 0
            self._deadline = operation_deadline
            self._expired = False
            self._resolved_ip = resolved_ip
            if kwargs.get("unix_socket") or kwargs.get("local_infile"):
                raise SqlTransportError("unsafe SQL transport options")
            remaining = operation_deadline - time.monotonic()
            if remaining <= 0:
                raise SqlTransportError("SQL operation deadline exceeded")
            self._abort_timer = threading.Timer(remaining, self._abort_at_deadline)
            self._abort_timer.daemon = True
            self._abort_timer.start()
            try:
                super().__init__(**kwargs)
                self._check_deadline()
            except BaseException:
                self._abort_timer.cancel()
                for owned in (getattr(self, "_rfile", None), getattr(self, "_sock", None)):
                    if owned is not None:
                        try:
                            owned.close()
                        except OSError:
                            pass
                raise

        def _abort_at_deadline(self):
            self._expired = True
            sock = getattr(self, "_sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        def _check_deadline(self):
            if self._expired or time.monotonic() >= self._deadline:
                raise SqlTransportError("SQL operation deadline exceeded")

        def _read_bytes(self, num_bytes):
            self._check_deadline()
            self._read_timeout = min(self._read_timeout, self._deadline - time.monotonic())
            data = super()._read_bytes(num_bytes)
            self._check_deadline()
            return data

        def _write_bytes(self, data):
            self._check_deadline()
            self._write_timeout = min(self._write_timeout, self._deadline - time.monotonic())
            result = super()._write_bytes(data)
            self._check_deadline()
            return result

        def close(self):
            self._abort_timer.cancel()
            return super().close()

        def connect(self, sock=None):
            if sock is not None or self._attempts:
                raise SqlTransportError("SQL transaction reconnect refused")
            self._check_deadline()
            self._attempts += 1
            if not isinstance(self._resolved_ip, str):
                raise SqlTransportError("bounded SQL endpoint resolution required")
            owned = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock = owned
            try:
                owned.settimeout(min(self.connect_timeout, self._deadline - time.monotonic()))
                owned.connect((self._resolved_ip, self.port))
                owned.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                owned.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                owned.settimeout(None)
                self.host_info = f"socket {self.host}:{self.port}"
                result = super().connect(sock=owned)
            except BaseException:
                owned.close()
                raise
            self._check_deadline()
            return result

        def ping(self, reconnect=False):
            if reconnect:
                raise SqlTransportError("SQL transaction reconnect refused")
            return super().ping(reconnect=False)

        def _request_authentication(self):
            if self._tls_mode == "disabled":
                if self.ssl or self.client_flag & CLIENT.SSL:
                    raise SqlTransportError("plaintext SQL binding cannot negotiate TLS")
                return super()._request_authentication()
            ctx = getattr(self, "ctx", None)
            if (
                not self.ssl
                or not isinstance(ctx, ssl.SSLContext)
                or ctx.verify_mode != ssl.CERT_REQUIRED
                or not ctx.check_hostname
                or ctx.minimum_version < ssl.TLSVersion.TLSv1_2
                or not self.server_capabilities & CLIENT.SSL
            ):
                raise SqlTransportError("mandatory verified TLS unavailable before authentication")
            return super()._request_authentication()

    return StrictConnection


def connect(settings: dict, broker: SecretBroker, *, deadline: float):
    """Open one TCP connection under its explicit policy; never retry or downgrade."""
    from .modules.config.contracts import HqSqlConnection

    try:
        settings = HqSqlConnection.model_validate(settings).model_dump()
    except ValueError:
        raise SqlTransportError("invalid SQL connection binding") from None
    tls_mode = settings["tls_mode"]
    required = ("host", "database", "user", "credential")
    if tls_mode == "required":
        required += ("server_name", "ca_file")
    if any(not settings.get(key) for key in required) or not all(
        settings["credential"].get(key) for key in ("config_path", "profile", "key")
    ):
        raise SqlTransportError("incomplete SQL connection binding")
    try:
        ctx = None
        if tls_mode == "required":
            ctx = ssl.create_default_context(cafile=settings["ca_file"])
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if not ctx.check_hostname or ctx.verify_mode != ssl.CERT_REQUIRED:
                raise SqlTransportError("verified TLS context unavailable")
        resolved_ip = _resolve_ipv4(settings["host"], deadline=deadline)
        password = broker.get(settings["credential"], deadline=deadline)
        if not password:
            raise SqlTransportError("empty SQL credential")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SqlTransportError("SQL operation deadline exceeded")
        Connection = _strict_driver()
        return Connection(
            host=settings["host"],
            port=settings["port"],
            database=settings["database"],
            user=settings["user"],
            password=password,
            ssl=ctx,
            tls_mode=tls_mode,
            autocommit=False,
            local_infile=False,
            connect_timeout=min(settings["connect_timeout"], remaining),
            read_timeout=min(settings["read_timeout"], remaining),
            write_timeout=min(settings["write_timeout"], remaining),
            charset="utf8mb4",
            operation_deadline=deadline,
            resolved_ip=resolved_ip,
        )
    except SqlTransportError:
        raise
    except Exception:
        # PyMySQL can include a user or password in raw exception strings.
        raise SqlTransportError("verified SQL connection unavailable") from None
