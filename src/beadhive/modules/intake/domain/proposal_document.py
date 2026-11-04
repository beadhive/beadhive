"""The ``intake.proposal`` document: a :class:`~.proposal.Proposal` as plain JSON values and back.

One flat top-level metadata key on the report bead, schema-versioned and rewritten whole, beside
``intake.thread`` and ``intake.outbox``. Private operational-store data: never published. Pure; no
I/O.

Decoding fails closed (:class:`ProposalDocumentError`) on a malformed document, an unknown schema
or an unknown disposition, target role, verdict or status: a store must never read such a document
as "no proposal", because the next write would replace it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import IntakeRefused
from .proposal import (
    PROPOSAL_SCHEMA,
    Proposal,
    ProposalDisposition,
    ProposalReview,
    ProposalStatus,
    ProposalTarget,
    ReviewVerdict,
    TargetRole,
    validate_proposal,
)

PROPOSAL_KEY = "intake.proposal"


class ProposalDocumentError(ValueError):
    """The stored ``intake.proposal`` value is not a document this schema can read."""


def proposal_to_document(proposal: Proposal) -> dict[str, Any]:
    """The JSON value stored under :data:`PROPOSAL_KEY`."""
    return {
        "schema": proposal.schema,
        "seq": proposal.seq,
        "status": proposal.status.value,
        "disposition": proposal.disposition.value,
        "targets": [{"id": t.id, "role": t.role.value} for t in proposal.targets],
        "share_with": proposal.share_with,
        "type": proposal.type,
        "priority": proposal.priority,
        "reporter_text": proposal.reporter_text,
        "evidence": list(proposal.evidence),
        "analysed_commit": proposal.analysed_commit,
        "thread_seq": proposal.thread_seq,
        "reviews": [
            {
                "pass": r.pass_no,
                "seat": r.seat,
                "verdict": r.verdict.value,
                "corrections": list(r.corrections),
            }
            for r in proposal.reviews
        ],
    }


# -- decoding --------------------------------------------------------------------------------


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProposalDocumentError(f"{what} is not an object")
    return value


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProposalDocumentError(f"{what} is not an integer")
    return value


def _str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ProposalDocumentError(f"{what} is not a string")
    return value


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ProposalDocumentError(f"{what} is not a list")
    return tuple(_str(item, what) for item in value)


def _items(value: Any, what: str) -> list[Any]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ProposalDocumentError(f"{what} is not a list")
    return list(value)


def proposal_from_document(value: Any) -> Proposal:
    """The :class:`Proposal` a stored value holds; :class:`ProposalDocumentError` if it is not
    one."""
    try:
        raw = _mapping(value, PROPOSAL_KEY)
        schema = _int(raw["schema"], "schema")
        if schema > PROPOSAL_SCHEMA:
            raise ProposalDocumentError(
                f"intake.proposal schema {schema} is newer than this host reads ({PROPOSAL_SCHEMA})"
            )
        if schema < 1:
            raise ProposalDocumentError(f"intake.proposal schema {schema} is not a known schema")
        targets = tuple(
            ProposalTarget(_str(t["id"], "target id"), TargetRole(_str(t["role"], "target role")))
            for t in (_mapping(item, "target") for item in _items(raw["targets"], "targets"))
        )
        reviews = tuple(
            ProposalReview(
                _int(r["pass"], "review pass"),
                _str(r["seat"], "review seat"),
                ReviewVerdict(_str(r["verdict"], "review verdict")),
                _strings(r.get("corrections", ()), "corrections"),
            )
            for r in (_mapping(item, "review") for item in _items(raw["reviews"], "reviews"))
        )
        proposal = Proposal(
            disposition=ProposalDisposition(_str(raw["disposition"], "disposition")),
            targets=targets,
            share_with=_str(raw.get("share_with", ""), "share_with"),
            type=_str(raw.get("type", ""), "type"),
            priority=_str(raw.get("priority", ""), "priority"),
            reporter_text=_str(raw.get("reporter_text", ""), "reporter_text"),
            evidence=_strings(raw.get("evidence", ()), "evidence"),
            analysed_commit=_str(raw.get("analysed_commit", ""), "analysed_commit"),
            thread_seq=_int(raw.get("thread_seq", 0), "thread_seq"),
            reviews=reviews,
            status=ProposalStatus(_str(raw["status"], "status")),
            seq=_int(raw["seq"], "seq"),
            schema=schema,
        )
        validate_proposal(proposal)
        return proposal
    except ProposalDocumentError:
        raise
    except IntakeRefused as exc:
        raise ProposalDocumentError(f"inconsistent intake.proposal document: {exc.detail}") from exc
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProposalDocumentError(f"malformed intake.proposal document: {exc}") from exc


__all__ = [
    "PROPOSAL_KEY",
    "ProposalDocumentError",
    "proposal_from_document",
    "proposal_to_document",
]
