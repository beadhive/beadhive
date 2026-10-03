"""Neutral five-field hive lease contract, shared by legacy and selected HQ adapters."""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass

LEASE_REF_ROOT = "refs/bh/lease/"
_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"


def lease_ref(prefix: str) -> str:
    """The HQ ref carrying `prefix`'s host lease. Raises on an empty prefix rather than
    computing ``refs/bh/lease/`` — a directory-shaped ref that would collide with every
    hive's."""
    if not prefix:
        raise ValueError("a hive prefix is required to name a host-lease ref")
    return LEASE_REF_ROOT + prefix


def now_stamp(at: float | None = None) -> str:
    """`at` (epoch seconds; default: now) as an ISO-8601 UTC stamp."""
    return time.strftime(_TIMESTAMP_FMT, time.gmtime(at if at is not None else time.time()))


def _parse_stamp(text: str) -> float:
    """An ISO-8601 UTC stamp back to epoch seconds. A malformed/empty stamp reads as 0.0 —
    i.e. *long expired*, which is the fail-closed direction for an expiry comparison (a
    corrupt lease must not read as an infinitely valid one).

    ``calendar.timegm`` is the documented inverse of ``time.gmtime``, which is what
    :func:`now_stamp` writes these stamps with — so parse and format are the same clock by
    construction, with no local-time or DST notion anywhere in the round trip.

    It replaces ``time.mktime(...) - time.timezone`` (bh-nf902), a well-known DST-broken
    idiom: ``mktime`` reads the struct as LOCAL time and applies whatever offset is in force
    (PDT, UTC-7), while ``time.timezone`` is always the STANDARD offset (PST, UTC-8). The two
    disagree by exactly one hour whenever DST is active, so every stamp parsed an hour early.
    A 30-minute lease — ``DEFAULT_TTL``, and the ``transient`` baseline a laptop gets — was
    therefore born expired, locking that host out of every write to an adopted hive. An
    ``executor``'s 4x tenure merely lost an hour of runway, silently, which is why the
    always-on host never surfaced it."""
    try:
        return calendar.timegm(time.strptime(text, _TIMESTAMP_FMT))
    except (ValueError, TypeError):
        return 0.0


@dataclass(frozen=True)
class HostLease:
    """One host lease record. Immutable — every operation returns a NEW record."""

    host_id: str
    label: str
    epoch: int
    adopted_at: str
    expires_at: str

    @property
    def is_tombstone(self) -> bool:
        """A released lease: same five fields, empty ``host_id``. Deliberately a record and
        not a deleted ref — see :func:`release`."""
        return not self.host_id

    def is_expired(self, at: float | None = None) -> bool:
        """Whether the lease's TTL has elapsed. A tombstone is always expired."""
        if self.is_tombstone:
            return True
        clock = at if at is not None else time.time()
        return _parse_stamp(self.expires_at) <= clock

    def held_by(self, host_id: str, at: float | None = None) -> bool:
        """Whether `host_id` holds this lease AND it is still live."""
        return bool(host_id) and self.host_id == host_id and not self.is_expired(at)

    def describe(self) -> str:
        """One line naming the holder and its expiry — the text a refusal shows an operator,
        who otherwise cannot tell *what to do* about being blocked."""
        if self.is_tombstone:
            return f"released (no holder; epoch {self.epoch})"
        return (
            f"{self.label or '?'} ({self.host_id}), epoch {self.epoch}, expires {self.expires_at}"
        )

    def to_record(self) -> dict:
        return {
            "host_id": self.host_id,
            "label": self.label,
            "epoch": self.epoch,
            "adopted_at": self.adopted_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_record(cls, record: dict) -> HostLease:
        """Build from a decoded blob. Raises ``ValueError`` on a record missing the shape —
        loud, never a best-effort partial read (hosts.py's convention)."""
        missing = [
            k for k in ("host_id", "label", "epoch", "adopted_at", "expires_at") if k not in record
        ]
        if missing:
            raise ValueError(f"host-lease record missing field(s): {', '.join(missing)}")
        return cls(
            host_id=str(record["host_id"]),
            label=str(record["label"]),
            epoch=int(record["epoch"]),
            adopted_at=str(record["adopted_at"]),
            expires_at=str(record["expires_at"]),
        )
