"""The GitHub adapter against the shared ``beadhive.testing.tracker`` conformance suite.

Every case in :data:`~beadhive.testing.tracker.TRACKER_CASES` runs against a fresh adapter over
the offline ``httpx.MockTransport`` stand-in (``tracker_github_fake.FakeGitHub``), on the public
host and on a GitHub Enterprise Server prefix. No case opens a socket, reads a clock or sleeps.

**Writers land one bead at a time.** Both writers have landed, ``comment.create`` (W8
``bh-03s4w``: grants, round trip, both ambiguous outcomes) and ``labels.change`` (W9
``bh-t082i``: ``labels.only-owned``), so every case runs for real and none is expected to fail.
Should a future writer land after its conformance case, the case is listed in
:data:`WRITER_CASES` and fails STRICTLY until it lands. Every case, including
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

#: Cases that need a writer that has not landed yet (removed as each writer lands).
WRITER_CASES: dict[str, str] = {
    # Empty: every writer the suite exercises has landed.
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


def test_with_no_write_enabled_the_adapter_has_no_write_method():
    subject = github_subject(enabled=frozenset())
    adapter = subject.adapter

    assert isinstance(adapter, TrackerAdapter)
    assert not isinstance(adapter, CommentWriter)
    assert not isinstance(adapter, LabelWriter)
    assert not hasattr(adapter, "post_comment") and not hasattr(adapter, "change_labels")
    supported = adapter.describe().supported  # what the package implements, enabled or not
    assert supported == {
        Operation.ISSUES_READ,
        Operation.COMMENTS_READ,
        Operation.COMMENT_CREATE,
        Operation.LABELS_CHANGE,
    }
    report = adapter.probe_capabilities()
    for op in (Operation.COMMENT_CREATE, Operation.LABELS_CHANGE):
        assert report.states[op].supported  # implemented, but neither enabled nor usable
        assert not report.states[op].enabled and not report.states[op].usable
    assert subject.world.write_requests() == 0


def test_the_comment_writer_exists_only_when_comment_create_is_enabled():
    subject = github_subject(enabled=frozenset({Operation.COMMENT_CREATE}))
    adapter = subject.adapter

    assert isinstance(adapter, TrackerAdapter) and isinstance(adapter, CommentWriter)
    assert Operation.COMMENT_CREATE in adapter.describe().supported
    state = adapter.probe_capabilities().states[Operation.COMMENT_CREATE]
    assert state.supported and state.enabled
    assert subject.world.write_requests() == 0  # building and probing never write


def test_the_label_writer_exists_only_when_labels_change_is_enabled():
    subject = github_subject(enabled=frozenset({Operation.LABELS_CHANGE}))
    adapter = subject.adapter

    assert isinstance(adapter, TrackerAdapter) and isinstance(adapter, LabelWriter)
    assert not isinstance(adapter, CommentWriter) and not hasattr(adapter, "post_comment")
    state = adapter.probe_capabilities().states[Operation.LABELS_CHANGE]
    assert state.supported and state.enabled
    assert subject.world.write_requests() == 0


def test_both_writers_compose_when_both_writes_are_enabled():
    enabled = frozenset({Operation.COMMENT_CREATE, Operation.LABELS_CHANGE})
    subject = github_subject(enabled=enabled)
    adapter = subject.adapter

    assert isinstance(adapter, CommentWriter) and isinstance(adapter, LabelWriter)
    report = adapter.probe_capabilities()
    for op in enabled:
        assert report.states[op].supported and report.states[op].enabled
    assert subject.world.write_requests() == 0
