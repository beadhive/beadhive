"""Offline tests for the spike prototype. No network: every HTTP exchange is an
httpx.MockTransport fixture. No sleeps, no clocks."""

from __future__ import annotations

import json
import logging

import httpx
import identity_probe as ip
import pytest

# Planted, obviously fake. Shaped like real tokens so the PATTERN scrub is exercised too.
PLANTED = "ghp_" + "A1b2C3d4" * 5
OTHER = "github_pat_" + "Z9y8X7w6" * 4


def make_client(routes: dict[tuple[str, str], httpx.Response], seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path))
        try:
            return routes[(request.method, request.url.path)]
        except KeyError:
            return httpx.Response(599, json={"message": f"unrouted {request.url.path}"})

    return httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))


def repo(perms, *, private=False, archived=False, issues=True, headers=None):
    body = {"private": private, "archived": archived, "has_issues": issues}
    if perms is not None:
        body["permissions"] = perms
    return httpx.Response(200, json=body, headers=headers or {})


WRITE = {"admin": False, "maintain": False, "push": True, "triage": True, "pull": True}
READ = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}


# ---- credential resolution ------------------------------------------------------------------


def test_resolution_order_github_token_then_gh_token_then_gh_auth_token():
    asked = []

    def gh():
        asked.append(1)
        return "from-gh"

    env = {"GITHUB_TOKEN": "a", "GH_TOKEN": "b"}
    assert ip.resolve_token(env, gh).source == "env:GITHUB_TOKEN"
    assert ip.resolve_token({"GITHUB_TOKEN": "  ", "GH_TOKEN": "b"}, gh).source == "env:GH_TOKEN"
    assert asked == []  # gh is consulted only when neither variable carries a value
    got = ip.resolve_token({}, gh)
    assert (got.source, got.value, asked) == ("gh-auth-token", "from-gh", [1])
    assert ip.resolve_token({}, lambda: None) is None


# ---- scrubbing: the planted-token bar --------------------------------------------------------


def test_token_never_appears_in_repr_str_logs_errors_or_serialised_output(caplog, tmp_path):
    tok = ip.ResolvedToken(PLANTED, "env:GITHUB_TOKEN")
    assert PLANTED not in repr(tok) and PLANTED not in str(tok) and PLANTED not in f"{tok}"

    # An upstream error body that ECHOES the token (and a second, unknown one) is scrubbed.
    body = {"message": f"Bad token {PLANTED}; also saw {OTHER}"}
    res = httpx.Response(403, json=body, headers={"x-accepted-github-permissions": "issues=write"})
    line = ip.diagnose_refused_write(res, source=tok.source)
    assert PLANTED not in line and OTHER not in line and ip.REDACTED in line

    # What the adapter logs and what it would persist is built only from states + provenance.
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("adapter").warning("write refused: %s (token %s)", line, tok)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"source": tok.source, "diagnostic": line}))
    haystack = caplog.text + ledger.read_text()
    assert PLANTED not in haystack and OTHER not in haystack
    assert not ip.TOKEN_PATTERN.search(haystack)  # the structural bar: no token-SHAPED text at all


def test_scrub_replaces_longest_first_and_by_pattern():
    short, long_ = "tok12345", "tok12345-tail"
    assert ip.scrub(f"x {long_} y", [short, long_]) == "x *** y"  # no '-tail' residue
    assert ip.scrub(f"echo {OTHER}") == "echo ***"  # unknown token caught by shape alone


def test_kind_prefix_is_a_hint_and_headers_are_the_evidence():
    assert ip.classify_by_prefix(PLANTED) == "classic-pat"
    assert ip.classify_by_prefix(OTHER) == "fine-grained-pat"
    assert ip.classify_by_prefix("gho_" + "x" * 36) == "oauth"
    assert ip.classify_by_prefix("ghs_" + "x" * 36) == "app-installation"
    assert ip.classify_by_prefix("a" * 40) == "classic-pat"
    assert ip.kind_from_headers(httpx.Headers({"x-oauth-scopes": ""})) == "scoped"
    assert ip.kind_from_headers(httpx.Headers({})) == "unscoped"


# ---- actor discovery -------------------------------------------------------------------------


def test_actor_equality_ignores_a_renamed_login():
    before = ip.Actor(11590115, "User", "old-name")
    after = ip.Actor(11590115, "User", "new-name")
    assert before == after and before.key == after.key
    assert ip.Actor(11590115, "Bot") != before  # type participates


def test_user_probe_for_user_bad_credentials_and_installation_token():
    ok = make_client(
        {("GET", "/user"): httpx.Response(200, json={"id": 7, "type": "User", "login": "me"})}
    )
    assert ip.discover_actor(ok) == ip.Actor(7, "User")
    bad = make_client({("GET", "/user"): httpx.Response(401, json={"message": "Bad credentials"})})
    with pytest.raises(ip.ProbeUnavailable, match="401"):
        ip.discover_actor(bad)
    inst = make_client(
        {
            ("GET", "/user"): httpx.Response(
                403, json={"message": "Resource not accessible by integration"}
            )
        }
    )
    assert ip.discover_actor(inst) is None  # an installation token cannot call /user


def test_app_bot_actor_is_resolved_from_the_public_user_not_from_user():
    client = make_client(
        {
            ("GET", "/users/my-app[bot]"): httpx.Response(
                200, json={"id": 99, "type": "Bot", "login": "my-app[bot]"}
            )
        }
    )
    # httpx normalises %5B/%5D in the decoded path, so the route key is the decoded form.
    assert ip.app_bot_actor(client, "my-app") == ip.Actor(99, "Bot")


# ---- capability probe: classic / OAuth -------------------------------------------------------


def cap(response, op="comment", private=False):
    seen: list = []
    client = make_client({("GET", "/repos/o/r"): response}, seen)
    result = ip.probe_capability(client, "o", "r", op)
    assert {m for m, _ in seen} == {"GET"}  # the probe is GET-only, always
    return result


def test_classic_pat_with_repo_scope_is_yes():
    r = cap(repo(WRITE, headers={"x-oauth-scopes": "gist, repo"}))
    assert r.state is ip.Tri.YES


def test_classic_pat_public_repo_scope_covers_public_but_not_private():
    assert cap(repo(WRITE, headers={"x-oauth-scopes": "public_repo"})).state is ip.Tri.YES
    private = cap(repo(WRITE, private=True, headers={"x-oauth-scopes": "public_repo"}))
    assert private.state is ip.Tri.NO


def test_classic_pat_without_repo_scopes_is_no():
    assert cap(repo(WRITE, headers={"x-oauth-scopes": "read:org"})).state is ip.Tri.NO
    assert cap(repo(WRITE, headers={"x-oauth-scopes": ""})).state is ip.Tri.NO


def test_label_needs_triage_but_comment_does_not_on_a_public_repo():
    h = {"x-oauth-scopes": "repo"}
    assert cap(repo(READ, headers=h), op="comment").state is ip.Tri.YES
    assert cap(repo(READ, headers=h), op="label").state is ip.Tri.NO


# ---- capability probe: fine-grained / installation (unscoped) --------------------------------


def test_fine_grained_token_is_unknown_never_yes():
    r = cap(repo(WRITE))  # no x-oauth-scopes header at all
    assert r.state is ip.Tri.UNKNOWN and "first write" in r.needs


def test_fine_grained_token_with_a_role_below_triage_is_a_provable_no_for_label():
    assert cap(repo(READ), op="label").state is ip.Tri.NO  # role below triage is still provable


@pytest.mark.parametrize(
    ("response", "needle"),
    [
        (httpx.Response(401, json={"message": "Bad credentials"}), "401"),
        (httpx.Response(404, json={"message": "Not Found"}), "404"),
        (repo(WRITE, archived=True, headers={"x-oauth-scopes": "repo"}), "archived"),
        (repo(WRITE, issues=False, headers={"x-oauth-scopes": "repo"}), "issues are disabled"),
        (repo(None, headers={}), "no credential"),
    ],
)
def test_definitive_no_cases(response, needle):
    r = cap(response)
    assert r.state is ip.Tri.NO and needle in r.reason


def test_unexpected_status_is_unknown_not_no():
    assert cap(httpx.Response(502, json={})).state is ip.Tri.UNKNOWN


# ---- first refused write diagnostics ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message", "headers", "needle"),
    [
        (401, "Bad credentials", {}, "env:GH_TOKEN"),
        (
            403,
            "Resource not accessible by personal access token",
            {"x-accepted-github-permissions": "issues=write"},
            "issues=write",
        ),
        (403, "Resource not accessible by integration", {}, "token lacks the permission"),
        (
            403,
            "Resource protected by organization SAML enforcement.",
            {"x-github-sso": "required; url=https://x"},
            "SSO",
        ),
        (403, "You have exceeded a secondary rate limit.", {"retry-after": "60"}, "rate limited"),
        (403, "Repository was archived so is read-only.", {}, "archived"),
        (403, "Unable to create comment because issue is locked.", {}, "locked"),
        (404, "Not Found", {}, "invisible"),
        (410, "Issues are disabled for this repo", {}, "disabled"),
        (422, "Validation Failed", {}, "NOT a capability failure"),
    ],
)
def test_first_refused_write_diagnostic(status, message, headers, needle):
    res = httpx.Response(status, json={"message": message}, headers=headers)
    assert needle in ip.diagnose_refused_write(res, source="env:GH_TOKEN")


# ---- own-comment recognition ------------------------------------------------------------------

ME = ip.Actor(7, "User", "shared-login")
HIVE = "beadhive/beadhive"
INSTALL = "3f2a9c1e-1111-4222-8333-444455556666"


def comment(cid, uid, utype, body, app=None):
    return {
        "id": cid,
        "user": {"id": uid, "type": utype, "login": "x"},
        "body": body,
        "performed_via_github_app": app,
    }


KNOWN = frozenset({ME.key})


def test_stamped_comment_by_the_known_actor_is_own_by_marker():
    body = ip.stamp("Triaged: tracking as bh-123.", HIVE, INSTALL)
    assert ip.classify_comment(
        comment(1, 7, "User", body), actors=KNOWN, hive=HIVE
    ) == ip.Provenance(True, "marker")


def test_human_using_the_same_token_without_marker_is_not_own():
    got = ip.classify_comment(
        comment(2, 7, "User", "I commented by hand."), actors=KNOWN, hive=HIVE
    )
    assert got == ip.Provenance(False, "same-actor-no-marker")  # user.id matched: necessary only


def test_marker_from_another_actor_or_hive_or_quoted_in_the_middle_is_not_own():
    stamped = ip.stamp("hello", HIVE, INSTALL)
    assert not ip.classify_comment(comment(3, 8, "User", stamped), actors=KNOWN, hive=HIVE).own
    other_hive = ip.stamp("hello", "someone/else", INSTALL)
    assert not ip.classify_comment(comment(4, 7, "User", other_hive), actors=KNOWN, hive=HIVE).own
    quoted = (
        f"> {ip.marker(HIVE, INSTALL)}\n\nmy reply"  # human quoting the bot: marker not last line
    )
    assert not ip.classify_comment(comment(5, 7, "User", quoted), actors=KNOWN, hive=HIVE).own


def test_outbox_id_wins_even_after_a_human_edit_stripped_the_marker_or_the_token_changed():
    got = ip.classify_comment(
        comment(6, 12345, "User", "edited, marker gone"),
        actors=KNOWN,
        hive=HIVE,
        outbox_ids=frozenset({6}),
    )
    assert got == ip.Provenance(True, "outbox")


def test_app_installation_comments_are_own_by_app_id_and_bot_type():
    bot = ip.Actor(99, "Bot", "my-app[bot]")
    known = frozenset({bot.key})
    app = {"id": 555, "slug": "my-app"}
    assert ip.classify_comment(
        comment(7, 99, "Bot", "no marker", app), actors=known, hive=HIVE, app_id=555
    ) == ip.Provenance(True, "app")
    # a different App's bot, or the right app id on a non-Bot user, is not accepted
    assert not ip.classify_comment(
        comment(8, 100, "Bot", "x", {"id": 555}), actors=known, hive=HIVE, app_id=555
    ).own
    assert not ip.classify_comment(
        comment(9, 99, "User", "x", app), actors=known, hive=HIVE, app_id=555
    ).own


def test_login_rename_does_not_change_recognition():
    renamed = comment(10, 7, "User", ip.stamp("x", HIVE, INSTALL))
    renamed["user"]["login"] = "brand-new-login"
    assert ip.classify_comment(renamed, actors=KNOWN, hive=HIVE).own
