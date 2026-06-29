"""Git-history extraction + assertions — the harness's validation surface.

We assert on authors, verified signatures (%G?/%GS via the repo-local allowed_signers),
branch names, and merge structure — never on commit message bodies.
"""

from __future__ import annotations

from pathlib import Path

from .world import git

_SEP = "\x1f"
_FMT = _SEP.join(["%H", "%an", "%ae", "%cn", "%G?", "%GS", "%P", "%s"])


def commits(repo: Path, ref: str = "main") -> list[dict]:
    """Commits reachable from `ref`, newest first."""
    out = git("log", f"--format={_FMT}", ref, cwd=repo).stdout
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        h, an, ae, cn, sig, signer, parents, subj = line.split(_SEP)
        rows.append(
            {
                "sha": h,
                "author": an,
                "email": ae,
                "committer": cn,
                "sig": sig,  # G=good, B=bad, U=good/unknown, N=none
                "signer": signer,
                "parents": parents.split(),
                "subject": subj,
            }
        )
    return rows


def merges(repo: Path, ref: str = "main") -> list[dict]:
    return [c for c in commits(repo, ref) if len(c["parents"]) > 1]


def local_branches(repo: Path) -> list[str]:
    out = git("branch", "--format=%(refname:short)", cwd=repo, check=False).stdout
    return [b.strip() for b in out.splitlines() if b.strip()]


def remote_branches(repo: Path) -> list[str]:
    """Branch names present in a bare repo (the push target)."""
    out = git("branch", "--format=%(refname:short)", cwd=repo, check=False).stdout
    return [b.strip() for b in out.splitlines() if b.strip()]


def author_commit(repo: Path, subject_contains: str, ref: str = "main") -> dict:
    """The single non-merge commit whose subject contains `subject_contains`."""
    hits = [
        c for c in commits(repo, ref) if len(c["parents"]) <= 1 and subject_contains in c["subject"]
    ]
    assert len(hits) == 1, f"expected 1 commit matching {subject_contains!r}, got {len(hits)}"
    return hits[0]


def assert_verified_signed_by(commit: dict, email: str):
    assert commit["sig"] == "G", f"signature not good: {commit['sig']} ({commit['subject']!r})"
    assert commit["signer"] == email, f"signer {commit['signer']!r} != {email!r}"


def assert_unsigned(commit: dict):
    assert commit["sig"] == "N", f"expected unsigned, got {commit['sig']}"


def assert_author(commit: dict, name: str, email: str):
    assert commit["author"] == name, f"author {commit['author']!r} != {name!r}"
    assert commit["email"] == email, f"email {commit['email']!r} != {email!r}"
