"""Opt-in conformance: the HTTP writes approve and bounce perform, against a real Beads 1.3.0.

Only the Beads HTTP writes are under test here — the review-label cleanup (guarded update) and
the bounce feedback comment — together with the actor each is attributed to. Gates and state
dimensions have no v1.3 HTTP route; they stay fake ports, exactly as in the policy tests.

Run against the supervised service of a disposable scratch hive (never a managed hive), started
the only supported way — ``bh host beads start --hive <scratch>`` — and resolved exactly as the
shell resolves it, from the published endpoint record (the test never starts ``bd serve``)::

    BEADS_SERVICE_DIR=$BH_HOME/run/beads-serve/<sanitized scratch hive key> \
    uv run --locked --all-packages pytest packages/beadhive-core/tests -m real_service

The label write's audit actor is read back with ``bd history --events`` in that scratch
workspace (the HTTP events journal is disabled by default); a comment records no audit event,
so its stored author is the attribution checked.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from beadhive_beads_client import BeadsSession
from beadhive_beads_client import service as beads_service
from beadhive_core import REVIEW_CAPABILITIES, Gate, ReviewCommands
from beads_v1_3.models import CreateIssueRequest, IssuePatchBody, UpdateIssueRequest

pytestmark = pytest.mark.real_service


class OneReviewGate:
    """A fake gate port: the gate route is CLI compatibility, not part of this proof."""

    def __init__(self, bead: str) -> None:
        self.gate = Gate("gate-fake", "open", f"blocks {bead}\n\nReason: bh:review abc1234")
        self.resolved: list[tuple[str, str, str]] = []

    def gates_for(self, bead: str) -> list[Gate]:
        return [self.gate] if not self.resolved else []

    def resolve(self, gate_id: str, *, reason: str, actor: str) -> None:
        self.resolved.append((gate_id, reason, actor))


class RecordedStates:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None:
        self.calls.append((dimension, value, actor))


def _audit(bd: str, workspace: Path, bead: str) -> list[dict[str, str]]:
    result = subprocess.run(
        [bd, "-C", str(workspace), "history", bead, "--events", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _published_spec(directory: Path) -> beads_service.ServiceSpec:
    """Rebuild the consumer-side spec from the record ``bh host beads start`` published."""
    paths = beads_service.ServicePaths(directory)
    record = beads_service.EndpointRecord.from_payload(json.loads(paths.record.read_text()))
    return beads_service.ServiceSpec(
        workspace=record.workspace,
        repo_root=Path(record.repo_root),
        project_id=record.project_id,
        database=record.database,
        bd_executable=Path(record.bd_executable),
        paths=paths,
        start_command=f"bh host beads start --hive {record.workspace}",
    )


def test_real_v13_review_writes_carry_the_reviewer_actor() -> None:
    directory = os.environ.get("BEADS_SERVICE_DIR")
    if not directory:
        pytest.skip("set BEADS_SERVICE_DIR to a scratch hive's `bh host beads start` directory")
    spec = _published_spec(Path(directory))

    def session() -> BeadsSession:
        # Resolution fails closed (ServiceUnavailable) if the service is absent or stale.
        endpoint = beads_service.resolve(spec, REVIEW_CAPABILITIES)
        return BeadsSession(endpoint, spec.expected(REVIEW_CAPABILITIES))

    tag = uuid4().hex[:8]
    author, reviewer = f"dev/author-{tag}", f"rev/reviewer-{tag}"

    with session() as setup:
        issue = setup.create_issue(
            CreateIssueRequest(
                actor=author, title=f"review proof {tag}", issue_type="task", priority=2
            )
        )
        setup.update_issue(
            issue.id,
            UpdateIssueRequest(
                actor=author,
                patch=IssuePatchBody(add_labels=["review:pending"], assignee=author),
                expected_version=setup.get_issue(issue.id).revision,
            ),
        )

    gates, states = OneReviewGate(issue.id), RecordedStates()
    approved = ReviewCommands(session, gates, states).approve(issue.id, reviewer)
    assert approved.gates == ("gate-fake",)
    assert states.calls == []

    gates = OneReviewGate(issue.id)
    feedback = f"changes requested by {reviewer}: tighten the proof {tag}"
    ReviewCommands(session, gates, states).bounce(issue.id, reviewer, f"tighten the proof {tag}")
    assert states.calls == [("review", "changes-requested", reviewer)]

    with session() as readback:
        detail = readback.get_issue(issue.id, include_comments=True)
    assert "review:pending" not in (detail.labels or [])
    # A v1.3 comment writes no audit event; its stored author IS the attribution record.
    assert [(c.author, c.text) for c in detail.comments or []] == [(reviewer, feedback)]

    events = _audit(str(spec.bd_executable), spec.repo_root, issue.id)
    removed = [e for e in events if e["event_type"] == "label_removed"]
    assert [(e["actor"], e["old_value"]) for e in removed] == [(reviewer, "review:pending")]
