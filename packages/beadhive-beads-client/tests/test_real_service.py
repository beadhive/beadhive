"""Opt-in conformance against one disposable, real Beads 1.3.0 service."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest

from beadhive_beads_client import (
    BeadsSession,
    ExpectedContext,
    IncompatibleService,
    IndeterminateWrite,
    LocalEndpoint,
    RemoteEndpoint,
    ServiceProblem,
)
from beads_v1_3.models import (
    AddCommentRequest,
    AddDependenciesRequest,
    ClaimNextRequest,
    ClaimRequest,
    CloseIssueRequest,
    CompareAndSetMetadataRequest,
    CreateIssueRequest,
    DependencyEdge,
    IssuePatchBody,
    ReleaseIssueRequest,
    RemoveDependencyRequest,
    ReopenIssueRequest,
    UpdateIssueRequest,
)
from beads_v1_3.types import UNSET

pytestmark = pytest.mark.real_service


class DropOnePatchResponse(httpx.BaseTransport):
    """Send a real PATCH, then lose exactly its response to model an ambiguous write."""

    def __init__(self) -> None:
        self.inner = httpx.HTTPTransport()
        self.dropped = False
        self.patch_count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        if request.method == "PATCH":
            self.patch_count += 1
        if request.method == "PATCH" and not self.dropped:
            self.dropped = True
            response.close()
            raise httpx.ReadTimeout("response deliberately lost after dispatch")
        return response

    def close(self) -> None:
        self.inner.close()


def test_real_v13_reads_mutations_and_reconciliation() -> None:
    url = os.environ.get("BEADS_V13_URL")
    project = os.environ.get("BEADS_V13_PROJECT_ID")
    database = os.environ.get("BEADS_V13_DATABASE")
    workspace = os.environ.get("BEADS_V13_WORKSPACE")
    if not all((url, project, database, workspace)):
        pytest.skip("set BEADS_V13_URL, PROJECT_ID, DATABASE and WORKSPACE for scratch service")
    assert url and project and database and workspace
    expected = ExpectedContext(project, database, repo_root=Path(workspace))
    endpoint = RemoteEndpoint(url)
    actor = "beadhive-v13-conformance"
    tag = uuid4().hex[:8]

    with BeadsSession(endpoint, expected) as session:
        assert session.context is not None
        assert session.context.bd_version == "1.3.0"
        with pytest.raises(ServiceProblem) as absent:
            session.get_issue("conf-does-not-exist")
        assert absent.value.problem.code == "not_found"

        first = session.create_issue(
            CreateIssueRequest(
                actor=actor, title=f"proof first {tag}", issue_type="task", priority=2
            )
        )
        second = session.create_issue(
            CreateIssueRequest(
                actor=actor, title=f"proof second {tag}", issue_type="task", priority=2
            )
        )
        third = session.create_issue(
            CreateIssueRequest(
                actor=actor, title=f"proof third {tag}", issue_type="task", priority=2
            )
        )
        assert {first.id, second.id, third.id} <= {
            row.id for row in session.list_issues(limit=100).items
        }
        page = session.list_issues(limit=1)
        assert page.has_more and page.next_cursor is not UNSET
        following = session.list_issues(limit=1, cursor=page.next_cursor)
        assert page.items[0].id != following.items[0].id

        edge = DependencyEdge(issue_id=second.id, depends_on_id=first.id, type_="blocks")
        assert session.add_dependencies(AddDependenciesRequest(actor=actor, edges=[edge])).added
        assert any(dep.issue_id == second.id for dep in session.list_dependencies(second.id).items)
        removed = session.remove_dependency(
            RemoveDependencyRequest(actor=actor, issue_id=second.id, depends_on_id=first.id)
        )
        assert removed.removed
        assert session.add_dependencies(AddDependenciesRequest(actor=actor, edges=[edge])).added

        before = session.get_issue(first.id)
        assert isinstance(before.revision, str)
        changed = session.update_issue(
            first.id,
            UpdateIssueRequest(
                actor=actor,
                patch=IssuePatchBody(title=f"proof changed {tag}"),
                expected_version=before.revision,
            ),
        )
        assert changed.changed and changed.revision != before.revision
        with pytest.raises(ServiceProblem) as conflict:
            session.update_issue(
                first.id,
                UpdateIssueRequest(
                    actor=actor,
                    patch=IssuePatchBody(title="stale"),
                    expected_version=before.revision,
                ),
            )
        assert conflict.value.problem.code == "precondition_failed"
        assert session.get_issue(first.id).title == f"proof changed {tag}"

        labelled = session.update_issue(
            first.id,
            UpdateIssueRequest(
                actor=actor,
                patch=IssuePatchBody(add_labels=[f"proof:{tag}"]),
                expected_version=changed.revision,
            ),
        )
        assert f"proof:{tag}" in session.get_issue(first.id).labels
        session.update_issue(
            first.id,
            UpdateIssueRequest(
                actor=actor,
                patch=IssuePatchBody(remove_labels=[f"proof:{tag}"]),
                expected_version=labelled.revision,
            ),
        )
        labels_after_remove = session.get_issue(first.id).labels
        assert labels_after_remove is UNSET or f"proof:{tag}" not in labels_after_remove

        swapped = session.compare_and_set_metadata(
            first.id,
            CompareAndSetMetadataRequest(actor=actor, key="proof.token", value=tag),
        )
        assert swapped.swapped and swapped.current == tag
        refused = session.compare_and_set_metadata(
            first.id,
            CompareAndSetMetadataRequest(
                actor=actor, key="proof.token", expected="stale", value="wrong"
            ),
        )
        assert not refused.swapped and refused.current == tag
        assert session.get_issue(first.id).metadata.to_dict()["proof.token"] == tag

        comment = session.add_comment(
            first.id,
            AddCommentRequest(author=actor, text=f"review feedback {tag}"),
        )
        assert comment.issue_id == first.id and comment.author == actor
        with_comments = session.get_issue(first.id, include_comments=True)
        assert any(
            row.id == comment.id and row.text == comment.text for row in with_comments.comments
        )

        claimed = session.claim_issue(first.id, ClaimRequest(actor=actor))
        assert claimed.issue.assignee == actor
        next_claim = session.claim_next(ClaimNextRequest(actor=actor))
        assert next_claim.claimed is not UNSET
        assert next_claim.claimed.id != second.id  # blocked by first until it closes

        released = session.release_issue(first.id, ReleaseIssueRequest(actor=actor))
        assert released.changed
        assert released.issue.status == "open"
        assert released.issue.assignee is UNSET

        revision = session.get_issue(first.id).revision
        closed = session.close_issue(
            first.id, CloseIssueRequest(actor=actor, reason="proof", expected_version=revision)
        )
        assert closed.issue.status == "closed"
        opened = session.reopen_issue(
            first.id, ReopenIssueRequest(actor=actor, expected_version=closed.revision)
        )
        assert opened.issue.status == "open"

        # A lost response is indeterminate even though the server committed.
        dropped = DropOnePatchResponse()
        with BeadsSession(endpoint, expected, transport=dropped) as uncertain:
            current = uncertain.get_issue(first.id)
            with pytest.raises(IndeterminateWrite):
                uncertain.update_issue(
                    first.id,
                    UpdateIssueRequest(
                        actor=actor,
                        patch=IssuePatchBody(notes=f"reconcile {tag}"),
                        expected_version=current.revision,
                    ),
                )
            assert dropped.dropped
            assert dropped.patch_count == 1
            assert uncertain.get_issue(first.id).notes == f"reconcile {tag}"

        contested = session.create_issue(
            CreateIssueRequest(
                actor=actor, title=f"proof contested {tag}", issue_type="task", priority=2
            )
        )
        barrier = Barrier(2)

        def contest(who: str) -> str:
            with BeadsSession(endpoint, expected) as claimant:
                barrier.wait(timeout=5)
                try:
                    claimant.claim_issue(contested.id, ClaimRequest(actor=who))
                except ServiceProblem as refusal:
                    return refusal.problem.code
                return "claimed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(contest, ("claimant-a", "claimant-b")))
        assert sorted(outcomes) == ["already_claimed", "claimed"]

    # Beads' own audit reader proves caller-supplied actor attribution. The
    # durable events journal is disabled by default; legacy audit events remain.
    result = subprocess.run(
        ["bd", "-C", workspace, "history", first.id, "--events"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert actor in result.stdout


def test_real_v13_authenticated_context() -> None:
    url = os.environ.get("BEADS_V13_AUTH_URL")
    token = os.environ.get("BEADS_V13_AUTH_TOKEN")
    project = os.environ.get("BEADS_V13_PROJECT_ID")
    database = os.environ.get("BEADS_V13_DATABASE")
    if not all((url, token, project, database)):
        pytest.skip("set authenticated scratch service URL and token")
    assert url and token and project and database
    expected = ExpectedContext(project, database)
    with pytest.raises(IncompatibleService, match="did not become ready"):
        BeadsSession(RemoteEndpoint(url), expected).open()
    with pytest.raises(IncompatibleService, match="identity mismatch"):
        BeadsSession(RemoteEndpoint(url, token), ExpectedContext("wrong-project", database)).open()
    with BeadsSession(RemoteEndpoint(url, token), expected) as session:
        assert session.context is not None
        assert session.context.project_id == project


def test_real_v13_local_supervision(monkeypatch: pytest.MonkeyPatch) -> None:
    project = os.environ.get("BEADS_V13_PROJECT_ID")
    database = os.environ.get("BEADS_V13_DATABASE")
    workspace = os.environ.get("BEADS_V13_WORKSPACE")
    executable = shutil.which("bd")
    if not all((project, database, workspace, executable)):
        pytest.skip("set scratch workspace and install bd for supervised service proof")
    assert project and database and workspace and executable
    for name in ("BEADS_DOLT_SHARED_SERVER", "BEADS_SHARED_SERVER_DIR", "BEADS_DOLT_SERVER_PORT"):
        monkeypatch.delenv(name, raising=False)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with BeadsSession(
        LocalEndpoint(Path(workspace), port=port, bd_executable=Path(executable)),
        ExpectedContext(project, database, repo_root=Path(workspace)),
    ) as session:
        assert session.list_ready().items is not None
        process = session._process
        assert process is not None and process.poll() is None
    assert process.poll() is not None
