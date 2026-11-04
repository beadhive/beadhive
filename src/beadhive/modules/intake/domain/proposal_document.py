"""The ``intake.proposal`` document: a :class:`~.proposal.Proposal` as plain JSON values and back.

One flat top-level metadata key on the report bead, schema-versioned and rewritten whole, beside
``intake.thread`` and ``intake.outbox``. Private operational-store data: never published. Pure; no
I/O.

Decoding fails closed (:class:`ProposalDocumentError`) on a malformed document, an unknown schema
or an unknown disposition, target role, verdict or status: a store must never read such a document
as "no proposal", because the next write would replace it.

``adopted`` and ``history`` (bh-9aqmv) are additive: written only when set, read as ``0`` / ``()``
when absent, so every earlier schema-1 document reads unchanged.

The two INPUT codecs read what the triage skill hands bh: :func:`content_from_document` an
analyst's proposal (only the fields the analyst owns; anything else, ``status`` or ``reviews``
included, is refused) and :func:`verdict_from_document` a verifier's verdict, optionally with the
corrected content it adopts.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import IntakeRefused
from .proposal import (
    ANALYST_FIELDS,
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
    document = _document(proposal)
    if proposal.adopted:
        document["adopted"] = proposal.adopted
    if proposal.history:
        document["history"] = [_document(earlier) for earlier in proposal.history]
    return document


def _document(proposal: Proposal) -> dict[str, Any]:
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
        "fixed_in": proposal.fixed_in,
        "fix_commit": proposal.fix_commit,
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
        history = tuple(
            _decode(_mapping(item, "history round"), nested=True)
            for item in _items(raw.get("history", ()), "history")
        )
        proposal = dataclasses.replace(_decode(raw, nested=False), history=history)
        validate_proposal(proposal)
        return proposal
    except ProposalDocumentError:
        raise
    except IntakeRefused as exc:
        raise ProposalDocumentError(f"inconsistent intake.proposal document: {exc.detail}") from exc
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProposalDocumentError(f"malformed intake.proposal document: {exc}") from exc


def _targets(value: Any) -> tuple[ProposalTarget, ...]:
    return tuple(
        ProposalTarget(_str(t["id"], "target id"), TargetRole(_str(t["role"], "target role")))
        for t in (_mapping(item, "target") for item in _items(value, "targets"))
    )


def _decode(raw: Mapping[str, Any], *, nested: bool) -> Proposal:
    """One round of the document, without its history (a history round has none)."""
    if nested and _items(raw.get("history", ()), "history"):
        raise ProposalDocumentError("a history round has a history of its own")
    schema = _int(raw["schema"], "schema")
    if schema > PROPOSAL_SCHEMA:
        raise ProposalDocumentError(
            f"intake.proposal schema {schema} is newer than this host reads ({PROPOSAL_SCHEMA})"
        )
    if schema < 1:
        raise ProposalDocumentError(f"intake.proposal schema {schema} is not a known schema")
    reviews = tuple(
        ProposalReview(
            _int(r["pass"], "review pass"),
            _str(r["seat"], "review seat"),
            ReviewVerdict(_str(r["verdict"], "review verdict")),
            _strings(r.get("corrections", ()), "corrections"),
        )
        for r in (_mapping(item, "review") for item in _items(raw["reviews"], "reviews"))
    )
    return Proposal(
        disposition=ProposalDisposition(_str(raw["disposition"], "disposition")),
        targets=_targets(raw["targets"]),
        share_with=_str(raw.get("share_with", ""), "share_with"),
        type=_str(raw.get("type", ""), "type"),
        priority=_str(raw.get("priority", ""), "priority"),
        reporter_text=_str(raw.get("reporter_text", ""), "reporter_text"),
        evidence=_strings(raw.get("evidence", ()), "evidence"),
        analysed_commit=_str(raw.get("analysed_commit", ""), "analysed_commit"),
        thread_seq=_int(raw.get("thread_seq", 0), "thread_seq"),
        fixed_in=_str(raw.get("fixed_in", ""), "fixed_in"),
        fix_commit=_str(raw.get("fix_commit", ""), "fix_commit"),
        reviews=reviews,
        status=ProposalStatus(_str(raw["status"], "status")),
        seq=_int(raw["seq"], "seq"),
        schema=schema,
        adopted=_int(raw.get("adopted", 0), "adopted"),
    )


# -- the input codecs: what the triage skill hands bh ---------------------------------------


def _input_target(value: Any) -> ProposalTarget:
    if isinstance(value, str):
        return ProposalTarget(value)
    item = _mapping(value, "target")
    unknown = set(item) - {"id", "role"}
    if unknown:
        raise ProposalDocumentError(f"a target has unknown keys: {sorted(unknown)}")
    return ProposalTarget(
        _str(item["id"], "target id"), TargetRole(_str(item.get("role", "owner"), "target role"))
    )


def content_from_document(value: Any) -> dict[str, Any]:
    """The analyst-owned fields of a proposal document, as :func:`~.proposal.new_proposal` and
    :func:`~.proposal.amend` take them, every field present (an absent one is its default, so a
    re-proposal replaces the whole content). ``disposition`` is required; a target is an id (an
    owner) or ``{"id", "role"}``. Any key the analyst does not own (``status``, ``reviews``,
    ``seq``, ...) is refused, so an input can never claim a review it did not get."""
    try:
        raw = _mapping(value, "the proposal")
        unknown = set(raw) - ANALYST_FIELDS
        if unknown:
            raise ProposalDocumentError(
                f"the proposal has keys the analyst does not own: {sorted(unknown)}"
            )
        if "disposition" not in raw:
            raise ProposalDocumentError("the proposal names no disposition")
        return {
            "disposition": ProposalDisposition(_str(raw["disposition"], "disposition")),
            "targets": tuple(_input_target(t) for t in _items(raw.get("targets", ()), "targets")),
            "share_with": _str(raw.get("share_with", ""), "share_with"),
            "type": _str(raw.get("type", ""), "type"),
            "priority": _priority(raw.get("priority", "")),
            "reporter_text": _str(raw.get("reporter_text", ""), "reporter_text"),
            "evidence": _strings(raw.get("evidence", ()), "evidence"),
            "analysed_commit": _str(raw.get("analysed_commit", ""), "analysed_commit"),
            "thread_seq": _int(raw.get("thread_seq", 0), "thread_seq"),
            "fixed_in": _str(raw.get("fixed_in", ""), "fixed_in"),
            "fix_commit": _str(raw.get("fix_commit", ""), "fix_commit"),
        }
    except ProposalDocumentError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProposalDocumentError(f"malformed proposal: {exc}") from exc


def _priority(value: Any) -> str:
    """A priority as the store keeps it (``"2"``); a bare integer is accepted."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _str(value, "priority")


#: The keys of a verdict document.
VERDICT_FIELDS = frozenset({"verdict", "seat", "corrections", "pass", "adopt"})


@dataclass(frozen=True)
class VerdictInput:
    """A decoded verdict. ``seat`` empty: the actor recording it; ``pass_no`` 0: any next pass
    (else it must BE the next pass, so the same verdict recorded twice is refused); ``adopt`` the
    corrected content (every analyst field, as :func:`content_from_document` reads it) or
    ``None``."""

    verdict: ReviewVerdict
    corrections: tuple[str, ...] = ()
    seat: str = ""
    pass_no: int = 0
    adopt: Mapping[str, Any] | None = None


def verdict_from_document(value: Any) -> VerdictInput:
    """A verifier's verdict: ``{"verdict", "corrections"?, "seat"?, "pass"?, "adopt"?}``.
    ``adopt`` is a proposal document (:func:`content_from_document`) and goes only with a
    ``corrected`` verdict."""
    try:
        raw = _mapping(value, "the verdict")
        unknown = set(raw) - VERDICT_FIELDS
        if unknown:
            raise ProposalDocumentError(f"the verdict has unknown keys: {sorted(unknown)}")
        verdict = ReviewVerdict(_str(raw["verdict"], "verdict"))
        adopt = raw.get("adopt")
        if adopt is not None and verdict is not ReviewVerdict.CORRECTED:
            raise ProposalDocumentError("only a corrected verdict adopts a correction")
        return VerdictInput(
            verdict=verdict,
            corrections=_strings(raw.get("corrections", ()), "corrections"),
            seat=_str(raw.get("seat", ""), "seat"),
            pass_no=_int(raw.get("pass", 0), "pass"),
            adopt=content_from_document(adopt) if adopt is not None else None,
        )
    except ProposalDocumentError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProposalDocumentError(f"malformed verdict: {exc}") from exc


__all__ = [
    "PROPOSAL_KEY",
    "VERDICT_FIELDS",
    "ProposalDocumentError",
    "VerdictInput",
    "content_from_document",
    "proposal_from_document",
    "proposal_to_document",
    "verdict_from_document",
]
