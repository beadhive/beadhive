"""The dry run renders, leak-checks and digests exactly the bytes a comment write posts (bh-crf27).

The operator approves the digest of ``render``; the writer is handed ``SendPlan.body`` and posts
``join_marker_line(body, marker_line)``, the tracker port's one joining rule (trailing whitespace
removed, one blank line, the marker line; an empty body posts the line alone). The outbox domain
may not import the port (X2), so the application injects that same function into
:class:`RenderContext`; these cases pin that the render payload, the bytes the leak check scans,
the approved digest and the bytes posted for the planned body are one and the same, for bodies with
trailing spaces, trailing newlines and an empty body. Pure: no tracker, no store, no clock.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.domain import (
    ApprovalRequest,
    Capabilities,
    DraftRequest,
    IssueBinding,
    LabelBase,
    LeakPolicy,
    Message,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxEvent,
    OutboxPolicy,
    PublicFacts,
    RenderContext,
)
from beadhive.modules.intake.domain import outbox as ob
from beadhive.modules.tracker.contracts import join_marker_line
from beadhive.testing.intake import RecordingSanitizer, SequentialIdSource

URL = "https://tracker.example/acme/widgets/issues/7"
BINDING = IssueBinding("acme-r1", "memory", "acme/widgets", URL, "#7")
POLICY = LeakPolicy(bead_prefixes=frozenset({"acme"}), owned_label_prefixes=("beadhive:",))
ON = OutboxPolicy(enabled=True)
ALL = Capabilities(frozenset({"comment.create", "comments.read"}))
LINE = "<!-- bh:v1 hive=acme/widgets install=inst-1 outbox=ob1_x -->"
C = OpKind.COMMENT

#: ``(body, the exact bytes posted for it)``; the expected bytes are spelled out, not computed.
CASES = [
    pytest.param("Shipped in v1.2.   ", f"Shipped in v1.2.\n\n{LINE}", id="trailing-spaces"),
    pytest.param("Shipped in v1.2.\n\n\n", f"Shipped in v1.2.\n\n{LINE}", id="trailing-newlines"),
    pytest.param("Shipped.\r\n \t\n", f"Shipped.\n\n{LINE}", id="mixed-trailing-whitespace"),
    pytest.param("  indented\n\nkept  \n", f"  indented\n\nkept\n\n{LINE}", id="inner-kept"),
    pytest.param("", LINE, id="empty"),
    pytest.param(" \n\t", LINE, id="whitespace-only"),
]


def ctx(sanitizer: RecordingSanitizer, **changes) -> RenderContext:
    base = RenderContext(
        URL,
        POLICY,
        sanitizer,
        marker_line=LINE,
        labels=LabelBase("r1", frozenset()),
        join_marker_line=join_marker_line,
    )
    return dataclasses.replace(base, **changes)


def drafted(body: str, render: RenderContext) -> tuple[OutboxDocument, str]:
    """A drafted comment whose stored text is ``body``. Drafting refuses a blank comment, so a
    blank body is set on the stored record afterwards: the render must still agree with the
    writer on it."""
    oid = SequentialIdSource().outbox_id()
    request = DraftRequest(
        BINDING, OutboxEvent.MERGED, "c1", "outbox", PublicFacts(), Message("merged", "draft")
    )
    doc = ob.draft(OutboxDocument(), request, outbox_id=oid, policy=ON, ctx=render).document
    record = dataclasses.replace(doc.records[oid], message=Message("merged", body))
    return dataclasses.replace(doc, records={**doc.records, oid: record}), oid


@pytest.mark.parametrize(("body", "posted"), CASES)
def test_the_render_is_the_posted_bytes_and_the_leak_check_and_digest_run_over_them(
    body: str, posted: str
) -> None:
    sanitizer = RecordingSanitizer()
    render = ctx(sanitizer)
    doc, oid = drafted(body, render)
    sanitizer.scanned.clear()

    rendered = ob.render(doc.records[oid], C, render)

    assert rendered.body == body  # the writer is handed the body unchanged
    assert join_marker_line(rendered.body, LINE) == posted  # what the writer posts for it
    assert rendered.payload == posted  # what the operator reviews
    assert sanitizer.scanned == [posted]  # the leak check scans exactly those bytes
    assert rendered.digest == ob.digest_of(URL, C, posted, None)  # and the digest names them


@pytest.mark.parametrize(("body", "posted"), CASES)
def test_the_approved_digest_and_the_send_plan_name_the_same_posted_bytes(
    body: str, posted: str
) -> None:
    sanitizer = RecordingSanitizer()
    render = ctx(sanitizer)
    doc, oid = drafted(body, render)
    shown = ob.render(doc.records[oid], C, render)
    approved = ob.approve(
        doc,
        ApprovalRequest(oid, C, shown.digest, "alice"),
        grant_id=SequentialIdSource().grant_id(),
        policy=ON,
        ctx=render,
        capabilities=ALL,
    ).document
    sanitizer.scanned.clear()

    sent = ob.begin_send(
        approved, oid, C, actor="alice", policy=ON, ctx=render, epoch=1, holder="h"
    )

    assert sent.record is not None and sent.record.op(C).state is OpState.SENDING
    assert sent.plan is not None
    assert join_marker_line(sent.plan.body, LINE) == posted  # the bytes the write posts
    assert sent.record.op(C).approval.digest == ob.digest_of(URL, C, posted, None)
    assert sanitizer.scanned == [posted]  # O9 re-checks the posted bytes, nothing else


def test_a_marker_line_without_the_ports_join_refuses_to_render() -> None:
    """Fail closed: the domain never falls back to a joining rule of its own."""
    render = ctx(RecordingSanitizer())
    doc, oid = drafted("Shipped.", render)

    with pytest.raises(ValueError, match="join_marker_line"):
        ob.render(doc.records[oid], C, dataclasses.replace(render, join_marker_line=None))


def test_without_a_marker_line_the_payload_is_the_body() -> None:
    """A draft rendered before any tracker is wired (no codec, so no marker) is not a post."""
    render = ctx(RecordingSanitizer(), marker_line="", join_marker_line=None)
    doc, oid = drafted("Shipped.  ", render)

    assert ob.render(doc.records[oid], C, render).payload == "Shipped.  "
