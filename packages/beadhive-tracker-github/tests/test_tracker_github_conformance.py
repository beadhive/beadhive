"""The GitHub adapter against the shared ``beadhive.testing.tracker`` conformance suite.

Every case in :data:`~beadhive.testing.tracker.TRACKER_CASES` runs against a fresh adapter over
the offline ``httpx.MockTransport`` stand-in (``tracker_github_fake.FakeGitHub``), on the public
host and on a GitHub Enterprise Server prefix. No case opens a socket, reads a clock or sleeps.

**Writers land one bead at a time.** Cases that enable a write and then require its writer
Protocol (``comment.create``: grants, round trip, both ambiguous outcomes) are expected to fail
until that writer lands (W8 ``bh-03s4w``), STRICTLY: the day a writer lands, its cases pass and
its marker must be removed. ``labels.change`` landed with W9 ``bh-t082i`` (``labels.only-owned``
passes). Every other case, including ``write.refused-when-not-enabled``, must pass.
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
def test_the_adapter_exposes_only_the_writers_that_have_landed(enabled):
    subject = github_subject(enabled=enabled)
    adapter = subject.adapter

    assert isinstance(adapter, TrackerAdapter)
    assert not isinstance(adapter, CommentWriter)
    assert not hasattr(adapter, "post_comment")
    # labels.change has landed (W9): the writer method exists but refuses unless enabled and granted
    # (test_tracker_github_labels.py); the composition root narrows it away when disabled.
    assert isinstance(adapter, LabelWriter)
    supported = adapter.describe().supported
    assert supported == {Operation.ISSUES_READ, Operation.COMMENTS_READ, Operation.LABELS_CHANGE}
    report = adapter.probe_capabilities()
    assert not report.states[Operation.COMMENT_CREATE].supported
    assert not report.states[Operation.COMMENT_CREATE].usable
    for op in (Operation.COMMENT_CREATE, Operation.LABELS_CHANGE):
        assert report.states[op].enabled is (op in enabled)
    assert subject.world.write_requests() == 0
