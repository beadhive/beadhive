"""Validation verdict ledger (bh-dfx0): skip redundant clean-checkout validations.

Records the outcome of a validation run keyed by **(tree hash, validate-cmd hash)** in a small
untracked JSON file inside the hive's git dir (``<hive>/.git/bh-validation-ledger.json`` —
repo-local state, never a tracked file, dies with the clone). Written by every clean-checkout
validation, and — since bh-i0p1.4 — by ``work check`` too when it ran against a CLEAN worktree
(a dirty tree's HEAD wouldn't represent what actually ran, so that case is never recorded).
Opt-in callers (``work submit``) reuse a recorded GREEN verdict for the exact key and skip the
throwaway checkout entirely, whichever of the two wrote it; a red verdict is recorded but never
reused, so a failure is always re-validated.

**The key is the TREE, not the commit** (bh-ku9n9.3, ``docs/design/attested-green-adr.md``).
A ``--no-ff`` merge onto an *unmoved* main produces a merge commit whose tree is byte-identical
to the branch tip's, so the land-time run that tested the tip already tested the exact bytes the
merge produces; keyed on the commit sha it would be re-validated for nothing. Two properties
fall out with no hand-written invalidation rules: if main moved, the merge tree differs from
both parents and the lookup misses; and the scheme is self-covering, because the justfile that
defines the gate is itself a file *in* the tree — editing it changes the tree hash and
invalidates every prior verdict.

The invariant, from the ADR's equality matrix: a verdict transfers on **exact tree match only**.
Never a same-patch-different-base rebase (different content), never a subtree match, never
"close enough". The public functions take a *rev* (commit sha, branch, or a tree hash) and
resolve it to its tree — so nothing but identical content can collide. A rev git cannot resolve
is used verbatim, which can only ever match itself: both directions of a resolve/no-resolve
mismatch simply miss and revalidate.

The commit sha(s) observed at a tree are recorded as **metadata** on the entry (``sha`` — the
most recent — and ``shas`` — every distinct one seen), never as identity. They are what a later
historical upload joins back to beads; they are read by nothing here.

The git-metadata asterisk: same-tree/different-commit means identical file *content* and
*different git history*, so a verdict cannot vouch for anything that reads git METADATA rather
than the tree (``git describe``, commit counts, tag-derived versions — this repo has that
exposure through commitizen / ``scripts/release-pin.sh``). Those tests carry the ``always_run``
pytest marker and are never covered by a tree hit.

Both writers establish their environment FROM THE TREE before validating, by running the hive's
``verify: true`` init rules (``worktree.run_init(..., verify_only=True)``) — ``clean_checkout``
in its verify dir, ``work check`` in the seat worktree (bh-ku9n9.14). The tree hash fixes the
*content*; re-deriving the environment from that content is what makes either writer's verdict a
property of the tree rather than of when its checkout happened to be provisioned, and so makes
the two interchangeable under one key. Without it, two runs over the identical tree could differ
in coverage yet be indistinguishable here — an exposure tree keying magnifies, since keying on
the tree is precisely what makes hits frequent and long-lived. bh interprets no rule — they are
opaque ``{run, if_exists?, verify?}`` entries spawned in the operator's declared order — so a
hive declaring none simply gets no environment establishment, in either writer.

Trust: the ledger is a **local optimization for trusted-local seats** — anything that can write
the file can fake a green. Under bh-dfx0's sha key that ruled landing boundaries out entirely.
Tree keying replaces that blanket refusal with the ADR's Decision 4 condition — *landing
boundaries may reuse, on exact tree match only* — and since bh-ku9n9.17 merge / postland /
finish / batch land do (``clean_checkout(..., reuse=True)``). No landing caller compares trees
itself: the key IS the match, and a hit cannot mean anything weaker. The trust model is
unchanged by that (see the ADR): this is a cache, not an authorization boundary, and it grants a
local seat no bypass it did not already have. Reviewer-facing runs (``work review --run``)
stay fresh by default and reuse only via an explicit ``--no-fresh``; ``reuse`` remains opt-in,
so any caller that does not ask for it validates for real.

Staleness: entries carry a timestamp and expire after ``work.ledger_ttl`` — an ISO-8601
duration (``PT30M`` / ``PT4H`` / ``P1D``), per-hive over global, default :data:`DEFAULT_TTL`
= ``P1D``, which is exactly the 24h bh-dfx0 shipped. The realistic reuse window is
minutes-to-hours; operators are expected to tune it **DOWN**, not up. The cmd hash in the key
covers command drift; the environment is established *from the tree* (verify-flagged init
rules run before the command, in both writers — see above), so it is deliberately not part of
the key. Freshness has a **lower** bound too (bh-ku9n9.19, item 7, ``_is_fresh``): an entry
timestamped in the future — planted, or produced by a forward clock jump an NTP correction
later fixes — never extends the trust window indefinitely; it reads as stale like anything
else outside ``[now - ttl, now]``.

In-flight marker: deliberately NOT implemented. Per-invocation verify dirs (bh-nikb)
already make concurrent duplicate validations *safe* — the ledger only removes
duplicate *cost* — and a wait/skip protocol would add cross-process locking for a
marginal saving. Revisit if `bh.work.validation` telemetry shows overlap matters.

All writes are best-effort (atomic tmp+rename, exceptions swallowed): a broken ledger
must never fail or skew the validation it records — callers just fall back to fresh runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import config, host, registry, test_report
from .run import run

LEDGER_FILENAME = "bh-validation-ledger.json"
DEFAULT_TTL = config.DEFAULT_LEDGER_TTL  # "P1D" — the 24h bh-dfx0 shipped, as a duration
LEDGER_TTL_SECONDS = config.duration_seconds(DEFAULT_TTL)  # the default, in seconds
_MAX_ENTRIES = 200  # hard cap so the ledger never grows unbounded
_MAX_SHAS = 20  # per entry: observed-commit metadata, capped like everything else here


#: Latched by :func:`seal_subset_run` the first time this process runs a `work.validate_subset`
#: command. Never cleared — see that function.
_SEALED = False


def seal_subset_run() -> None:
    """Shut this process's ledger for good: no verdict may be recorded after a subset run.

    THE EPIC'S CENTRAL HAZARD, closed structurally (bh-ku9n9.8, ADR settled decision 1). Once a
    command built from `work.validate_subset` has run, this process has observed a **converged**
    result — a few tests re-run in isolation, most not run at all — and no such result may ever
    become an attestation. `converge` seals *before* it spawns, so the guarantee does not depend
    on which branch the caller takes afterwards, on no exception being raised, or on a future
    caller remembering the rule. A test proves it rather than inspection (`tests/test_converge.py`).

    **Never cleared, and the failure direction is deliberate.** The worst this can do is withhold
    a cache entry, which costs one re-validation — already the ledger's documented answer to a
    miss, an expired entry, or a corrupt file. The other direction costs the trust in every
    verdict bh has ever written. In the CLI nothing follows a converge in-process anyway:
    `work check` records its verdict before converging, and that run is red by definition."""
    global _SEALED
    _SEALED = True


def cmd_hash(cmd: str) -> str:
    """Short stable hash of the validation command string — the env-drift half of the key."""
    return hashlib.sha256(cmd.encode()).hexdigest()[:16]


def tree_of(entry, rev: str) -> str:
    """The TREE hash `rev` names, resolved in the hive's main clone — the ledger's identity half.

    `rev` may be a commit sha, a branch, or a tree hash (which resolves to itself, so the
    functions below are idempotent under re-keying). A rev git cannot resolve — no clone, a
    faked sha in a test — comes back verbatim: it can then only ever match an identical literal,
    so a resolve/no-resolve mismatch between the writer and the reader misses and revalidates
    rather than serving a verdict for content it never saw."""
    if not rev:
        return ""
    res = run(
        ["git", "-C", str(registry.hive_dir(entry)), "rev-parse", f"{rev}^{{tree}}"],
        check=False,
        capture=True,
    )
    out = (getattr(res, "stdout", "") or "").strip()
    return out if res.returncode == 0 and out else rev


def _ttl(entry, ttl: int | None, cfg=None) -> int:
    """`ttl` when a caller pinned one, else `work.ledger_ttl` for this hive (layered per-hive >
    global > P1D). `cfg`, when given, is used as-is instead of re-reading config from disk — a
    caller that already loaded it (bh-ku9n9.19: `clean_checkout` / `check_push_main` / `check`
    all do) gets its own value honoured rather than silently overridden by a fresh `config.load()`.
    Config problems degrade to the default — the ledger never fails a caller."""
    if ttl is not None:
        return ttl
    try:
        return config.ledger_ttl(cfg if cfg is not None else config.load(), entry)
    except (FileNotFoundError, OSError, ValueError):
        return LEDGER_TTL_SECONDS


def _ledger_path(entry) -> Path | None:
    """The hive-local ledger file, or None when there is no plain `.git` dir to keep it in
    (linked worktree / missing clone) — callers then simply fall back to fresh runs."""
    git_dir = registry.hive_dir(entry) / ".git"
    return git_dir / LEDGER_FILENAME if git_dir.is_dir() else None


def _load(path: Path) -> list[dict]:
    """The ledger's entry list; [] on any read/shape problem (corrupt file == empty ledger)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _is_fresh(e: dict, now: float, ttl: int) -> bool:
    """`e["at"]` is at most `ttl` seconds old — AND not in the future (bh-ku9n9.19, item 7).
    `now - at` has no lower bound clamped here on purpose: a future `at` (a planted entry, or a
    forward clock jump before an NTP correction — nobody has to hand-edit a file to produce one)
    would otherwise make `now - at` negative, which is trivially `<= ttl` and so extends the
    trust window indefinitely. Bounding age at 0 closes that without touching the ordinary case,
    where `at <= now` always. A pathological `at` (1e30, inf) still degrades safely — either the
    comparison above is simply False, or, for a value too large for `float()` itself, the
    `OverflowError` below is caught the same as any other malformed entry: a miss, never a pass
    and never a crash."""
    try:
        age = now - float(e["at"])
        return 0 <= age <= ttl
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def record(
    entry,
    rev: str,
    cmd: str,
    rc: int,
    ttl: int | None = None,
    report: dict | None = None,
    cfg=None,
) -> None:
    """Record a validation verdict for (tree of `rev`, cmd). Best-effort: never raises, never
    fails the validation it records. Prunes expired entries and replaces a same-key entry,
    carrying that entry's observed-commit metadata forward.

    `report` is an optional ingested test report (`test_report.ingest`) — **detail, never the
    verdict**. `rc` remains the sole verdict (bh-ku9n9.20, binding constraint 1): a report
    claiming everything passed alongside a non-zero `rc` still records `rc` and so still misses
    :func:`green_verdict`. Only the counts are kept — per-test records in a 200-entry ledger
    would cost ~96 MiB per hive (bh-ku9n9.4, Evidence 9), and the durable per-tree triage store
    is bh-ku9n9.6's. `None` (the normal case for a hive that opts into nothing) adds no key at
    all, so an rc-only entry is byte-for-byte what it has always been.

    `cfg`, when given, is used for the TTL lookup instead of a fresh `config.load()` (bh-ku9n9.19,
    item 2) — see :func:`_ttl`."""
    path = _ledger_path(entry)
    if path is None or not rev or _SEALED:  # sealed: a converged result is never an attestation
        return
    now = time.time()
    tree, key = tree_of(entry, rev), cmd_hash(cmd)
    existing = _load(path)
    same = [e for e in existing if e.get("tree") == tree and e.get("cmd_hash") == key]
    # Resolved ONCE, not once per pruned entry (bh-ku9n9.19, item 1): `_ttl` can call the
    # uncached `config.load()`, and this list comprehension can run up to `_MAX_ENTRIES` (200)
    # times per write.
    ttl_seconds = _ttl(entry, ttl, cfg)
    kept = [e for e in existing if _is_fresh(e, now, ttl_seconds) and e not in same]
    # Commit shas are METADATA, never identity (bh-ku9n9.3): `sha` is the one observed by this
    # run, `shas` every distinct one seen at this tree — the join key a later historical upload
    # needs, and exactly what the --no-ff-onto-unmoved-main case produces two of. Nothing here
    # reads them back. `host` is diagnostic-only too (bh-ytbb.4): the stable `host_id()` UUID,
    # not `socket.gethostname()`, for consistency with the other two markers.
    seen = [s for e in same for s in e.get("shas", []) if s != rev]
    new = {
        "tree": tree,
        "cmd_hash": key,
        "rc": int(rc),
        "at": now,
        "host": host.host_id(),
        "sha": rev,
        "shas": (seen + [rev])[-_MAX_SHAS:],
    }
    if (summary := test_report.counts(report)) is not None:
        new["report"] = summary
    entries = (kept + [new])[-_MAX_ENTRIES:]
    try:
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(entries) + "\n")
        os.replace(tmp, path)  # atomic: a concurrent reader never sees a torn file
    except OSError:
        pass


def verdict(entry, rev: str, cmd: str, ttl: int | None = None, cfg=None) -> dict | None:
    """The FRESH recorded entry for exactly (tree of `rev`, cmd) **whatever its rc**, else None.

    :func:`green_verdict` is the read almost everything wants — "may I skip this run?" — and is
    defined in terms of this one. This raw form exists for the single caller that must tell a
    RED verdict apart from NO verdict: `bh release await` (bh-ku9n9.7), which waits on a
    background gate over the bump tree. "not finished yet" must keep waiting while "finished and
    failed" must refuse the release immediately, and `green_verdict` collapses both to None.

    Nothing here treats a red entry as permission for anything: the only consumer of a non-green
    return is a caller that refuses harder because of it.

    `cfg`, when given, is used for the TTL lookup instead of a fresh `config.load()` — see
    :func:`_ttl` (bh-ku9n9.19, item 2)."""
    path = _ledger_path(entry)
    if path is None or not rev:
        return None
    now = time.time()
    tree, key = tree_of(entry, rev), cmd_hash(cmd)
    hit = next(
        (e for e in reversed(_load(path)) if e.get("tree") == tree and e.get("cmd_hash") == key),
        None,
    )
    return hit if hit is not None and _is_fresh(hit, now, _ttl(entry, ttl, cfg)) else None


def green_verdict(entry, rev: str, cmd: str, ttl: int | None = None, cfg=None) -> dict | None:
    """The recorded entry for exactly (tree of `rev`, cmd) iff it is GREEN (rc == 0) and fresh
    (within `ttl`, default `work.ledger_ttl`), else None. A red / stale / missing verdict always
    means: run the validation. Only an EXACT tree match hits — a rebase of the same patch onto a
    different base is a different tree and always revalidates.

    `cfg` is forwarded to :func:`verdict`'s TTL lookup (bh-ku9n9.19, item 2)."""
    hit = verdict(entry, rev, cmd, ttl, cfg)
    # `!= 0` rather than a truthiness test on purpose: a malformed rc (the string "0", None, a
    # dict) is not the integer 0 and so is NOT green — a corrupt record must never read as a pass.
    return None if hit is None or hit.get("rc") != 0 else hit
