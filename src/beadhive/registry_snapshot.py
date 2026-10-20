"""Registry capture (ADR section 2, P2): what is registered AGAINST a store, not stored in it.

There is ONE registry implementation: :meth:`beadhive.engine.Engine.read_registry` returns a
:class:`beadhive.dolt_remote.RegistrySnapshot` of every Dolt-level remote and backup. This module
only WRAPS that snapshot (:class:`RegistryCapture`) with the two facts the engine does not own --
the HQ remote descriptor and ``.beads/config.yaml`` ``sync.remote`` -- and persists it.

``bd bootstrap`` drops and recreates the database, which erases remote and backup registrations;
this capture is what a cut-over rolls back to (``Engine.restore_registry``) and what a post-state
check compares against. A failed read is a refusal (:class:`RegistryUnreadable`), never an empty
registry.

Privacy: :meth:`RegistryCapture.write` persists the ``to_raw()`` payload (real URLs, needed for an
exact restore) at mode 0600; anything printed or put in a ledger uses :meth:`redacted`.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import bd
from .dolt_remote import RegistrySnapshot
from .migration_ledger import digest_of

SCHEMA = "bh.registry-capture/1"


class RegistryUnreadable(RuntimeError):
    """The engine could not read the registry; the registration is UNKNOWN."""


@dataclass(frozen=True)
class RegistryCapture:
    hive: str
    taken_at: str
    snapshot: RegistrySnapshot
    hq_descriptor: Mapping | None
    sync_remote: str
    schema: str = SCHEMA

    def _body(self) -> dict:
        return {
            "schema": self.schema,
            "hive": self.hive,
            "registry": self.snapshot.to_raw(),
            "hq_descriptor": dict(self.hq_descriptor) if self.hq_descriptor else None,
            "sync_remote": self.sync_remote,
        }

    def digest(self) -> str:
        """Over the registry content only (not ``taken_at``): equal registries digest equal."""
        return digest_of(self._body())

    def to_raw(self) -> dict:
        """Private storage only: real URLs."""
        return {**self._body(), "taken_at": self.taken_at, "digest": self.digest()}

    def redacted(self) -> dict:
        """Safe for ledgers, logs and reports: no raw URL."""
        return {
            "schema": self.schema,
            "hive": self.hive,
            "taken_at": self.taken_at,
            "registry": self.snapshot.redacted(),
            "hq_descriptor": _redact_descriptor(self.hq_descriptor),
            "sync_remote": _redact(self.sync_remote),
            "digest": self.digest(),
        }

    @classmethod
    def from_raw(cls, raw: Mapping) -> RegistryCapture:
        if raw.get("schema") != SCHEMA:
            raise ValueError(f"unsupported registry capture schema {raw.get('schema')!r}")
        cap = cls(
            hive=str(raw["hive"]),
            taken_at=str(raw["taken_at"]),
            snapshot=RegistrySnapshot.from_raw(raw["registry"]),
            hq_descriptor=raw.get("hq_descriptor"),
            sync_remote=str(raw.get("sync_remote") or ""),
        )
        if cap.digest() != raw.get("digest"):
            raise ValueError("registry capture digest mismatch (edited or corrupt)")
        return cap

    def write(self, path: Path) -> None:
        """Atomic, fsynced, 0600."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cap-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_raw(), fh, sort_keys=True, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path) -> RegistryCapture:
        return cls.from_raw(json.loads(Path(path).read_text(encoding="utf-8")))

    def diff(self, other: RegistryCapture) -> tuple[str, ...]:
        """Redacted, human-readable differences; empty means equal."""
        out = [
            f"{kind.value} {name!r} {what}"
            for what, keys in (
                ("missing from the other registry", self.snapshot.diff(other.snapshot).removed),
                ("only in the other registry", self.snapshot.diff(other.snapshot).added),
                ("differs", self.snapshot.diff(other.snapshot).changed),
            )
            for kind, name in keys
        ]
        if self.sync_remote != other.sync_remote:
            out.append("sync.remote differs")
        if (self.hq_descriptor or None) != (other.hq_descriptor or None):
            out.append("HQ remote descriptor differs")
        return tuple(out)

    def restore(self, engine, hive_dir: Path, *, extra_env=None):
        """Re-apply the registrations through the engine (registration only; no data moves)."""
        return engine.restore_registry(hive_dir, self.snapshot, extra_env=extra_env)


def _redact(url: str) -> str:
    from .dolt_remote import redact_url

    return redact_url(url) if url else ""


def _redact_descriptor(desc: Mapping | None) -> dict | None:
    if not desc:
        return None
    return {k: (_redact(v) if k == "url" and isinstance(v, str) else v) for k, v in desc.items()}


def _now() -> datetime:
    return datetime.now(UTC)


def capture(
    hive_dir: Path,
    *,
    hive: str,
    engine,
    hq_descriptor: Mapping | None = None,
    clock: Callable[[], datetime] = _now,
    sync_remote: Callable[[Path], str] = bd.sync_remote,
) -> RegistryCapture:
    """Read the registry through ``engine.read_registry`` and wrap it."""
    hive_dir = Path(hive_dir)
    read = engine.read_registry(hive_dir)
    if not read.ok or read.snapshot is None:
        raise RegistryUnreadable(f"registry unreadable: {read.error or 'unknown'}")
    return RegistryCapture(
        hive=hive,
        taken_at=clock().astimezone(UTC).isoformat(),
        snapshot=read.snapshot,
        hq_descriptor=hq_descriptor,
        sync_remote=sync_remote(hive_dir),
    )
