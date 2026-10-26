"""The GitHub adapter against the shared ``beadhive.testing.tracker`` conformance suite.

Every case in :data:`~beadhive.testing.tracker.TRACKER_CASES` runs against a fresh adapter over
the offline ``httpx.MockTransport`` stand-in (``tracker_github_fake.FakeGitHub``), on the public
host and on a GitHub Enterprise Server prefix. No case opens a socket, reads a clock or sleeps.

**The read path has no writer.** Five cases enable a write and then require the writer Protocol
(``comment.create``: grants, round trip, both ambiguous outcomes; ``labels.change``: owned labels
only). The writers land in their own beads (W8 ``bh-03s4w``, W9 ``bh-t082i``), each behind its
own operator-approved gate, so those cases are expected to fail here, STRICTLY: the day a writer
lands, its cases pass and this marker must be removed. Every other case, including
``write.refused-when-not-enabled`` (an absent writer is the structural refusal), must pass.
"""

from __future__ import annotations

import pytest

from beadhive.modules.tracker.contracts import (
    CommentWriter,
    LabelWriter,
    Operation,
    TrackerAdapter,
)
from beadhive.testing.conformance import ConformanceFailure
from beadhive.testing.tracker import TRACKER_CASES, run_tracker_case
from tracker_github_fake import github_subject

#: Cases that need a writer the read path does not have (removed as W8 / W9 land).
WRITER_CASES = {
    "write.grant-matches-and-single-use": "W8 bh-03s4w (comment.create)",
    "write.comment-round-trip": "W8 bh-03s4w (comment.create)",
    "write.ambiguous-not-retried": "W8 bh-03s4w (comment.create)",
    "write.ambiguous-not-applied": "W8 bh-03s4w (comment.create)",
    "labels.only-owned": "W9 bh-t082i (labels.change)",
}


def _params():
    for ghes in (False, True):
        for case in TRACKER_CASES:
            marks = ()
            if case.case_id in WRITER_CASES:
                marks = (
                    pytest.mark.xfail(
                        raises=ConformanceFailure,
                        strict=True,
                        reason=f"no writer yet; lands with {WRITER_CASES[case.case_id]}",
                    ),
                )
            host = "ghes" if ghes else "github.com"
            yield pytest.param(case, ghes, id=f"{host}-{case.case_id}", marks=marks)


@pytest.mark.parametrize(("case", "ghes"), list(_params()))
def test_github_adapter_conforms(case, ghes):
    def factory(**kwargs):
        return github_subject(ghes=ghes, **kwargs)

    run_tracker_case(factory, case, subject="github tracker adapter")


def test_every_writer_case_is_named_and_exists():
    """The expected-failure list cannot drift: every name is a real case id."""
    ids = {case.case_id for case in TRACKER_CASES}
    assert set(WRITER_CASES) <= ids


@pytest.mark.parametrize(
    "enabled",
    [frozenset(), frozenset({Operation.COMMENT_CREATE, Operation.LABELS_CHANGE})],
)
def test_the_read_path_exposes_no_writer_even_with_write_flags_on(enabled):
    subject = github_subject(enabled=enabled)
    adapter = subject.adapter

    assert isinstance(adapter, TrackerAdapter)
    assert not isinstance(adapter, CommentWriter)
    assert not isinstance(adapter, LabelWriter)
    assert not hasattr(adapter, "post_comment") and not hasattr(adapter, "change_labels")
    supported = adapter.describe().supported
    assert supported == {Operation.ISSUES_READ, Operation.COMMENTS_READ}
    report = adapter.probe_capabilities()
    for op in (Operation.COMMENT_CREATE, Operation.LABELS_CHANGE):
        assert not report.states[op].supported and not report.states[op].usable
        assert report.states[op].enabled is (op in enabled)
    assert subject.world.write_requests() == 0
