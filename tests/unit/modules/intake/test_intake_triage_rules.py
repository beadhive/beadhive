"""Propose, review and apply-reviewed (bh-9aqmv): the pure half. Adopting a correction, the
needs-info new round and its immutable history, the store's rewrite rule, the input codecs, the
verification rule, the apply order and the disposition-to-verb mapping. No I/O."""

from __future__ import annotations

import dataclasses
import json

import pytest

from beadhive.modules.intake.contracts import (
    ApplyPlan,
    ApplyStep,
    IntakeRefused,
    Proposal,
    ProposalDisposition,
    ProposalDocumentError,
    ProposalStatus,
    ProposalTarget,
    RefusalReason,
    ReviewVerdict,
    TargetRole,
    add_review,
    amend,
    apply_order,
    check_rewrite,
    content_from_document,
    mark_applied,
    mark_stale,
    may_reopen,
    new_proposal,
    new_round,
    plan_apply,
    proposal_from_document,
    proposal_to_document,
    verdict_from_document,
    verification_gap,
)
from beadhive.modules.intake.domain import (
    CLOSE_REASON_LIMIT,
    CloseReason,
    close_reason,
    close_reason_for,
    half_rejected,
)

D = ProposalDisposition
SEAT = "dev/verifier"


def refused(reason: RefusalReason):
    return pytest.raises(IntakeRefused, check=lambda e: e.reason is reason)


def asked() -> Proposal:
    return new_proposal(D.NEEDS_INFO, reporter_text="Which version are you on?", thread_seq=2)


def applied(proposal: Proposal) -> Proposal:
    return mark_applied(add_review(proposal, SEAT, ReviewVerdict.CONFIRMED))


# -- adopting a correction -------------------------------------------------------------------


def test_a_corrected_verdict_that_adopts_its_correction_verifies_the_proposal() -> None:
    wrong = new_proposal(D.FIXED, targets=(ProposalTarget("w-1"),), fixed_in="v0.8.0")
    fixed = add_review(
        wrong,
        SEAT,
        ReviewVerdict.CORRECTED,
        ("only half is fixed",),
        adopt={"disposition": D.NEW, "targets": (), "fixed_in": "", "type": "bug"},
    )
    assert fixed.status is ProposalStatus.VERIFIED and fixed.adopted == 1
    assert (fixed.disposition, fixed.targets, fixed.fixed_in, fixed.seq) == (D.NEW, (), "", 2)
    assert verification_gap(fixed) == ""
    # any later amendment un-verifies it and forgets the adoption
    amended = amend(fixed, priority="1")
    assert (amended.status, amended.adopted) == (ProposalStatus.PROPOSED, 0)
    assert "amended" not in verification_gap(amended) and verification_gap(amended)


def test_only_a_corrected_verdict_adopts_and_only_analyst_fields() -> None:
    with refused(RefusalReason.PROPOSAL_INVALID):
        add_review(asked(), SEAT, ReviewVerdict.CONFIRMED, adopt={"type": "bug"})
    with refused(RefusalReason.PROPOSAL_INVALID):
        add_review(asked(), SEAT, ReviewVerdict.CORRECTED, ("x",), adopt={"status": "verified"})


def test_verification_gap_names_what_is_missing() -> None:
    fresh = new_proposal(D.NEW)
    assert "not reviewed" in verification_gap(fresh)
    corrected = add_review(fresh, SEAT, ReviewVerdict.CORRECTED, ("wrong",))
    assert "not adopted" in verification_gap(corrected)
    unsure = add_review(fresh, SEAT, ReviewVerdict.UNSURE)
    assert "unsure" in verification_gap(unsure)
    confirmed = add_review(fresh, SEAT, ReviewVerdict.CONFIRMED)
    assert verification_gap(confirmed) == ""
    assert "amended since" in verification_gap(amend(confirmed, type="bug"))
    # a later correction after a confirmation wins: the LAST review decides
    assert verification_gap(add_review(confirmed, SEAT, ReviewVerdict.CORRECTED, ("no",)))


def test_adopted_must_name_a_correcting_pass() -> None:
    confirmed = add_review(new_proposal(D.NEW), SEAT, ReviewVerdict.CONFIRMED)
    document = proposal_to_document(confirmed) | {"adopted": 1}
    with pytest.raises(ProposalDocumentError, match="corrected nothing"):
        proposal_from_document(document)
    with pytest.raises(ProposalDocumentError, match="no review pass"):
        proposal_from_document(proposal_to_document(confirmed) | {"adopted": 3})


# -- the needs-info new round ------------------------------------------------------------------


def test_only_an_applied_needs_info_proposal_reopens() -> None:
    assert may_reopen(applied(asked()))
    assert not may_reopen(asked())
    assert not may_reopen(applied(new_proposal(D.NEW)))
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):
        new_round(applied(new_proposal(D.NEW)), new_proposal(D.NEW))
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):
        new_round(asked(), new_proposal(D.NEW))


def test_a_new_round_keeps_the_applied_record_unchanged_in_history() -> None:
    first = applied(asked())
    second = new_round(first, new_proposal(D.NEEDS_INFO, reporter_text="And the OS?"))
    assert second.history == (first,) and second.seq == first.seq + 1
    assert second.status is ProposalStatus.PROPOSED and second.reviews == ()
    third = new_round(applied(second), new_proposal(D.NEW, thread_seq=9))
    # flat, oldest first: each round without a nested history
    assert [h.reporter_text for h in third.history] == ["Which version are you on?", "And the OS?"]
    assert all(h.history == () for h in third.history)
    # a reviewed or historied proposal is not a fresh round
    with refused(RefusalReason.PROPOSAL_INVALID):
        new_round(first, add_review(new_proposal(D.NEW), SEAT, ReviewVerdict.CONFIRMED))


def test_history_round_trips_and_older_documents_read_unchanged() -> None:
    second = new_round(applied(asked()), new_proposal(D.NEW, type="bug"))
    document = proposal_to_document(second)
    assert json.loads(json.dumps(document)) == document
    assert proposal_from_document(document) == second
    # additive: a document without the new keys is what it always was
    plain = proposal_to_document(asked())
    assert "history" not in plain and "adopted" not in plain
    assert proposal_from_document(plain) == asked()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["history"].__setitem__(0, d["history"][0] | {"status": "verified"}),
        lambda d: d["history"].__setitem__(0, d["history"][0] | {"disposition": "new"}),
        lambda d: d["history"].__setitem__(0, d["history"][0] | {"history": [d["history"][0]]}),
        lambda d: d.__setitem__("history", "not a list"),
    ],
)
def test_a_malformed_history_fails_closed(mutate) -> None:
    document = proposal_to_document(new_round(applied(asked()), new_proposal(D.NEW)))
    mutate(document)
    with pytest.raises(ProposalDocumentError):
        proposal_from_document(document)


def test_check_rewrite_is_the_store_rule() -> None:
    first = applied(asked())
    following = new_round(first, new_proposal(D.NEW))
    check_rewrite(None, new_proposal(D.NEW))
    check_rewrite(first, following)  # the deliberate new round
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):  # a bare rewrite of the applied record
        check_rewrite(first, dataclasses.replace(first, reporter_text="x", seq=first.seq + 1))
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):  # an applied outcome other than needs-info
        final = applied(new_proposal(D.NEW))
        check_rewrite(final, new_round(first, new_proposal(D.NEW)))
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):  # a round that edits the kept record
        edited = dataclasses.replace(first, reporter_text="edited")
        check_rewrite(first, dataclasses.replace(following, history=(edited,)))
    reviewed = add_review(following, SEAT, ReviewVerdict.CONFIRMED)
    check_rewrite(following, reviewed)  # history carried forward
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):  # ... and never dropped
        check_rewrite(following, dataclasses.replace(reviewed, history=()))
    stale = mark_stale(following)
    check_rewrite(following, stale)


# -- the input codecs --------------------------------------------------------------------------


def test_content_reads_every_analyst_field_with_defaults() -> None:
    content = content_from_document(
        {
            "disposition": "tracked",
            "targets": ["w-1", {"id": "w-2", "role": "related"}],
            "priority": 2,
            "reporter_text": "Tracked.",
        }
    )
    assert content["disposition"] is D.TRACKED and content["priority"] == "2"
    assert content["targets"] == (
        ProposalTarget("w-1", TargetRole.OWNER),
        ProposalTarget("w-2", TargetRole.RELATED),
    )
    assert content["share_with"] == "" and content["thread_seq"] == 0
    assert new_proposal(**content).seq == 1


@pytest.mark.parametrize(
    "document",
    [
        {},  # no disposition
        {"disposition": "maybe"},
        {"disposition": "new", "status": "verified"},  # cannot claim a review
        {"disposition": "new", "reviews": []},
        {"disposition": "new", "targets": "w-1"},
        {"disposition": "new", "targets": [{"id": "w-1", "role": "boss"}]},
        {"disposition": "new", "thread_seq": "3"},
        "new",
    ],
)
def test_content_fails_closed(document) -> None:
    with pytest.raises(ProposalDocumentError):
        content_from_document(document)


def test_verdict_reads_an_adopted_correction() -> None:
    verdict = verdict_from_document(
        {
            "verdict": "corrected",
            "corrections": ["half fixed"],
            "pass": 1,
            "adopt": {"disposition": "new", "type": "bug"},
        }
    )
    assert verdict.verdict is ReviewVerdict.CORRECTED and verdict.pass_no == 1
    assert verdict.adopt is not None and verdict.adopt["disposition"] is D.NEW
    assert verdict_from_document({"verdict": "confirmed"}).adopt is None
    with pytest.raises(ProposalDocumentError):
        verdict_from_document({"verdict": "confirmed", "adopt": {"disposition": "new"}})
    with pytest.raises(ProposalDocumentError):
        verdict_from_document({"verdict": "confirmed", "status": "applied"})


# -- the apply order and the verb mapping ------------------------------------------------------


def test_apply_order_is_canonicals_then_singles_then_members() -> None:
    run = {
        "r-1": new_proposal(D.DUPLICATE_OF, share_with="r-9"),
        "r-2": new_proposal(D.REJECT, reporter_text="no"),
        "r-5": new_proposal(D.DUPLICATE_OF, share_with="r-3"),
        "r-3": new_proposal(D.NEW),
        "r-9": new_proposal(D.TRACKED, targets=(ProposalTarget("w-1"),)),
    }
    assert apply_order(run) == ("r-3", "r-9", "r-2", "r-1", "r-5")


def _plan(disposition: D, *, bound: bool = True, **fields) -> ApplyPlan | str:
    extra = {
        k: fields.pop(k) for k in ("fixed_in", "canonical_work", "canonical_ref") if k in fields
    }
    return plan_apply(new_proposal(disposition, **fields), bound=bound, **extra)


def test_every_disposition_maps_onto_the_existing_verbs() -> None:
    owner = (ProposalTarget("w-1"),)
    assert _plan(D.NEW) == ApplyPlan((ApplyStep.ACCEPT_NEW_WORK,))
    assert _plan(D.NEW, bound=False) == ApplyPlan((ApplyStep.ACCEPT,))
    assert _plan(D.TRACKED, targets=owner) == ApplyPlan((ApplyStep.ACCEPT_WORK,), work="w-1")
    assert _plan(D.FIXED, targets=owner) == ApplyPlan((ApplyStep.ACCEPT_WORK,), work="w-1")
    no_bead = _plan(D.FIXED, fix_commit="abc1234", fixed_in="v1.2.0")
    assert isinstance(no_bead, ApplyPlan)
    assert no_bead.steps == (ApplyStep.DRAFT_REPLY, ApplyStep.REJECT)
    assert "v1.2.0" in no_bead.reason and no_bead.close is CloseReason.COMPLETED
    assert _plan(D.PARTIALLY_FIXED, targets=owner).steps == (ApplyStep.ACCEPT_WORK,)
    assert _plan(D.PARTIALLY_FIXED).steps == (ApplyStep.ACCEPT_NEW_WORK,)
    member = _plan(D.DUPLICATE_OF, share_with="r-1", canonical_work="w-7", canonical_ref="#4")
    assert member == ApplyPlan(
        (ApplyStep.DRAFT_REPLY, ApplyStep.LINK, ApplyStep.REJECT),
        work="w-7",
        reason="This is a duplicate of #4; the work is tracked there.",
        close=CloseReason.DUPLICATE,
    )
    unbound = _plan(
        D.DUPLICATE_OF, bound=False, share_with="r-1", canonical_work="w-7", canonical_ref="#4"
    )
    assert isinstance(unbound, ApplyPlan) and unbound.close is None
    assert unbound.steps == (ApplyStep.LINK, ApplyStep.REJECT)
    # the outbox off (the default): linked and rejected with no reply and no close, as on 0.35.2
    silent = plan_apply(
        new_proposal(D.DUPLICATE_OF, share_with="r-1"),
        bound=True,
        canonical_work="w-7",
        canonical_ref="#4",
        replies=False,
    )
    assert isinstance(silent, ApplyPlan) and silent.close is None
    assert silent.steps == (ApplyStep.LINK, ApplyStep.REJECT)
    # a fixed or reject reply still needs the outbox: its draft refuses first, nothing written
    assert _plan(D.REJECT, reporter_text="No.").steps[0] is ApplyStep.DRAFT_REPLY
    assert "tracks no work" in _plan(D.DUPLICATE_OF, share_with="r-1")
    assert _plan(D.NEEDS_INFO, reporter_text="Which OS?") == ApplyPlan(
        (ApplyStep.NEEDS_INFO,), question="Which OS?"
    )  # never a close
    assert isinstance(_plan(D.NEEDS_INFO, bound=False, reporter_text="?"), str)
    assert _plan(D.REJECT, reporter_text="Works as designed.") == ApplyPlan(
        (ApplyStep.DRAFT_REPLY, ApplyStep.REJECT),
        reason="Works as designed.",
        close=CloseReason.NOT_PLANNED,
    )
    born_native = _plan(D.REJECT, bound=False, reporter_text="No.")
    assert born_native.steps == (ApplyStep.REJECT,) and born_native.close is None
    assert _plan(D.REROUTE).steps == (ApplyStep.REROUTE,)
    assert "never applied" in _plan(D.DECISION)


def test_only_a_thread_ending_outcome_closes_the_issue() -> None:
    assert close_reason_for(D.FIXED) is CloseReason.COMPLETED
    assert close_reason_for(D.REJECT) is CloseReason.NOT_PLANNED
    assert close_reason_for(D.DUPLICATE_OF) is CloseReason.DUPLICATE
    for keeps_open in (D.NEEDS_INFO, D.NEW, D.TRACKED, D.PARTIALLY_FIXED, D.REROUTE, D.DECISION):
        assert close_reason_for(keeps_open) is None


# -- closing a fielded report (bh-mqv12) ------------------------------------------------------


def test_a_close_reason_is_cut_to_what_beads_stores() -> None:
    short = "Fixed in v1.2.0."
    assert close_reason(short) is short
    exact = "x" * CLOSE_REASON_LIMIT
    assert close_reason(exact) == exact
    cut = close_reason("word " * 100)
    assert len(cut) <= CLOSE_REASON_LIMIT and cut.endswith("\u2026")
    assert ("word " * 100).startswith(cut[:-1])


@pytest.mark.parametrize(
    ("status", "labels", "half"),
    [
        ("open", ("intake:rejected",), True),
        ("closed", ("intake:rejected",), False),
        ("open", ("intake:untriaged",), False),
        ("open", (), False),
    ],
)
def test_only_an_open_report_on_intake_rejected_is_half_rejected(status, labels, half) -> None:
    assert half_rejected(status, labels) is half
