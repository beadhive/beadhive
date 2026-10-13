"""Fail over from a primary engine to a fallback engine, loudly (validation-broker-adr).

The port's outage rule is "backend down -> fall back to host slots and warn; validation never
blocks on the broker". :class:`FailoverBroker` keeps that rule in one place: it serves every
operation from the primary until the primary is unavailable or raises something that is not a
contract answer, then warns once and serves new work from the fallback for good.

Contract answers pass through unchanged and never trip it: ``GrantRefused`` (cannot fit),
``LeaseLost`` (gone), ``GrantTimeout`` (still queued). A top-level job the primary had not
granted yet moves to the fallback; a granted lease stays with the engine that granted it, and a
nested request stays with the engine that holds its parent lease (a fallback engine knows no
lease of the primary's, and re-queueing a nested request host-wide is the hq-vbha deadlock).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator

from ._errors import BrokerUnavailable, GrantRefused, GrantTimeout, LeaseLost
from ._ports import BrokerPort
from ._types import BrokerEvent, BrokerStatus, Grant, JobHandle, JobSpec, Outcome, Usage

__all__ = ["FailoverBroker"]

_ANSWERS = (GrantRefused, LeaseLost, GrantTimeout)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class FailoverBroker:
    """A :class:`~beadhive_validation_broker.BrokerPort` over a primary and a fallback engine."""

    def __init__(
        self,
        primary: BrokerPort,
        fallback: Callable[[], BrokerPort],
        *,
        warn: Callable[[str], None] = _stderr,
    ) -> None:
        self._primary = primary
        self._make_fallback = fallback
        self._fallback: BrokerPort | None = None
        self._warn = warn
        self._lock = threading.RLock()
        self._tripped: BaseException | None = None
        self._routes: dict[str, BrokerPort] = {}
        self._specs: dict[str, JobSpec] = {}
        self._granted: set[str] = set()
        self._leases: dict[str, BrokerPort] = {}

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._active().name

    @property
    def tripped(self) -> bool:
        return self._tripped is not None

    # ---- the port ----------------------------------------------------------------------

    def submit(self, job: JobSpec) -> JobHandle:
        with self._lock:
            if job.job_id in self._routes:
                return self._routes[job.job_id].submit(job)
            engine = self._leases.get(job.parent_lease or "") if job.parent_lease else None
            engine = engine or self._active()
            try:
                handle = engine.submit(job)
            except _ANSWERS:
                raise
            except Exception as exc:
                if engine is not self._primary or job.parent_lease is not None:
                    raise
                self._trip(exc)
                engine = self._active()
                handle = engine.submit(job)
            self._routes[job.job_id] = engine
            self._specs[job.job_id] = job
            return handle

    def wait_grant(self, handle: JobHandle, timeout: float | None = None) -> Grant:
        engine = self._route(handle)
        try:
            grant = engine.wait_grant(handle, timeout)
        except _ANSWERS:
            raise
        except Exception as exc:
            with self._lock:
                spec = self._specs[handle.job_id]
                if (
                    engine is not self._primary
                    or spec.parent_lease is not None
                    or handle.job_id in self._granted
                ):
                    raise
                self._trip(exc)
                self._best_effort_cancel(handle, "moved to the fallback engine")
                engine = self._active()
                handle = engine.submit(spec)
                self._routes[handle.job_id] = engine
            grant = engine.wait_grant(handle, timeout)
        with self._lock:
            self._granted.add(handle.job_id)
            self._leases[grant.lease_id] = engine
        return grant

    def report(self, handle: JobHandle, outcome: Outcome, usage: Usage) -> None:
        self._route(handle).report(self._native(handle), outcome, usage)

    def cancel(self, handle: JobHandle, reason: str) -> None:
        engine = self._routes.get(handle.job_id)
        if engine is not None:
            engine.cancel(self._native(handle), reason)

    def status(self) -> BrokerStatus:
        engine = self._active()
        try:
            return engine.status()
        except Exception as exc:
            if engine is not self._primary:
                raise
            self._trip(exc)
            return self._active().status()

    def events(self, since: int = 0) -> Iterator[BrokerEvent]:
        """The active engine's events; after a trip the sequence restarts (``seq`` goes back)."""
        engine = self._active()
        try:
            return engine.events(since)
        except Exception as exc:
            if engine is not self._primary:
                raise
            self._trip(exc)
            return self._active().events(0)

    # ---- internals ---------------------------------------------------------------------

    def _active(self) -> BrokerPort:
        with self._lock:
            if self._tripped is None:
                return self._primary
            if self._fallback is None:
                self._fallback = self._make_fallback()
            return self._fallback

    def _trip(self, error: BaseException) -> None:
        with self._lock:
            if self._tripped is not None:
                return
            self._tripped = error
            kind = "unavailable" if isinstance(error, BrokerUnavailable) else "failed"
            self._warn(
                f"⚠ validation broker engine {self._primary.name!r} {kind} ({error}); "
                "falling back to the slots engine for the rest of this process"
            )

    def _route(self, handle: JobHandle) -> BrokerPort:
        with self._lock:
            engine = self._routes.get(handle.job_id)
        if engine is None:
            raise LeaseLost(f"{handle.job_id}: unknown job")
        return engine

    def _native(self, handle: JobHandle) -> JobHandle:
        """The handle the routed engine issued (a moved job was re-submitted there)."""
        engine = self._route(handle)
        if engine is self._primary or handle.backend == engine.name:
            return handle
        return engine.submit(self._specs[handle.job_id])  # idempotent per job id

    def _best_effort_cancel(self, handle: JobHandle, reason: str) -> None:
        try:
            self._primary.cancel(handle, reason)
        except Exception:  # noqa: BLE001 - the primary is already known broken
            pass
