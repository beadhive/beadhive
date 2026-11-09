"""Waiting for the reporter: ``intake:needs-info`` and wake-on-reply (bh-61xkc). Standard library
only.

The additive half of ``bh-elkv`` that the intake ADR defers to it (D3). Triage asks a bound
report's reporter for the facts it is missing: the question is drafted as a supervised ``reply``
outbox record (an operator still approves and sends it) and the report moves from
``intake:untriaged`` to ``intake:needs-info``, so it leaves the untriaged queue instead of looking
like a fresh report. A reply from a human on the issue thread wakes it: ``intake:untriaged``
again, with the reply left unread (new evidence) for the next triage pass. The report keeps its
lifetime ``intake-hold:`` gate throughout, so neither state ever makes it ready or claimable.

The rules, in short:

* **Bound reports only.** A born-native report (``bh report``, ``bh escalate``) has no external
  thread to reply on and no ``intake-hold:`` gate, so asking it is refused.
* **Ask only what is not already answered.** Asking while the thread holds external evidence
  triage has not read is refused (:attr:`RefusalReason.UNREAD_EVIDENCE`): read it, record the
  read with ``thread considered``, then ask. This is also what makes "a reply wakes it" exact:
  any new evidence after the ask is a reply that arrived after the question.
* **What wakes.** Exactly the thread's derived ``new_evidence``: a live external comment that is
  new or revised since triage's last read, or one deleted since. Own messages (this install's
  replies, by receipt or marker) and peer messages are context, never evidence, so posting the
  question, or editing it, never wakes the report.
* **Never through the watermark.** Waking never marks anything considered; the reply stays new
  evidence for triage. Marking considered never wakes or touches ``intake``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .errors import IntakeRefused, RefusalReason
from .models import BeadView
from .vocabulary import is_awaiting_triage, is_needs_info

#: The ``intake`` value of a bound report waiting for its reporter.
NEEDS_INFO = "needs-info"
#: The ``intake`` value a woken report returns to.
UNTRIAGED = "untriaged"


@dataclass(frozen=True)
class NeedsInfoRequest:
    """Ask ``report``'s reporter for missing facts: ``text`` is the question, drafted as a reply
    (posted only after an operator approves and sends it)."""

    report: str
    text: str
    actor: str


@dataclass(frozen=True)
class NeedsInfoOutcome:
    """What asking did. ``outbox_id`` is the drafted reply (the existing one when the same
    question was already drafted against the same thread ``seq``); ``state_written`` is False when
    the report was already waiting (another question was drafted, the state was kept); ``held``
    is True when a leak or sanitizer finding holds the draft for the operator."""

    report: str
    outbox_id: str
    state: str = NEEDS_INFO
    state_written: bool = True
    held: bool = False
    detail: str = ""


@dataclass(frozen=True)
class WakeRequest:
    """Wake ``report`` if its reporter replied since triage last read the thread."""

    report: str
    actor: str


def wake_reopen_text() -> str:
    """The reporter-visible reply a reopen rides on when a reporter answers on a closed issue
    (``bh-ih484``): a template, never the reporter's words."""
    return (
        "Thanks for the follow-up. This issue was closed, so we are reopening it to take another "
        "look at what you added."
    )


@dataclass(frozen=True)
class WakeOutcome:
    """What one wake did. ``woken`` is True when the report moved back to ``intake:untriaged``;
    ``evidence`` names the comments that woke it (or would have); ``detail`` says why it was left
    waiting; ``error`` is a sweep's per-report failure (the sweep moved on); ``reopen`` is the
    outbox record drafted (never sent) to reopen the report's issue, when the reporter answered on
    one that is closed (``bh-ih484``)."""

    report: str
    woken: bool = False
    evidence: tuple[str, ...] = ()
    detail: str = ""
    error: str = ""
    reopen: str = ""


@dataclass(frozen=True)
class WakeSweep:
    """Every waiting report a sweep looked at, in store order."""

    items: tuple[WakeOutcome, ...] = ()

    @property
    def woken(self) -> tuple[str, ...]:
        return tuple(item.report for item in self.items if item.woken)

    @property
    def errors(self) -> tuple[WakeOutcome, ...]:
        return tuple(item for item in self.items if item.error)


def _require_bound_open(report_id: str, view: BeadView | None, verb: str) -> BeadView:
    if view is None:
        raise IntakeRefused(RefusalReason.NOT_FOUND, f"{report_id} not found")
    if view.binding is None:
        raise IntakeRefused(
            RefusalReason.NOT_A_REPORT,
            f"{report_id} is not bound to an external issue, so it cannot wait for its reporter: "
            f"{verb} is for imported reports only (a report filed with `bh report` or "
            "`bh escalate` has no issue thread to reply on and no intake-hold gate); triage it "
            "with accept, reject, reroute or promote",
        )
    if view.status == "closed":
        raise IntakeRefused(RefusalReason.REPORT_CLOSED, f"{report_id} is closed")
    return view


def check_ask(
    report_id: str, view: BeadView | None, text: str, unread: Iterable[str] = ()
) -> BeadView:
    """Every refusal of ``needs-info``, in order, with nothing written: an empty question, an
    unknown report, a born-native report, a closed one, one triage already fielded, then a thread
    holding evidence triage has not read. Returns the report as read."""
    if not text.strip():
        raise IntakeRefused(
            RefusalReason.INVALID_REQUEST, "--ask needs the question to put to the reporter"
        )
    checked = _require_bound_open(report_id, view, "needs-info")
    if not is_awaiting_triage(checked.labels):
        raise IntakeRefused(
            RefusalReason.NOT_UNTRIAGED,
            f"{report_id} is not an untriaged intake report; only a report triage has not fielded "
            "yet can be put on needs-info",
        )
    pending = tuple(unread)
    if pending:
        raise IntakeRefused(
            RefusalReason.UNREAD_EVIDENCE,
            f"{report_id}'s thread has comments triage has not read ({', '.join(pending)}); they "
            f"may already answer the question: read them with `bh work thread show {report_id}`, "
            f"record the read with `bh work thread considered {report_id} --through <seq>`, "
            "then ask",
        )
    return checked


def check_wake(report_id: str, view: BeadView | None) -> BeadView:
    """Every refusal of ``wake`` for one named report, with nothing written."""
    checked = _require_bound_open(report_id, view, "wake")
    if not is_needs_info(checked.labels):
        raise IntakeRefused(
            RefusalReason.NOT_NEEDS_INFO,
            f"{report_id} is not waiting for its reporter (no intake:needs-info); nothing to wake",
        )
    return checked


def wakes(new_evidence: Iterable[str]) -> bool:
    """Whether a waiting report wakes: exactly when its thread has new human evidence."""
    return any(True for _ in new_evidence)


__all__ = [
    "NEEDS_INFO",
    "UNTRIAGED",
    "NeedsInfoOutcome",
    "NeedsInfoRequest",
    "WakeOutcome",
    "WakeRequest",
    "WakeSweep",
    "check_ask",
    "check_wake",
    "wake_reopen_text",
    "wakes",
]
