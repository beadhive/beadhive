"""Refusals and store failures of the intake module. Standard library only.

:class:`IntakeRefused` carries a machine-readable :class:`RefusalReason` plus a one-line detail
that names the fix; presentation (exit codes, glyphs) belongs to the transport. The reasons that
name later behaviour (import, link, discovery) are declared here so the contract is one place;
:attr:`RefusalReason.NOT_AVAILABLE` marks an operation of the port that the skeleton does not
implement yet and disappears as each owning bead lands.
"""

from __future__ import annotations

from enum import StrEnum


class RefusalReason(StrEnum):
    # intake switched off, or nothing bound to read from (the ADR's section 5, R0)
    INTAKE_DISABLED = "intake_disabled"
    # import refusals R1 to R8
    PUBLIC_STORE_ID = "public_store_id"
    NOT_A_URL = "not_a_url"
    UNKNOWN_URL = "unknown_url"
    OTHER_PROJECT = "other_project"
    CHANGE_REQUEST = "change_request"
    NOT_VISIBLE = "not_visible"
    DUPLICATE_BINDING = "duplicate_binding"
    NOT_FOUND_IN_PROJECT = "not_found_in_project"
    # link refusals
    NOT_A_REPORT = "not_a_report"
    CROSS_STORE_LINK = "cross_store_link"
    SELF_LINK = "self_link"
    TARGET_UNTRIAGED = "target_untriaged"
    TARGET_IS_REPORT = "target_is_report"
    ALREADY_LINKED_ELSEWHERE = "already_linked_elsewhere"
    # filing and disposition refusals (today's report.py and triage.py guards)
    INVALID_TYPE = "invalid_type"
    INVALID_LABELS = "invalid_labels"
    NOT_FOUND = "not_found"
    NOT_UNTRIAGED = "not_untriaged"
    REASON_REQUIRED = "reason_required"
    UNKNOWN_DISPOSITION = "unknown_disposition"
    # an operation of the port that no bead has implemented yet
    NOT_AVAILABLE = "not_available"


class IntakeRefused(Exception):
    """A request intake declined before changing anything it could not undo."""

    def __init__(self, reason: RefusalReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


class IntakeStoreError(Exception):
    """The report store could not complete a read or write; ``detail`` is already scrubbed."""

    def __init__(self, detail: str, *, bead: str = "") -> None:
        self.detail = detail
        self.bead = bead
        super().__init__(detail)


__all__ = ["IntakeRefused", "IntakeStoreError", "RefusalReason"]
