"""Refusal diagnostics: the exact port-ADR wording (bh-mqvdq table) and the port error each maps
to. Pure: status, headers and GitHub's ``message`` in; one scrubbed line out."""

from __future__ import annotations

import pytest

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    NotVisible,
    Operation,
    PayloadRejected,
    PermissionDenied,
    TargetStateRefused,
    TrackerError,
)
from beadhive_tracker_github.adapter.diagnostics import (
    ANONYMOUS,
    MESSAGE_LIMIT,
    diagnose,
    refusal_error,
)

SOURCE = "env:GITHUB_TOKEN"

TABLE = [
    (
        401,
        {},
        "Bad credentials",
        "401 bad credentials: the token from env:GITHUB_TOKEN is invalid, revoked or expired",
        AuthenticationFailed,
    ),
    (
        403,
        {"x-github-sso": "required; url=https://github.com/orgs/acme/sso?authorization_request=1"},
        "Resource protected by organization SAML enforcement.",
        "403 SSO: authorize this token for the organization (SAML SSO) and retry",
        PermissionDenied,
    ),
    (
        403,
        {"x-accepted-github-permissions": "issues=write"},
        "Resource not accessible by personal access token",
        "403 token lacks the permission (endpoint needs issues=write): "
        "Resource not accessible by personal access token",
        PermissionDenied,
    ),
    (
        403,
        {},
        "Resource not accessible by integration",
        "403 token lacks the permission: Resource not accessible by integration",
        PermissionDenied,
    ),
    (
        403,
        {"retry-after": "60"},
        "You have exceeded a secondary rate limit.",
        "403 rate limited (not a permission refusal): back off; do NOT mark writes off",
        None,  # decided by the retry layer, never a permission refusal
    ),
    (
        403,
        {"x-ratelimit-remaining": "0"},
        "API rate limit exceeded",
        "403 rate limited (not a permission refusal): back off; do NOT mark writes off",
        None,
    ),
    (
        403,
        {},
        "Repository was archived so is read-only.",
        "403 repository is archived (read-only)",
        TargetStateRefused,
    ),
    (
        403,
        {},
        "Unable to create comment because issue is locked.",
        "403 conversation is locked (not a credential problem)",
        TargetStateRefused,
    ),
    (
        404,
        {},
        "Not Found",
        "404: the repository or issue is invisible to this token (GitHub reports 'forbidden' "
        "as 'not found'); check the token's repository access and scope",
        NotVisible,
    ),
    (
        410,
        {},
        "Issues are disabled for this repo",
        "410 issues are disabled on this repository",
        TargetStateRefused,
    ),
    (410, {}, "This issue was deleted", "410 the issue was deleted", NotVisible),
    (
        422,
        {},
        "Validation Failed",
        "422 payload rejected after authorization (Validation Failed); NOT a capability failure",
        PayloadRejected,
    ),
    (418, {}, "", "418 unexpected: no message", TrackerError),
]


@pytest.mark.parametrize(("status", "headers", "message", "line", "error"), TABLE)
def test_each_refusal_gets_its_exact_one_line_diagnostic(status, headers, message, line, error):
    assert diagnose(status, headers, message, source=SOURCE) == line
    if error is None:
        return
    raised = refusal_error(status, headers, message, source=SOURCE, operation=Operation.ISSUES_READ)
    assert type(raised) is error and str(raised) == line


def test_the_permission_needed_is_carried_on_the_error():
    raised = refusal_error(
        403,
        {"x-accepted-github-permissions": "issues=write"},
        "Resource not accessible by personal access token",
        source=SOURCE,
        operation=Operation.COMMENT_CREATE,
    )
    assert isinstance(raised, PermissionDenied)
    assert raised.needed == "issues=write" and raised.operation is Operation.COMMENT_CREATE


def test_a_diagnostic_is_scrubbed_bounded_and_one_line():
    token = "ghp_" + "Q" * 36
    other = "github_pat_" + "R" * 40
    message = f"token {token}\nwas\trejected; also {other} " + "x" * 400
    line = diagnose(403, {}, message, source=SOURCE, secrets=(token,))
    assert token not in line and other not in line and "***" in line
    assert "\n" not in line and "\t" not in line
    assert len(line) <= len("403 refused: ") + MESSAGE_LIMIT


def test_an_anonymous_refusal_names_no_credential():
    line = diagnose(401, {}, "Requires authentication", source=None)
    assert ANONYMOUS in line
