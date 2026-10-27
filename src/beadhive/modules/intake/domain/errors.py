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
    # the held close: a bound report with a live blocker other than its own intake-hold
    LIVE_BLOCKER = "live_blocker"
    # thread ingestion refusals (the conversation model, bh-fptob)
    NOT_BOUND = "not_bound"  # the report is not bound to an external issue: it has no thread
    PUBLISHED_STORE = "published_store"  # the operational store would be published
    CHECKPOINT_SCOPE = "checkpoint_scope"  # a stored token offered to a query of another scope
    INTAKE_BUSY = "intake_busy"  # another intake operation holds the binding's lock
    THREAD_MOVED = "thread_moved"  # the stored document's seq moved since it was read
    # outbox refusals (the communication records and their O1 to O19 machine, bh-6wj9y R5)
    OUTBOX_DISABLED = "outbox_disabled"  # intake.external.outbox.enabled is off (the default)
    UNKNOWN_OUTBOX = "unknown_outbox"  # no record, or no such operation on it
    INVALID_OUTBOX_ID = "invalid_outbox_id"  # not an opaque ob1_ id (never a bead id)
    INVALID_DRAFT = "invalid_draft"  # a draft with no operation, or a label op with no base
    COMMIT_LINKS_OFF = "commit_links_off"  # a public commit URL while commit_links is off
    LABEL_NOT_OWNED = "label_not_owned"  # a label outside the owned prefixes
    OUTBOX_TRANSITION = "outbox_transition"  # the event is not allowed from the current state
    OPERATOR_SEAT_REQUIRED = "operator_seat_required"  # an agent seat, or no actor at all
    STALE_DIGEST = "stale_digest"  # the digest is not the fresh render's
    UNRESOLVED_FINDING = "unresolved_finding"  # a leak finding neither fixed nor published
    NEVER_OVERRIDABLE = "never_overridable"  # a secret, no sanitizer, or a label not owned
    UNKNOWN_FINDING = "unknown_finding"  # an override names no current finding
    CAPABILITY_UNUSABLE = "capability_unusable"  # the write, or comments.read, is not usable
    TARGET_MISMATCH = "target_mismatch"  # the record's target is no longer the binding
    GRANT_REUSED = "grant_reused"  # a grant id the document already holds
    GRANT_UNUSABLE = "grant_unusable"  # the approval's grant was consumed or voided
    UNKNOWN_OUTCOME = "unknown_outcome"  # discarding an unknown needs accept_unknown
    # the supervised sender (I9a): the store's seq re-check and the host half of IntakeLock
    OUTBOX_MOVED = "outbox_moved"  # the stored intake.outbox seq moved since it was read
    NOT_PRIMARY = "not_primary"  # this host is not the hive's primary writer
    # an operation of the port that no bead has implemented yet
    NOT_AVAILABLE = "not_available"


class IntakeRefused(Exception):
    """A request intake declined before changing anything it could not undo."""

    def __init__(self, reason: RefusalReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


class IntakeStoreError(Exception):
    """The report store could not complete a read or write; ``detail`` is already scrubbed.

    ``operation`` names the failed step in the module's own vocabulary (``create``, ``set_state``,
    ``update_fields``, ``close``, ``assign``, ``track``, ``record_snapshot``, ``read_thread``,
    ``write_thread``, ``open_hold``, ``resolve_hold``, ``read_outbox``, ``write_outbox``, plus
    the service's ``stamp:<dimension>`` and ``clear_intake`` re-tags) so a transport can word its
    message; ``returncode`` is the store's own exit status (1 when it has none)."""

    def __init__(
        self,
        detail: str,
        *,
        bead: str = "",
        operation: str = "",
        returncode: int = 1,
    ) -> None:
        self.detail = detail
        self.bead = bead
        self.operation = operation
        self.returncode = returncode
        super().__init__(detail)


__all__ = ["IntakeRefused", "IntakeStoreError", "RefusalReason"]
