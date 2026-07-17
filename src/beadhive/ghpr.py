"""GitHub PR plumbing for PR-governed landings (`work.landing: pr`) — one place, on purpose.

Owned-GitHub-repo, PR-only-main case (bh-v0wu): pushing a branch and opening/inspecting the PR
that lands it. Kept as its own tiny module so the adjacent PR machinery (contribution plane
bh-uxam, guided Gitea sign-off bh-aa5b) can grow beside or on top of it without entangling
work.py / worktree.py.

Seams: every `gh` invocation flows through this module's ``run`` symbol so tests fake the CLI by
patching ``ghpr.run`` (the same per-module convention as ``work.run`` / ``worktree.run``). `gh`
is never required at import time — availability is probed with ``shutil.which`` at call time, and
every probe is best-effort: a missing gh / non-GitHub hive / failed call yields None/False, never
an exception (callers decide whether that's fatal).
"""

from __future__ import annotations

import json
import shutil

from .run import run


def available() -> bool:
    """True iff the `gh` CLI is on PATH (call-time probe — never required at import)."""
    return shutil.which("gh") is not None


def is_github(entry) -> bool:
    """True iff the hive entry is GitHub-backed (provider == github — the same fail-closed
    guard as ``registry.has_push_access``)."""
    return str((entry or {}).get("provider", "")) == "github"


def repo_slug(entry) -> str:
    """``<org>/<repo>`` for `gh --repo`, '' when the entry lacks either part."""
    org, repo = str((entry or {}).get("org", "")), str((entry or {}).get("repo", ""))
    return f"{org}/{repo}" if org and repo else ""


def _pr_list(entry, branch: str, state: str):
    """Most recent PR with head ``branch`` in ``state`` (gh's open|merged|closed|all) as a
    ``{number, url, state, mergedAt}`` row, or None (no PR / non-GitHub / gh absent / error)."""
    slug = repo_slug(entry)
    if not slug or not is_github(entry) or not available():
        return None
    res = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--head",
            branch,
            "--state",
            state,
            "--json",
            "number,url,state,mergedAt",
            "--limit",
            "1",
        ],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return None
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None


def pr_for_branch(entry, branch: str):
    """The most recent PR (ANY state) whose head is ``branch``, or None."""
    return _pr_list(entry, branch, "all")


def open_pr_for(entry, branch: str):
    """The OPEN PR whose head is ``branch`` (for idempotent re-runs), or None."""
    return _pr_list(entry, branch, "open")


def merged_pr_for(entry, branch: str):
    """The MERGED PR whose head is ``branch`` — the squash-proof landed signal
    (``gh pr list --state merged --head <branch>``) — or None."""
    return _pr_list(entry, branch, "merged")


def create_pr(entry, base: str, head: str, title: str, body: str) -> tuple[int, str]:
    """``gh pr create`` against ``base``. Returns ``(exit_code, output)`` — on success the
    output's last line is the new PR's url (gh's contract)."""
    res = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo_slug(entry),
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
        ],
        check=False,
        capture=True,
    )
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    return res.returncode, out


def pr_from_url(text: str) -> dict:
    """A ``{number, url}`` row parsed from ``gh pr create`` output (the PR url line)."""
    url = next((ln.strip() for ln in reversed((text or "").splitlines()) if "/pull/" in ln), "")
    number = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    return {"number": number, "url": url}
