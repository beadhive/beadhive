"""A JSON record stored at an arbitrary git ref, read and written by compare-and-swap.

The primitive the multi-host write model is built on
(``docs/design/multi-host-model-adr.md``, Decision 2 + Amendment 1). Eventual convergence of
*bead data* provides no mutual exclusion, but **a git remote is a linearization point**: a ref
update is atomic there, and ``git push --force-with-lease=<ref>:<expected-sha>`` is a genuine
compare-and-swap against a single authority every hive already has.

Two records ride this module:

  * the **host lease** at ``refs/bh/lease/<prefix>`` in the HQ repo (:mod:`beadhive.host_lease`)
  * the **epoch fence** at ``refs/bh/epoch`` beside a hive's ``refs/dolt/data``
    (:mod:`beadhive.host_fence`)

Both are *blobs*, not commits: a ref may point at any object, the record has no history worth
keeping (only its current value is meaningful), and a blob keeps the object graph empty — no
tree, no parent, nothing to walk. Verified against git 2.54: a blob-pointing ref pushes,
``ls-remote``s, fetches, and CASes exactly like a commit-pointing one.

Two git behaviours this module depends on, both verified against a scratch remote rather than
assumed (``tests/test_gitref.py`` pins them, so a git upgrade that changed either would fail
the suite rather than silently weaken the fence):

  * ``--force-with-lease=<ref>:<expected>`` rejects the push when the remote ref is not at
    ``<expected>`` — exit non-zero, ``! [rejected] … (stale info)``, nothing written.
  * ``--force-with-lease=<ref>:`` with an **empty** expected value means *the ref must not
    exist yet* — which is how "adopt from absent" is expressed as a CAS rather than as a
    check-then-write race.

Typer-free and config-free on purpose: it is plumbing over one subprocess seam
(:func:`beadhive.run.run`), so every caller and every test drives it against a scratch bare
repo in a tmp dir.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .run import run

# Bounds a ls-remote/fetch/push so a wedged remote can't hang a bh verb (hq.py's convention).
GIT_TIMEOUT = 30.0

# The expected-value spelling for "this ref must not exist yet" — an empty `<expect>` in
# `--force-with-lease=<ref>:<expect>`. Named because `expected=""` reads like "don't care"
# at a call site when it means the exact opposite (a hard assertion of absence).
ABSENT = ""


class RemoteUnreachable(RuntimeError):
    """A read of the remote's ref state failed (network, auth, no such remote).

    Distinct from a CAS *rejection*: unreachable means we learned nothing, so a caller must
    degrade (read-only) rather than conclude anything about who holds what."""


@dataclass(frozen=True)
class CasResult:
    """Outcome of one compare-and-swap against a remote ref."""

    ok: bool
    ref: str
    sha: str  # the object the CAS installed (or tried to); "" for a delete
    detail: str = ""  # git's own message on rejection — never paraphrased away


def _git(args: list[str], cwd: Path):
    return run(["git", *args], cwd=str(cwd), check=False, capture=True, timeout=GIT_TIMEOUT)


def _msg(res) -> str:
    """The most diagnostic line of git's own output, verbatim — a lease/fence refusal that
    paraphrases git loses the one line an operator can act on.

    Prefers the per-ref verdict (``! [rejected] … (stale info)`` / ``(atomic push failed)``),
    which git prints BEFORE the generic ``error: failed to push some refs`` trailer; falls
    back to the last line when there is no per-ref verdict to quote."""
    lines = [
        line.strip()
        for line in ((res.stderr or "") + "\n" + (res.stdout or "")).splitlines()
        if line.strip()
    ]
    if not lines:
        return f"exit {res.returncode}"
    verdicts = [line for line in lines if line.startswith("!") or "[rejected]" in line]
    return verdicts[0] if verdicts else lines[-1]


def encode(record: Mapping) -> str:
    """Canonical JSON for a record: sorted keys, no insignificant whitespace, one trailing
    newline. Canonical so the blob sha is a pure function of the record's *content* — two
    hosts that compute the same record compute the same sha, and a CAS expectation can be
    stated as a sha without anyone having to agree on dict ordering."""
    return json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"


def decode(text: str) -> dict:
    """Parse a record blob. Raises ``ValueError`` on anything that is not a JSON object —
    a malformed record is a loud failure, never a silently-empty one (a lease that decodes
    to ``{}`` would read as "nobody holds this", which is exactly the wrong default)."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object at this ref, got {type(data).__name__}")
    return data


def write_object(record: Mapping, *, cwd: Path) -> str:
    """Hash + store `record` as a blob in `cwd`'s object database, returning its sha.

    ``-w`` is what makes the object pushable: a bare ``hash-object`` computes the sha without
    writing it, and the subsequent push would fail on a missing object."""
    res = run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(cwd),
        check=False,
        capture=True,
        text_input=encode(record),
        timeout=GIT_TIMEOUT,
    )
    if res.returncode:
        raise RuntimeError(f"git hash-object failed: {_msg(res)}")
    return (res.stdout or "").strip()


def remote_sha(remote: str, ref: str, *, cwd: Path) -> str:
    """The sha `ref` points at on `remote`, or ``""`` when the ref does not exist.

    Raises :class:`RemoteUnreachable` when the *query itself* failed — the distinction
    matters: "absent" licenses an adopt, "unreachable" licenses nothing."""
    res = _git(["ls-remote", remote, ref], cwd)
    if res.returncode:
        raise RemoteUnreachable(f"git ls-remote {remote} {ref} failed: {_msg(res)}")
    line = (res.stdout or "").strip()
    return line.split()[0] if line else ""


def read_remote(remote: str, ref: str, *, cwd: Path) -> tuple[str, dict | None]:
    """``(sha, record)`` for `ref` on `remote`; ``("", None)`` when the ref is absent.

    Fetches the object into `cwd` first — the record is a blob the local repo may never have
    seen, and ``cat-file`` cannot read what is not local. The fetch is by explicit refspec
    with no destination, so it lands in ``FETCH_HEAD`` and leaves **no local ref** behind:
    reading the remote's lease must never install a local ref that a later CAS might mistake
    for our own state."""
    sha = remote_sha(remote, ref, cwd=cwd)
    if not sha:
        return "", None
    fetched = _git(["fetch", remote, ref], cwd)
    if fetched.returncode:
        raise RemoteUnreachable(f"git fetch {remote} {ref} failed: {_msg(fetched)}")
    shown = _git(["cat-file", "-p", sha], cwd)
    if shown.returncode:
        raise RemoteUnreachable(f"git cat-file {sha} failed: {_msg(shown)}")
    return sha, decode(shown.stdout or "")


def cas(
    remote: str, ref: str, record: Mapping, *, expected: str, cwd: Path
) -> CasResult:
    """Compare-and-swap `record` into `ref` on `remote`, conditional on the remote ref being
    at `expected` (:data:`ABSENT` — the empty string — asserts the ref does not exist yet).

    Returns a :class:`CasResult`; a lost race is ``ok=False`` with git's own message, **never**
    an exception and never a retry. Retrying a lost CAS is precisely the bug this primitive
    exists to prevent — the loser of an adopt race must be told it lost, so callers decide,
    not this layer."""
    sha = write_object(record, cwd=cwd)
    res = _git(
        ["push", f"--force-with-lease={ref}:{expected}", remote, f"{sha}:{ref}"], cwd
    )
    return CasResult(ok=res.returncode == 0, ref=ref, sha=sha, detail=_msg(res))


def read_local(ref: str, *, cwd: Path) -> tuple[str, dict | None]:
    """``(sha, record)`` for a LOCAL `ref` in `cwd`; ``("", None)`` when absent.

    The offline read: what this host last knew, with no network at all. The write path always
    CASes against the remote, but a hot-path *check* (``guard_primary``) reads this instead —
    per Amendment 1 §4 an established primary must not need HQ on every write verb."""
    res = _git(["rev-parse", "--verify", "--quiet", ref], cwd)
    sha = (res.stdout or "").strip()
    if res.returncode or not sha:
        return "", None
    shown = _git(["cat-file", "-p", sha], cwd)
    if shown.returncode:
        return "", None
    try:
        return sha, decode(shown.stdout or "")
    except ValueError:
        return sha, None


def set_local(ref: str, sha: str, *, cwd: Path) -> None:
    """Point a LOCAL `ref` at `sha` — the cache write that mirrors a won CAS, so
    :func:`read_local` can answer without the network. Raises on failure: a silently-unwritten
    cache would make a fresh primary look like a follower to its own guard."""
    res = _git(["update-ref", ref, sha], cwd)
    if res.returncode:
        raise RuntimeError(f"git update-ref {ref} {sha} failed: {_msg(res)}")


def delete_local(ref: str, *, cwd: Path) -> None:
    """Drop a LOCAL `ref` (best-effort — a missing ref is already the desired state)."""
    _git(["update-ref", "-d", ref], cwd)
