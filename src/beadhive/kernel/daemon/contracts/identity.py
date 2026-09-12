"""Pure identity and filesystem-path contracts for one host-daemon singleton."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DaemonKey:
    account_id: str
    bh_home: str
    host_id: str

    @property
    def digest(self) -> str:
        raw = "\0".join((self.account_id, self.bh_home, self.host_id)).encode()
        return hashlib.sha256(raw).hexdigest()[:20]


@dataclass(frozen=True)
class DaemonPaths:
    directory: Path
    lock: Path
    control: Path

    @classmethod
    def for_key(cls, key: DaemonKey) -> DaemonPaths:
        directory = Path(key.bh_home) / "run" / "host-daemon"
        stem = f"daemon-{key.digest}"
        return cls(
            directory=directory,
            lock=directory / f"{stem}.lock",
            control=directory / f"{stem}.json",
        )


__all__ = ("DaemonKey", "DaemonPaths")
