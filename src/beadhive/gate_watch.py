"""Observe review gates resolving and hand each one to the runtime (bh-6z2cj).

`docs/design/gas-city-runtime-seam-adr.md` Decision 3: the loop's `coordination.gate_check` pass
is where gate resolution is noticed, and `Runtime.on_gate_resolved(GateResolution)` is how a
runtime that hosts long-lived sessions learns about the **bounce and feedback points**. This
module is that caller. It owns no lifecycle decision: a bounced bead is re-scheduled by the
loop's own `resume-changes-requested` row, never by the runtime this notifies.

`bd gate check` reports only counts, and a human review gate is never auto-resolved by it (a
person runs `bh work approve` / `bounce`), so the watcher reads the gate list itself and
reports every review gate that CLOSED at or after the watcher started and has not yet been
delivered. Nothing is persisted: the delivered set is process memory, which gives the ADR's
at-least-once semantics (a restarted loop may deliver a recently closed gate again; runtimes
must be idempotent). A hook that raises is logged and retried on later passes up to
:data:`MAX_ATTEMPTS`, then dropped with an error log so a permanently broken hook cannot wedge
or flood the loop.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

from . import log
from .gate_resolution import GateResolution

_LOG = log.get_logger(__name__)

#: A raising hook is retried on this many passes in total before the resolution is dropped.
MAX_ATTEMPTS = 3

# A convention review gate's reason marker (`bh:review <sha>`, legacy bare `review <sha>`); the
# same shape `work_logic.is_review_gate_desc` selects, kept local so this module stays a leaf.
_REVIEW_REASON = re.compile(r"reason: (?:bh:)?review [0-9a-f]{7,40}\b")
_BLOCKING = re.compile(r"blocking ([A-Za-z0-9][A-Za-z0-9._-]*)")
_BATCH = re.compile(r"batch \S+: ([^\n]+)")
_BEAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def beads_named(description: str) -> list[str]:
    """The bead id(s) a review gate guards: the members of a batch gate's `batch <g>: a, b`
    reason when present, else the single `blocking <bead>` target."""
    batch = _BATCH.search(description)
    if batch:
        members = [m for m in (t.strip(" ,") for t in batch.group(1).split(",")) if m]
        found = [m for m in members if _BEAD_ID.fullmatch(m)]
        if found:
            return found
    single = _BLOCKING.search(description)
    return [single.group(1)] if single else []


def verdict_of(row: dict, bead_labels: Callable[[str], list[str]], bead: str) -> str:
    """`approved` or `changes_requested` for one closed review gate. The gate's own close reason
    is authoritative (`bh work bounce` writes `changes requested by …`, `approve` writes
    `approved by …`); a gate resolved some other way falls back to the bead's `review:` label."""
    reason = str(row.get("close_reason") or "").strip().lower()
    if reason.startswith("changes requested") or reason.startswith("changes-requested"):
        return "changes_requested"
    if reason.startswith("approved"):
        return "approved"
    labels = bead_labels(bead)
    return "changes_requested" if "review:changes-requested" in labels else "approved"


class GateWatcher:
    """Turns "a review gate closed" into `GateResolution`s, each delivered until it sticks."""

    def __init__(
        self,
        read_gates: Callable[[], list[dict]],
        read_labels: Callable[[str], list[str]],
        *,
        since: str | None = None,
    ) -> None:
        self._read_gates = read_gates
        self._read_labels = read_labels
        self._since = since if since is not None else _now()
        self._delivered: set[tuple[str, str]] = set()
        self._attempts: dict[tuple[str, str], int] = {}

    def pending(self) -> list[GateResolution]:
        """Every resolved review gate (one per guarded bead) not yet delivered or dropped."""
        out: list[GateResolution] = []
        for row in self._read_gates():
            description = str(row.get("description") or "")
            if str(row.get("status") or "") != "closed" or not _REVIEW_REASON.search(
                description.lower()
            ):
                continue
            if str(row.get("closed_at") or "") < self._since:
                continue
            gate_id = str(row.get("id") or "")
            for bead in beads_named(description):
                if (gate_id, bead) in self._delivered:
                    continue
                verdict = verdict_of(row, self._read_labels, bead)
                out.append(
                    GateResolution(
                        gate_id=gate_id,
                        bead_id=bead,
                        verdict=verdict,  # type: ignore[arg-type]
                        feedback_ref=gate_id if verdict == "changes_requested" else None,
                    )
                )
        return out

    def deliver(self, hook: Callable[[GateResolution], None]) -> int:
        """Call `hook` once for each pending resolution; return how many it accepted. A raising
        hook is logged and left pending for the next pass (bounded by :data:`MAX_ATTEMPTS`)."""
        accepted = 0
        for resolution in self.pending():
            key = (resolution.gate_id, resolution.bead_id)
            try:
                hook(resolution)
            except Exception as exc:  # noqa: BLE001 - a runtime hook must never break the pass
                n = self._attempts[key] = self._attempts.get(key, 0) + 1
                final = n >= MAX_ATTEMPTS
                _LOG.warning(
                    "gate_resolved_hook_failed",
                    gate=resolution.gate_id,
                    bead=resolution.bead_id,
                    attempt=n,
                    dropped=final,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if final:
                    self._delivered.add(key)
                continue
            self._delivered.add(key)
            accepted += 1
        return accepted
