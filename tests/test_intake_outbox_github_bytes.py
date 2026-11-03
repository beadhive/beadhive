"""Approved bytes == posted bytes over the production outbox wiring (bh-crf27).

``bh work outbox show`` renders the payload the operator approves by digest; ``send`` hands the
writer the planned body, and the first-party GitHub adapter's request body is
``join_marker_line(body, marker_line)``. A draft whose text ends in spaces or newlines used to be
shown as ``body + "\\n\\n" + line`` while the adapter posted the stripped join, so the approved
digest named bytes that were never posted. Here the draft runs through the real CLI and the real
``outbox_wiring`` (over ``MemoryTracker``, which records the body it was handed), and the posted
bytes are rebuilt with the GitHub adapter's own pure request-body builder.

No live GitHub, no clock, no sleep.
"""

from __future__ import annotations

import pytest
from tests.test_intake_outbox_wiring import COMMON, Wired, bh, doc

from beadhive import intake_services
from beadhive.modules.intake.domain import OpKind
from beadhive.modules.intake.domain.outbox import digest_of
from beadhive.modules.tracker.contracts import OwnMarker, join_marker_line


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Wired:
    return Wired(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    ("text", "shown_text"),
    [
        pytest.param("Thanks, fixed soon.   ", "Thanks, fixed soon.", id="trailing-spaces"),
        pytest.param("Thanks, fixed soon.\n\n\n", "Thanks, fixed soon.", id="trailing-newlines"),
    ],
)
def test_the_shown_digest_names_exactly_the_bytes_the_send_posts(rig, text, shown_text) -> None:
    comments = pytest.importorskip("beadhive_tracker_github.adapter.comments")
    report, issue = rig.report()
    oid = doc(bh("outbox", "draft", report, "--text", text, *COMMON))["results"][0]["outbox_id"]
    install = intake_services.install_id(rig.hive)
    marker = OwnMarker("acme/widget", install, oid)
    line = f"<!-- bh:v1 hive=acme/widget install={install} outbox={oid} -->"

    (render,) = doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))["renders"]
    assert render["payload"] == f"{shown_text}\n\n{line}"  # no trailing whitespace before it

    approved = bh("outbox", "approve", oid, "--op", f"comment:{render['digest']}", *COMMON)
    assert approved.exit_code == 0, approved.output
    sent = bh("outbox", "send", oid, *COMMON)
    assert sent.exit_code == 0 and doc(sent)["results"][0]["state"] == "sent"

    (posted,) = rig.world.comments.values()  # MemoryTracker keeps the body it was handed
    assert posted.marker == marker
    on_the_wire = comments.comment_payload(posted.body, posted.marker)["body"]
    assert on_the_wire == join_marker_line(posted.body, line) == render["payload"]
    assert digest_of(issue.url, OpKind.COMMENT, on_the_wire, None) == render["digest"]
