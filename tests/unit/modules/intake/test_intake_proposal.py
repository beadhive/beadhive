"""The ``intake.proposal`` document: model, status machine and fail-closed codec. Pure."""

from __future__ import annotations

import copy
import json

import pytest

from beadhive.modules.intake.contracts import (
    PROPOSAL_KEY,
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
    mark_applied,
    mark_stale,
    new_proposal,
    proposal_from_document,
    proposal_to_document,
)


def make(disposition: ProposalDisposition = ProposalDisposition.TRACKED) -> Proposal:
    return new_proposal(
        disposition,
        targets=(
            ProposalTarget("bh-a1", TargetRole.OWNER),
            ProposalTarget("bh-b2", TargetRole.RELATED),
        ),
        share_with="bh-canon",
        type="bug",
        priority="2",
        reporter_text="it crashes on start",
        evidence=("thread comment 3", "commit abc123"),
        analysed_commit="abc123",
        thread_seq=7,
    )


def refused(reason: RefusalReason):
    return pytest.raises(IntakeRefused, check=lambda e: e.reason is reason)


def test_key_is_the_flat_metadata_key() -> None:
    assert PROPOSAL_KEY == "intake.proposal"


@pytest.mark.parametrize("disposition", list(ProposalDisposition))
def test_round_trip_for_every_disposition(disposition: ProposalDisposition) -> None:
    proposal = add_review(make(disposition), "dev/reviewer", ReviewVerdict.CONFIRMED)
    document = proposal_to_document(proposal)
    assert json.loads(json.dumps(document)) == document
    assert proposal_from_document(document) == proposal


def test_round_trip_keeps_corrections_and_every_status() -> None:
    corrected = add_review(make(), "dev/r", ReviewVerdict.CORRECTED, ("wrong target",))
    applied = mark_applied(add_review(make(), "dev/r", ReviewVerdict.CONFIRMED))
    stale = mark_stale(make())
    for proposal in (corrected, applied, stale):
        assert proposal_from_document(proposal_to_document(proposal)) == proposal


def test_round_trip_keeps_a_fix_claim_and_reads_a_document_without_one() -> None:
    claim = new_proposal(ProposalDisposition.FIXED, fixed_in="v0.34.0", fix_commit="abc1234")
    assert proposal_from_document(proposal_to_document(claim)) == claim
    older = proposal_to_document(make())
    del older["fixed_in"], older["fix_commit"]  # written before the fix claim fields existed
    assert proposal_from_document(older) == make()
    assert amend(claim, fixed_in="v0.35.0").fixed_in == "v0.35.0"


def test_new_proposal_starts_proposed_at_seq_one() -> None:
    proposal = make()
    assert (proposal.status, proposal.seq, proposal.reviews) == (ProposalStatus.PROPOSED, 1, ())


def test_confirmed_review_verifies_and_numbers_passes() -> None:
    first = add_review(make(), "dev/a", ReviewVerdict.UNSURE)
    assert first.status is ProposalStatus.PROPOSED
    second = add_review(first, "dev/b", ReviewVerdict.CONFIRMED)
    assert second.status is ProposalStatus.VERIFIED
    assert [r.pass_no for r in second.reviews] == [1, 2]
    assert second.seq == first.seq + 1


def test_correcting_review_moves_verified_back_to_proposed() -> None:
    verified = add_review(make(), "dev/a", ReviewVerdict.CONFIRMED)
    back = add_review(verified, "dev/b", ReviewVerdict.CORRECTED, ("target is closed",))
    assert back.status is ProposalStatus.PROPOSED
    assert back.reviews[-1].corrections == ("target is closed",)


def test_unsure_review_does_not_leave_a_proposal_verified() -> None:
    verified = add_review(make(), "dev/a", ReviewVerdict.CONFIRMED)
    assert add_review(verified, "dev/b", ReviewVerdict.UNSURE).status is ProposalStatus.PROPOSED


def test_corrections_go_with_a_corrected_verdict_only() -> None:
    with refused(RefusalReason.PROPOSAL_INVALID):
        add_review(make(), "dev/a", ReviewVerdict.CORRECTED)
    with refused(RefusalReason.PROPOSAL_INVALID):
        add_review(make(), "dev/a", ReviewVerdict.CONFIRMED, ("nit",))


def test_amend_unverifies_and_refuses_unknown_fields() -> None:
    verified = add_review(make(), "dev/a", ReviewVerdict.CONFIRMED)
    amended = amend(verified, disposition=ProposalDisposition.FIXED, priority="1")
    assert (amended.status, amended.disposition, amended.priority) == (
        ProposalStatus.PROPOSED,
        ProposalDisposition.FIXED,
        "1",
    )
    assert amended.reviews == verified.reviews
    with refused(RefusalReason.PROPOSAL_INVALID):
        amend(verified, status=ProposalStatus.APPLIED)


def test_apply_needs_verified() -> None:
    with refused(RefusalReason.PROPOSAL_TRANSITION):
        mark_applied(make())
    applied = mark_applied(add_review(make(), "dev/a", ReviewVerdict.CONFIRMED))
    assert applied.status is ProposalStatus.APPLIED


def test_applied_proposal_is_immutable() -> None:
    applied = mark_applied(add_review(make(), "dev/a", ReviewVerdict.CONFIRMED))
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):
        add_review(applied, "dev/b", ReviewVerdict.CORRECTED, ("late",))
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):
        amend(applied, priority="0")
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):
        mark_applied(applied)
    with refused(RefusalReason.PROPOSAL_IMMUTABLE):
        mark_stale(applied)


def test_stale_is_terminal_and_idempotent() -> None:
    stale = mark_stale(make())
    assert stale.status is ProposalStatus.STALE
    assert mark_stale(stale) is stale
    with refused(RefusalReason.PROPOSAL_TRANSITION):
        add_review(stale, "dev/a", ReviewVerdict.CONFIRMED)
    with refused(RefusalReason.PROPOSAL_TRANSITION):
        mark_applied(stale)
    with refused(RefusalReason.PROPOSAL_TRANSITION):
        amend(stale, priority="1")


def test_duplicate_or_blank_targets_are_refused() -> None:
    with refused(RefusalReason.PROPOSAL_INVALID):
        new_proposal(
            ProposalDisposition.TRACKED,
            targets=(ProposalTarget("bh-a"), ProposalTarget("bh-a", TargetRole.RELATED)),
        )
    with refused(RefusalReason.PROPOSAL_INVALID):
        new_proposal(ProposalDisposition.TRACKED, targets=(ProposalTarget(""),))


# -- fail-closed decode ----------------------------------------------------------------------


def stored() -> dict:
    return proposal_to_document(add_review(make(), "dev/a", ReviewVerdict.CONFIRMED))


def broken(**edit) -> dict:
    document = copy.deepcopy(stored())
    document.update(edit)
    return document


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(broken(schema=99), id="newer-schema"),
        pytest.param(broken(schema=0), id="schema-zero"),
        pytest.param(broken(schema="1"), id="schema-not-int"),
        pytest.param(broken(disposition="maybe"), id="unknown-disposition"),
        pytest.param(broken(status="done"), id="unknown-status"),
        pytest.param(broken(targets=[{"id": "x", "role": "boss"}]), id="unknown-role"),
        pytest.param(broken(targets=[{"id": "x"}]), id="target-without-role"),
        pytest.param(broken(targets="bh-a"), id="targets-not-list"),
        pytest.param(
            broken(reviews=[{"pass": 1, "seat": "s", "verdict": "ok", "corrections": []}]),
            id="unknown-verdict",
        ),
        pytest.param(
            broken(reviews=[{"pass": 2, "seat": "s", "verdict": "confirmed"}]), id="pass-gap"
        ),
        pytest.param(
            broken(reviews=[{"pass": 1, "seat": "s", "verdict": "corrected"}]),
            id="corrected-without-corrections",
        ),
        pytest.param(broken(thread_seq="7"), id="thread-seq-not-int"),
        pytest.param(broken(seq=True), id="seq-bool"),
        pytest.param(broken(evidence="x"), id="evidence-not-list"),
        pytest.param({k: v for k, v in stored().items() if k != "seq"}, id="missing-seq"),
        pytest.param(None, id="none"),
        pytest.param([], id="list"),
    ],
)
def test_decode_fails_closed(document) -> None:
    with pytest.raises(ProposalDocumentError):
        proposal_from_document(document)
