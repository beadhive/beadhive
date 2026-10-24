# bh-mqvdq prototype artifacts

Research prototype for `docs/spikes/bh-mqvdq-github-identity.md`. **Never imported by `src/`.**
Only HTTP `GET` is ever issued. Run in a throwaway venv, never the project env:

```sh
uv venv /tmp/spikevenv && uv pip install --python /tmp/spikevenv/bin/python httpx pytest
cd docs/spikes/artifacts/bh-mqvdq
/tmp/spikevenv/bin/python -m pytest -c /dev/null --rootdir=. -p no:cacheprovider -q test_identity_probe.py
```

## Live read-only probe script (GET only; the token is never printed)

```sh
# Spike bh-mqvdq: the live READ-ONLY probes behind the Evidence section. GET only (`gh api` with no
# -X/-f/-F issues GET). The token is never printed: only response headers on an allowlist and
# selected non-secret JSON fields are shown. Run with a throwaway/low-privilege credential.
set -euo pipefail
H='^(HTTP|x-oauth-scopes|x-accepted|x-github-api-version-selected)'
echo "--- /user (id, type only)";  gh api user --jq '{id,type}'
echo "--- /user headers";          gh api -i user | grep -iE "$H"
echo "--- repo + permissions";     gh api repos/cli/cli --jq '{permissions,archived,has_issues}'
echo "--- repo headers";           gh api -i repos/cli/cli | grep -iE "$H"
echo "--- bad credential (fake)";  GH_TOKEN="ghp_$(printf '0%.0s' $(seq 36))" gh api -i user 2>&1 | grep -iE '^HTTP|message' || true
echo "--- bot actor by slug";      gh api 'users/github-actions%5Bbot%5D' --jq '{id,type}'
```

Recorded output (non-secret fields only):

```text
--- /user (id, type only)
{"id":<operator numeric id>,"type":"User"}
--- /user headers
HTTP/2.0 200 OK
X-Accepted-Oauth-Scopes: 
X-Github-Api-Version-Selected: 2022-11-28
X-Oauth-Scopes: admin:public_key, admin:ssh_signing_key, gist, read:org, repo
--- repo + permissions
{"archived":false,"has_issues":true,"permissions":{"admin":false,"maintain":false,"pull":true,"push":false,"triage":false}}
--- repo headers
HTTP/2.0 200 OK
X-Accepted-Oauth-Scopes: repo
X-Github-Api-Version-Selected: 2022-11-28
X-Oauth-Scopes: admin:public_key, admin:ssh_signing_key, gist, read:org, repo
--- bad credential (fake)
HTTP/2.0 401 Unauthorized
  "message": "Bad credentials",
--- bot actor by slug
{"id":41898282,"type":"Bot"}
```

## Offline test run (`httpx.MockTransport`, no network, no sleeps)

```text
============================= test session starts ==============================
platform linux -- Python 3.14.6, pytest-9.1.1, pluggy-1.7.0 -- <venv>/bin/python
rootdir: <worktree>
configfile: ../../../../../../../../../../dev/null
plugins: anyio-4.15.1
collecting ... collected 35 items

test_identity_probe.py::test_resolution_order_github_token_then_gh_token_then_gh_auth_token PASSED [  2%]
test_identity_probe.py::test_token_never_appears_in_repr_str_logs_errors_or_serialised_output PASSED [  5%]
test_identity_probe.py::test_scrub_replaces_longest_first_and_by_pattern PASSED [  8%]
test_identity_probe.py::test_kind_prefix_is_a_hint_and_headers_are_the_evidence PASSED [ 11%]
test_identity_probe.py::test_actor_equality_ignores_a_renamed_login PASSED [ 14%]
test_identity_probe.py::test_user_probe_for_user_bad_credentials_and_installation_token PASSED [ 17%]
test_identity_probe.py::test_app_bot_actor_is_resolved_from_the_public_user_not_from_user PASSED [ 20%]
test_identity_probe.py::test_classic_pat_with_repo_scope_is_yes PASSED   [ 22%]
test_identity_probe.py::test_classic_pat_public_repo_scope_covers_public_but_not_private PASSED [ 25%]
test_identity_probe.py::test_classic_pat_without_repo_scopes_is_no PASSED [ 28%]
test_identity_probe.py::test_label_needs_triage_but_comment_does_not_on_a_public_repo PASSED [ 31%]
test_identity_probe.py::test_fine_grained_token_is_unknown_never_yes PASSED [ 34%]
test_identity_probe.py::test_fine_grained_token_with_a_role_below_triage_is_a_provable_no_for_label PASSED [ 37%]
test_identity_probe.py::test_definitive_no_cases[response0-401] PASSED   [ 40%]
test_identity_probe.py::test_definitive_no_cases[response1-404] PASSED   [ 42%]
test_identity_probe.py::test_definitive_no_cases[response2-archived] PASSED [ 45%]
test_identity_probe.py::test_definitive_no_cases[response3-issues are disabled] PASSED [ 48%]
test_identity_probe.py::test_definitive_no_cases[response4-no credential] PASSED [ 51%]
test_identity_probe.py::test_unexpected_status_is_unknown_not_no PASSED  [ 54%]
test_identity_probe.py::test_first_refused_write_diagnostic[401-Bad credentials-headers0-env:GH_TOKEN] PASSED [ 57%]
test_identity_probe.py::test_first_refused_write_diagnostic[403-Resource not accessible by personal access token-headers1-issues=write] PASSED [ 60%]
test_identity_probe.py::test_first_refused_write_diagnostic[403-Resource not accessible by integration-headers2-token lacks the permission] PASSED [ 62%]
test_identity_probe.py::test_first_refused_write_diagnostic[403-Resource protected by organization SAML enforcement.-headers3-SSO] PASSED [ 65%]
test_identity_probe.py::test_first_refused_write_diagnostic[403-You have exceeded a secondary rate limit.-headers4-rate limited] PASSED [ 68%]
test_identity_probe.py::test_first_refused_write_diagnostic[403-Repository was archived so is read-only.-headers5-archived] PASSED [ 71%]
test_identity_probe.py::test_first_refused_write_diagnostic[403-Unable to create comment because issue is locked.-headers6-locked] PASSED [ 74%]
test_identity_probe.py::test_first_refused_write_diagnostic[404-Not Found-headers7-invisible] PASSED [ 77%]
test_identity_probe.py::test_first_refused_write_diagnostic[410-Issues are disabled for this repo-headers8-disabled] PASSED [ 80%]
test_identity_probe.py::test_first_refused_write_diagnostic[422-Validation Failed-headers9-NOT a capability failure] PASSED [ 82%]
test_identity_probe.py::test_stamped_comment_by_the_known_actor_is_own_by_marker PASSED [ 85%]
test_identity_probe.py::test_human_using_the_same_token_without_marker_is_not_own PASSED [ 88%]
test_identity_probe.py::test_marker_from_another_actor_or_hive_or_quoted_in_the_middle_is_not_own PASSED [ 91%]
test_identity_probe.py::test_outbox_id_wins_even_after_a_human_edit_stripped_the_marker_or_the_token_changed PASSED [ 94%]
test_identity_probe.py::test_app_installation_comments_are_own_by_app_id_and_bot_type PASSED [ 97%]
test_identity_probe.py::test_login_rename_does_not_change_recognition PASSED [100%]

============================== 35 passed in 0.54s ==============================
```
