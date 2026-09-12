"""Canonical local identity, control-record, and read-only status boundary for the daemon."""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import platform
import subprocess
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import host as host_identity
from .config_consumer_ports import daemon_settings as config
from .daemon_contract import CONTRACT_VERSION
from .kernel.daemon.contracts.identity import DaemonKey as _DaemonKey
from .kernel.daemon.contracts.identity import DaemonPaths

CONTROL_KEY_BYTES = 32
_compatibility_key_factory = None
_compatibility_status_reader = None


def configure_compatibility_facade(*, key_factory, status_reader) -> None:
    """Preserve characterized host-daemon patch seams without importing that facade."""
    global _compatibility_key_factory, _compatibility_status_reader
    _compatibility_key_factory = key_factory
    _compatibility_status_reader = status_reader


def current_daemon_key() -> DaemonKey:
    if _compatibility_key_factory is not None:
        return _compatibility_key_factory()
    return DaemonKey.current()


class DaemonKey(_DaemonKey):
    """The v1 singleton scope: account, canonical ``BH_HOME``, and stable host id."""

    @classmethod
    def current(cls) -> DaemonKey:
        home = str(config.home().expanduser().resolve(strict=False))
        account = f"uid:{os.getuid()}" if hasattr(os, "getuid") else f"user:{getpass.getuser()}"
        return cls(account_id=account, bh_home=home, host_id=host_identity.host_id())


@dataclass(frozen=True)
class ControlRecord:
    contract: str
    account_id: str
    bh_home: str
    host_id: str
    instance_id: str
    pid: int
    process_start: str
    listener_host: str
    listener_port: int
    started_at: str
    authentication: str = ""

    @classmethod
    def create(
        cls,
        key: DaemonKey,
        *,
        listener_host: str,
        listener_port: int,
        verification_key: bytes,
    ) -> ControlRecord:
        record = cls(
            contract=CONTRACT_VERSION,
            account_id=key.account_id,
            bh_home=key.bh_home,
            host_id=key.host_id,
            instance_id=str(uuid.uuid4()),
            pid=os.getpid(),
            process_start=process_start_token(os.getpid()),
            listener_host=listener_host,
            listener_port=listener_port,
            started_at=datetime.now(UTC).isoformat(),
            authentication="",
        )
        return record.authenticated(verification_key)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ControlRecord:
        record = cls(
            contract=str(value["contract"]),
            account_id=str(value["account_id"]),
            bh_home=str(value["bh_home"]),
            host_id=str(value["host_id"]),
            instance_id=str(value["instance_id"]),
            pid=int(value["pid"]),
            process_start=str(value["process_start"]),
            listener_host=str(value["listener_host"]),
            listener_port=int(value["listener_port"]),
            started_at=str(value["started_at"]),
            authentication=str(value["authentication"]),
        )
        uuid.UUID(record.instance_id)
        if len(record.authentication) != hashlib.sha256().digest_size * 2:
            raise ValueError("control record authentication has the wrong size")
        return record

    def authenticated(self, verification_key: bytes) -> ControlRecord:
        if len(verification_key) != CONTROL_KEY_BYTES:
            raise ValueError("daemon control verification key has the wrong size")
        return replace(
            self,
            authentication=hmac.new(
                verification_key, self._authentication_payload(), hashlib.sha256
            ).hexdigest(),
        )

    def verify(self, verification_key: bytes) -> bool:
        expected = hmac.new(
            verification_key, self._authentication_payload(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(self.authentication, expected)

    def _authentication_payload(self) -> bytes:
        values = asdict(self)
        values.pop("authentication")
        return json.dumps(values, sort_keys=True, separators=(",", ":")).encode()

    def matches(self, key: DaemonKey) -> bool:
        return (
            self.contract == CONTRACT_VERSION
            and self.account_id == key.account_id
            and self.bh_home == key.bh_home
            and self.host_id == key.host_id
        )


@dataclass(frozen=True)
class DaemonStatus:
    state: str
    running: bool
    verified: bool
    detail: str
    key: DaemonKey
    record: ControlRecord | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "running": self.running,
            "verified": self.verified,
            "detail": self.detail,
            "key": asdict(self.key),
            "record": asdict(self.record) if self.record else None,
        }


def process_start_token(pid: int) -> str:
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text()
        fields = raw[raw.rfind(")") + 2 :].split()
        return f"linux:{fields[19]}"
    except (OSError, IndexError):
        pass
    if platform.system() in {"Darwin", "FreeBSD"}:
        try:
            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                return f"ps:{result.stdout.strip()}"
        except (OSError, subprocess.SubprocessError):
            pass
    return "unavailable"


def read_control(
    path: Path, verification_key: bytes | None = None
) -> tuple[ControlRecord | None, str]:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("control record root is not an object")
        record = ControlRecord.from_dict(value)
        if verification_key is not None and not record.verify(verification_key):
            raise ValueError("control record authentication failed")
        return record, ""
    except FileNotFoundError:
        return None, "missing control record"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return None, f"invalid control record: {exc}"


def lock_held(path: Path) -> bool:
    if not path.exists():
        return False
    import fcntl

    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def read_verification_key(path: Path) -> bytes | None:
    try:
        verification_key = path.read_bytes()
    except OSError:
        return None
    return verification_key if len(verification_key) == CONTROL_KEY_BYTES else None


def read_verified_control(paths: DaemonPaths) -> tuple[ControlRecord | None, str]:
    verification_key = read_verification_key(paths.lock)
    if verification_key is not None:
        return read_control(paths.control, verification_key)
    record, problem = read_control(paths.control)
    if record is None:
        return None, problem
    return None, "invalid control record: verification key is missing or invalid"


def daemon_status(key: DaemonKey | None = None) -> DaemonStatus:
    if _compatibility_status_reader is not None:
        return _compatibility_status_reader(key or current_daemon_key())
    expected = key or DaemonKey.current()
    paths = DaemonPaths.for_key(expected)
    held = lock_held(paths.lock)
    record, problem = read_verified_control(paths)
    if not held:
        if record is None and problem == "missing control record":
            return DaemonStatus("stopped", False, False, "no daemon owns the singleton", expected)
        detail = problem if record is None else "a stale control record remains"
        return DaemonStatus(
            "stale", False, False, f"singleton is free but {detail}", expected, record
        )
    if record is None:
        return DaemonStatus("unverified", True, False, problem, expected)
    if not record.matches(expected):
        return DaemonStatus(
            "unverified",
            True,
            False,
            "control record identity does not match this host",
            expected,
            record,
        )
    current_start = process_start_token(record.pid)
    if current_start == "unavailable" or current_start != record.process_start:
        return DaemonStatus(
            "unverified",
            True,
            False,
            "control record PID incarnation could not be verified",
            expected,
            record,
        )
    return DaemonStatus(
        "running",
        True,
        True,
        "singleton, host identity, and process incarnation verified",
        expected,
        record,
    )


__all__ = ("ControlRecord", "DaemonKey", "DaemonPaths", "DaemonStatus", "daemon_status")
