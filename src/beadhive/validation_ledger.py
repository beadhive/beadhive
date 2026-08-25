"""Validation verdict ledger (bh-dfx0): skip redundant clean-checkout validations.

Each execution is authoritative in ``<git-common-dir>/bh/validation/runs/<run-id>/manifest.json``.
A reconstructable pointer for the newest completed-green run is keyed by **(tree hash,
validate-cmd hash)** below ``validation/verdicts/``. Written by every clean-checkout validation,
and — since bh-i0p1.4 — by ``work check`` too when it ran against a CLEAN worktree (a dirty
tree's HEAD wouldn't represent what actually ran, so that case is never recorded). Opt-in
callers (``work submit``) reuse a fresh GREEN verdict for the exact key and skip the throwaway
checkout entirely, whichever of the two wrote it; a red verdict remains a run fact but never a
reusable pointer, so a failure is always re-validated.

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

The commit sha(s) observed at a tree are recorded as **metadata** on manifests and derived
pointers (``sha`` — the most recent — and ``shas`` — every distinct green observation), never
as identity. They are what a later historical upload joins back to beads.

The git-metadata asterisk: same-tree/different-commit means identical file *content* and
*different git history*, so a verdict cannot vouch for anything that reads git METADATA rather
than the tree (``git describe``, commit counts, tag-derived versions — this repo has that
exposure through commitizen / ``scripts/release-pin.sh``). Those tests carry the ``always_run``
pytest marker and are never covered by a tree hit — because :func:`green_verdict` RUNS them
before it hands a hit back (bh-ehmd8, ``work.always_run``). A hit therefore means "skip the
expensive command, still run the small set a tree hash cannot vouch for", not "skip
everything"; a hive declaring no such command gets the whole hit, exactly as before.

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

Trust: this validation store is a **local optimization for trusted-local seats** — anything that
can write the git-private state can fake a green. Under bh-dfx0's sha key that ruled landing
boundaries out entirely.
Tree keying replaces that blanket refusal with the ADR's Decision 4 condition — *landing
boundaries may reuse, on exact tree match only* — and since bh-ku9n9.17 merge / postland /
finish / batch land do (``clean_checkout(..., reuse=True)``). No landing caller compares trees
itself: the key IS the match, and a hit cannot mean anything weaker. The trust model is
unchanged by that (see the ADR): this is a cache, not an authorization boundary, and it grants a
local seat no bypass it did not already have. Reviewer-facing runs (``work review --run``)
stay fresh by default and reuse only via an explicit ``--no-fresh``; ``reuse`` remains opt-in,
so any caller that does not ask for it validates for real.

Staleness: completed runs carry a timestamp and expire after ``work.ledger_ttl`` — an ISO-8601
duration (``PT30M`` / ``PT4H`` / ``P1D``), per-hive over global, default :data:`DEFAULT_TTL`
= ``P1D``, which is exactly the 24h bh-dfx0 shipped. The realistic reuse window is
minutes-to-hours; operators are expected to tune it **DOWN**, not up. The cmd hash in the key
covers command drift; the environment is established *from the tree* (verify-flagged init
rules run before the command, in both writers — see above), so it is deliberately not part of
the key. Freshness has a **lower** bound too (bh-ku9n9.19, item 7, ``_is_fresh``): an entry
timestamped in the future — planted, or produced by a forward clock jump an NTP correction
later fixes — never extends the trust window indefinitely; it reads as stale like anything
else outside ``[now - ttl, now]``.

In-flight lifecycle is explicit: each execution starts with a ``running`` manifest and a
reconstructable ``validation/active/<run-id>.json`` pointer, then becomes ``completed`` or
``abandoned``. Concurrent duplicate validations remain safe and independently recorded; active
state is lifecycle evidence and cleanup input, not a wait/skip lock.

All writes are best-effort (atomic tmp+rename, exceptions swallowed): a broken validation store
must never fail or skew the validation it records — callers just fall back to fresh runs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import secrets
import shlex
import time
from pathlib import Path

import typer

from . import config, otel, private_paths, registry, test_report, validation_records
from .run import missing_binary, run

# Read-only compatibility input from 0.15.1. New writes never touch it: rows are imported once
# into manifests and the green pointer is rebuilt from those manifests.
LEGACY_LEDGER_FILENAME = "bh-validation-ledger.json"
DEFAULT_TTL = config.DEFAULT_LEDGER_TTL  # "P1D" — the 24h bh-dfx0 shipped, as a duration
LEDGER_TTL_SECONDS = config.duration_seconds(DEFAULT_TTL)  # the default, in seconds
_MAX_SHAS = 20  # per entry: observed-commit metadata, capped like everything else here

# Public consumer seam: every outer boundary asks the same typed predicate.
is_qualifying_green = validation_records.is_qualifying_green


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


def always_run_cmd(cfg, entry) -> str:
    """`work.always_run` for this hive (per-hive > global), or `""` when nothing is declared.

    **Absent is the default and is fully supported**: no command ⇒ nothing to run on a hit ⇒
    :func:`green_verdict` behaves exactly as it did before this existed. bh never learns what the
    value means — `pytest -m always_run`, `cargo test --test metadata`, a shell script — it is
    spawned opaquely, like every other `work.*` command. Config problems degrade to absent for
    the same reason :func:`_ttl` degrades to the default: the ledger never fails a caller.

    **This is deliberately fail-OPEN, even though it is the one place in this area where that is
    the less safe direction** — everywhere else "config problem" means "run the expensive thing
    for real"; here it means "skip the git-metadata coverage a hit would otherwise be missing".
    Kept anyway because it is effectively unreachable: both current callers (`_always_run_ok`,
    transitively `green_verdict`) already hold a loaded `cfg` before calling in, so the
    `config.load()` branch never runs, and `config.work_value` raising here would mean the
    config this same call path already validated moments earlier has become unreadable
    mid-request. If a caller ever does reach this with `cfg=None` on a genuinely broken config,
    this degrades the same way `_ttl` does rather than adding a second, differently-shaped
    failure mode to the same area — consistency over a theoretical extra guard for a path
    nothing exercises."""
    try:
        cfg = config.load() if cfg is None else cfg
        return str(config.work_value(cfg, entry, "always_run", "") or "")
    except (FileNotFoundError, OSError, ValueError):
        return ""


def _always_run_ok(entry, cfg) -> bool:
    """Run the hive's always-run set; True iff a green verdict may still be honoured (bh-ehmd8).

    THE ONE THING A TREE HASH CANNOT VOUCH FOR. The ADR's git-metadata asterisk says a
    same-tree/different-commit verdict transfers for anything reading the working tree and is
    UNSOUND for anything reading git metadata. Those tests were labelled (bh-ku9n9.3's
    ``always_run`` marker) and then never run, because a hit short-circuited the whole command
    and took them with it. This is the consumer: on a hit the expensive command is still skipped,
    but the small declared set runs first, in the hive's own clone.

    **That set answers about the clone's checked-out HEAD, not necessarily the rev the verdict is
    for.** ``git describe --tags --exact-match`` is a function of (tags, HEAD): tags are
    repository-scoped refs, but HEAD is per-checkout, so this only tests the exact rev a hit is
    honouring when the clone happens to be sitting on it. On the release path (bh-ku9n9.7's
    preflight and pre-push) the clone's HEAD *is* the bump commit at both points, so the set runs
    at exactly the right rev — covered. At `work submit` and at landing boundaries the clone sits
    on `main` while the honoured rev is a branch tip, so coverage there is partial: the gap needs
    a bead branch tip sitting exactly on a tag with a `pyproject` skew, and beads are never
    tagged (only release bumps are, which go through the covered path), so it is narrow and never
    turns a fail into a pass. Skipping a per-rev checkout here is still the right call — a
    checkout per hit is exactly what tree-keying exists to avoid — this only corrects what the set
    actually vouches for.

    **The refusal is structural, not conventional.** A non-zero exit does two things, in this
    order: it latches :func:`seal_subset_run`, so no verdict can be recorded by this process
    afterwards, and only then reports the refusal. The hit is not honoured and the failing
    outcome cannot become an attestation — the caller that falls through to a full run in a
    verify checkout (whose git metadata may well differ, which is the whole exposure) finds the
    ledger already shut. Proved by attempting the write, in `tests/test_always_run.py`.

    **Why the seal is on the FAILURE and not before the spawn**, unlike `converge._subset_run`.
    A subset run observes a *converged* result and so may never attest, whatever it returns. This
    set re-runs nothing and narrows nothing — it is extra coverage on top of an already-earned
    full verdict, so a PASS observes nothing that could launder anything, and sealing there would
    withhold verdicts from every honest full run later in the same process for no safety gain.
    A FAILURE is the observation that matters: this rev is red, and a subsequent green must not
    be recorded over it.

    Anything that stops the command from RUNNING — an unsplittable value, a binary that is not on
    PATH — refuses the hit too, but does NOT seal: nothing was observed, so a later full run is an
    honest confirming run and may still attest. That matters for the ordinary case of a typo in
    `work.always_run`, which would otherwise silently cost a hive every verdict it earns."""
    cmd = always_run_cmd(cfg, entry)
    if not cmd:
        return True  # nothing declared: today's behaviour, the hit is honoured whole
    try:
        argv = shlex.split(cmd)
    except ValueError as exc:
        typer.echo(f"  ✗ work.always_run is not a runnable command line ({exc})", err=True)
        return False
    if not argv:
        return True  # whitespace-only: nothing to run, same as absent
    typer.echo(f"  → running the always-run set before honouring the verdict: {cmd}")
    hive = str(registry.hive_dir(entry))
    res = run(argv, cwd=hive, check=False, env=otel.telemetry_neutral_env())
    if binary := missing_binary(res):
        typer.echo(
            f"  ✗ the always-run set could not RUN: `{binary}` is not on PATH. That says nothing "
            f"about this tree — the verdict is not honoured and the validation runs for real.",
            err=True,
        )
        return False
    if res.returncode == 0:
        return True
    seal_subset_run()  # BEFORE the refusal is reported: this process may no longer attest
    typer.echo(
        f"  ✗ the always-run set FAILED (exit {res.returncode}) — the verdict is NOT honoured. "
        f"A tree hash cannot vouch for git metadata, and this ledger is now shut for the rest of "
        f"this process, so nothing that follows can record a verdict over that failure.",
        err=True,
    )
    return False


def cmd_hash(cmd: str) -> str:
    """Short stable hash of the validation command string — the env-drift half of the key."""
    return hashlib.sha256(cmd.encode()).hexdigest()[:16]


def tree_of(entry, rev: str) -> str:
    """The TREE hash `rev` names, resolved in the hive's main clone — verdict identity's half.

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


def _verdict_path(entry, tree: str, command_hash: str, *, create: bool = False) -> Path | None:
    """The derived pointer for one exact identity, without creating state on a read.

    Trees and command hashes are opaque identifiers, but never paths.  A malformed value is a
    miss rather than an opportunity to escape the private root.
    """
    if (
        not tree
        or not command_hash
        or any(
            value in {".", ".."} or "/" in value or "\\" in value for value in (tree, command_hash)
        )
    ):
        return None
    hive = registry.hive_dir(entry)
    root = (
        private_paths.ensure_git_private_root(hive)
        if create
        else private_paths.git_private_root(hive)
    )
    # Unit seams and degraded legacy callers can name a plain `.git` directory without a
    # functioning repository.  Preserve the former best-effort behaviour there; linked
    # worktrees have a `.git` *file* and therefore never take this unsafe-looking fallback.
    if root is None and (git_dir := hive / ".git").is_dir():
        root = git_dir / "bh"
    return root / "validation" / "verdicts" / tree / f"{command_hash}.json" if root else None


def _read_index(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_index(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _run_timestamp(run_record: dict) -> float | None:
    """Return the manifest's authoritative completion time as epoch seconds."""
    value = run_record.get("finished_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        timestamp = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _run_entry(run_record: dict) -> dict | None:
    """Compatibility-shaped read result derived from one authoritative manifest."""
    at = _run_timestamp(run_record)
    tree = run_record.get("tree")
    key = run_record.get("command_hash")
    if at is None or not isinstance(tree, str) or not isinstance(key, str):
        return None
    verdict = run_record.get("verdict")
    rc = 0 if verdict == "green" else run_record.get("exit_code")
    return {
        "schema": 1,
        "run_id": run_record.get("run_id"),
        "lifecycle": run_record.get("lifecycle"),
        "verdict": verdict,
        "exit_code": run_record.get("exit_code"),
        "signal": run_record.get("signal"),
        "reason": run_record.get("reason"),
        "tree": tree,
        "command_hash": key,
        "cmd_hash": key,
        "rc": rc,
        "at": at,
        "sha": run_record.get("sha"),
        "shas": [run_record["sha"]] if isinstance(run_record.get("sha"), str) else [],
        "host": (run_record.get("owner") or {}).get("host"),
    }


def _legacy_ledger_path(entry) -> Path | None:
    git_dir = registry.hive_dir(entry) / ".git"
    return git_dir / LEGACY_LEDGER_FILENAME if git_dir.is_dir() else None


def _legacy_time(value: object) -> str | None:
    try:
        timestamp = float(value)
        if not math.isfinite(timestamp):
            return None
        return dt.datetime.fromtimestamp(timestamp, dt.UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _migrate_legacy_ledger(entry) -> int:
    """Import the retired flat ledger once, without ever consulting it as live authority.

    The source remains in place for rollback. A git-private completion marker prevents later
    edits to that mutable compatibility file from manufacturing new executions. Corrupt and
    special-file inputs remain a bounded-time miss and are not marked complete, so a repaired
    regular file can still migrate on a later invocation.
    """
    source = _legacy_ledger_path(entry)
    if source is None or not source.is_file():
        return 0
    hive = registry.hive_dir(entry)
    root = private_paths.ensure_git_private_root(hive)
    if root is None:
        return 0
    marker = root / "validation" / "migrations" / "flat-ledger-v1.json"
    if marker.is_file():
        return 0
    try:
        value = json.loads(source.read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(value, list):
        return 0

    imported = 0
    keys: set[tuple[str, str]] = set()
    for ordinal, row in enumerate(value):
        if not isinstance(row, dict):
            continue
        tree, key, rc = row.get("tree"), row.get("cmd_hash"), row.get("rc")
        stamp = _legacy_time(row.get("at"))
        if (
            not isinstance(tree, str)
            or not tree
            or not isinstance(key, str)
            or not key
            or type(rc) is not int
            or stamp is None
        ):
            continue
        iso_time = stamp
        digest = hashlib.sha256(
            json.dumps([ordinal, row], sort_keys=True, default=str).encode()
        ).hexdigest()[:32]
        run_id = f"run-legacy-{ordinal:06d}-{digest[:24]}"
        observed = (
            [
                candidate
                for candidate in row.get("shas", [])
                if isinstance(candidate, str) and candidate
            ]
            if isinstance(row.get("shas"), list)
            else []
        )
        sha = row.get("sha") if isinstance(row.get("sha"), str) else tree
        if sha not in observed:
            observed.append(sha)
        directory = root / "validation" / "runs" / run_id
        manifest = {
            "schema": 1,
            "run_id": run_id,
            "bead": None,
            "phase": "legacy-ledger-import",
            "branch": None,
            "worktree": None,
            "sha": sha,
            "shas": observed[-_MAX_SHAS:],
            "tree": tree,
            "command_hash": key,
            "command": None,
            "owner": {"host": row.get("host"), "pid": None, "start_token": None},
            "started_at": iso_time,
            "finished_at": iso_time,
            "lifecycle": "completed",
            "verdict": "green" if rc == 0 else "red",
            "exit_code": rc,
            "signal": None,
            "reason": "legacy_ledger_import",
        }
        if isinstance(row.get("report"), dict):
            manifest["report"] = row["report"]
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "manifest.json"
            if not path.exists():
                validation_records._atomic_json(path, manifest)
                imported += 1
            keys.add((tree, key))
        except OSError:
            continue
    try:
        _write_index(marker, {"schema": 1, "source": LEGACY_LEDGER_FILENAME, "rows": imported})
    except OSError:
        return imported
    for tree, key in keys:
        _sync_index(entry, tree, key)
    return imported


def _sync_index(entry, tree: str, key: str) -> None:
    """Make one pointer exactly reflect the newest retained execution fact.

    The manifest is truth.  A pointer exists only for its newest completed-green manifest; every
    other lifecycle/verdict removes it.  Atomic replace means concurrent readers see an old whole
    pointer, a new whole pointer, or a miss — never torn JSON.
    """
    hive = registry.hive_dir(entry)
    latest = validation_records.latest_run(hive, tree=tree, command_hash=key)
    path = _verdict_path(entry, tree, key, create=latest is not None)
    if path is None:
        return
    try:
        if latest is None or not validation_records.is_qualifying_green(latest):
            path.unlink(missing_ok=True)
            return
        at = _run_timestamp(latest)
        if at is None:
            path.unlink(missing_ok=True)
            return
        runs = sorted(
            validation_records.matching_runs(hive, tree=tree, command_hash=key),
            key=validation_records._run_order_key,
        )
        shas = []
        for run_record in runs:
            if not validation_records.is_qualifying_green(run_record):
                continue
            observed = run_record.get("shas", [])
            candidates = observed if isinstance(observed, list) else []
            candidates = [*candidates, run_record.get("sha")]
            for candidate in candidates:
                if isinstance(candidate, str) and candidate and candidate not in shas:
                    shas.append(candidate)
        _write_index(
            path,
            {
                "schema": 1,
                "run_id": latest["run_id"],
                "tree": tree,
                "command_hash": key,
                "rc": 0,
                "at": at,
                "sha": latest.get("sha"),
                "shas": shas[-_MAX_SHAS:],
                "host": (latest.get("owner") or {}).get("host"),
            },
        )
    except (OSError, KeyError, TypeError):
        pass


def rebuild_verdict_index(entry) -> int:
    """Reconstruct all lookup pointers from retained run manifests; return green pointers made."""
    hive = registry.hive_dir(entry)
    root = private_paths.git_private_root(hive)
    runs = root / "validation" / "runs" if root else None
    if runs is None or not runs.is_dir():
        return 0
    keys = {
        (run.get("tree"), run.get("command_hash"))
        for child in runs.iterdir()
        if (run := validation_records.read_run(hive, child.name)) is not None
        and isinstance(run.get("tree"), str)
        and isinstance(run.get("command_hash"), str)
    }
    rebuilt = 0
    for tree, key in keys:
        _sync_index(entry, tree, key)
        path = _verdict_path(entry, tree, key)
        rebuilt += bool(path is not None and path.is_file())
    return rebuilt


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
    run_id: str | None = None,
    phase: str = "validation",
    bead: str | None = None,
    branch: str | None = None,
    worktree: str | Path | None = None,
) -> None:
    """Record a validation execution for (tree of `rev`, cmd). Best-effort: never raises or
    fails the validation it records. The run manifest is authority; a green pointer is rebuilt
    from the newest execution fact and carries observed-commit metadata forward.

    `report` is an optional ingested test report (`test_report.ingest`) — **detail, never the
    verdict**. `rc` remains authoritative (bh-ku9n9.20, binding constraint 1): a report claiming
    everything passed alongside a non-zero `rc` still records red and so still misses
    :func:`green_verdict`. Only the counts are attached under the manifest's canonical bounded
    `summary`; per-test records remain in the artifact store. `None` (the normal case for a hive
    that opts into nothing) adds no counts.

    `cfg`, when given, is used for the TTL lookup instead of a fresh `config.load()` (bh-ku9n9.19,
    item 2) — see :func:`_ttl`."""
    if not rev or _SEALED:  # sealed: a converged result is never an attestation
        return
    tree, key = tree_of(entry, rev), cmd_hash(cmd)
    # The run history is authoritative. The retired 0.15.1 flat file is a one-time read-only
    # migration input; new decisions never derive identity from a mutable row.
    hive = registry.hive_dir(entry)
    run_record = validation_records.read_run(hive, run_id) if run_id else None
    if run_id is None:
        run_record = validation_records.begin_run(
            hive,
            bead=bead or os.environ.get("BH_BEAD_ID"),
            phase=phase or os.environ.get("BH_VALIDATION_PHASE", "validation"),
            branch=branch or os.environ.get("BH_VALIDATION_BRANCH"),
            worktree=worktree or os.environ.get("BH_VALIDATION_WORKTREE"),
            sha=rev,
            tree=tree,
            command_hash=key,
            command=cmd,
            owner_start=os.environ.get("BH_OWNER_START_TOKEN"),
        )
    if run_record is not None and run_record.get("lifecycle") == "running":
        run_record = validation_records.finish_run(
            hive,
            run_record["run_id"],
            exit_code=int(rc),
            reason="command_exit",
        )
    if (
        run_record is not None
        and run_record.get("lifecycle") == "completed"
        and (summary := test_report.counts(report)) is not None
    ):
        run_record = (
            validation_records.attach_summary(
                hive, run_record["run_id"], {"counts": summary, "tree": tree}
            )
            or run_record
        )
    if run_record is not None and run_record.get("lifecycle") == "completed":
        validation_records.record_use(
            hive,
            run_id=run_record["run_id"],
            bead=bead or run_record.get("bead"),
            phase=phase or run_record["phase"],
            branch=branch or run_record.get("branch"),
            worktree=worktree or run_record.get("worktree"),
            sha=rev,
            tree=tree,
            command_hash=key,
            reused=False,
        )
    # The pointer is derived solely from retained manifests. Report counts remain attached to the
    # durable run summary; they are not verdict authority and are deliberately absent here.
    _sync_index(entry, tree, key)


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
    if not rev:
        return None
    _migrate_legacy_ledger(entry)
    now = time.time()
    tree, key = tree_of(entry, rev), cmd_hash(cmd)
    source = validation_records.latest_run(registry.hive_dir(entry), tree=tree, command_hash=key)
    if (
        source is None
        or not validation_records.is_completed_verdict(source)
        or (entry_value := _run_entry(source)) is None
        or not _is_fresh(entry_value, now, _ttl(entry, ttl, cfg))
    ):
        return None
    if source.get("verdict") == "green" and not validation_records.is_qualifying_green(source):
        return None
    if source.get("verdict") != "green":
        return entry_value
    hit = _read_index(_verdict_path(entry, tree, key))
    if (
        hit is None
        or hit.get("schema") != 1
        or hit.get("run_id") != source.get("run_id")
        or hit.get("tree") != tree
        or hit.get("command_hash") != key
        or hit.get("rc") != 0
        or hit.get("at") != entry_value["at"]
    ):
        return None
    return {**entry_value, **hit}


def green_verdict(entry, rev: str, cmd: str, ttl: int | None = None, cfg=None) -> dict | None:
    """The recorded entry for exactly (tree of `rev`, cmd) iff it is GREEN (rc == 0) and fresh
    (within `ttl`, default `work.ledger_ttl`), else None. A red / stale / missing verdict always
    means: run the validation. Only an EXACT tree match hits — a rebase of the same patch onto a
    different base is a different tree and always revalidates.

    **THE ONE PLACE A HIT IS DECIDED** (bh-ehmd8), which is why the always-run set runs here and
    nowhere else. Every boundary that may skip work on a recorded verdict asks this one question
    — `clean_checkout(reuse=True)` via `worktree._reuse_verdict_hit` (submit, and every landing
    boundary since bh-ku9n9.17), and `prepush.check_push_main` (the pre-push hook, and the
    release pre-flight through it) — so `work.always_run` is enforced for all of them by
    existing, not replicated at three call sites where a fourth could forget it. `verdict` above
    is deliberately NOT the seam: `release await` polls it every few seconds to tell a red gate
    from an unfinished one, and it waits on a full run it fired itself at that exact tree.

    `cfg` is forwarded to :func:`verdict`'s TTL lookup (bh-ku9n9.19, item 2) and to the
    always-run lookup, so a caller that already loaded config pays for neither twice."""
    hit = verdict(entry, rev, cmd, ttl, cfg)
    # `!= 0` rather than a truthiness test on purpose: a malformed rc (the string "0", None, a
    # dict) is not the integer 0 and so is NOT green — a corrupt record must never read as a pass.
    if hit is None or not validation_records.is_qualifying_green(hit):
        return None
    if not _always_run_ok(entry, cfg):
        return None
    return hit
