""".bh/validation/runs/.summary/<tree>/ — bounded validation summaries.

`test_report` (bh-ku9n9.20) is the READ/TRUST side: it exports a drop zone, parses what the
hive's own runner leaves there, and hands back a report. This module is the WRITE/STORE side:
it decides **what is persisted, where, and what reads it back**. It invokes no runner and
parses no XML — it consumes `test_report.ingest`'s output.

**Why two directories, and why that is forced rather than a preference.** `test_report`'s drop
zone is `TemporaryDirectory`: fresh per run, removed after, because a stale green report is
literally a pass a full run would not have produced (its binding constraint 2). This store is
the exact opposite — durable, accumulating, keyed by tree. Keying on the tree does not dissolve
the conflict, because **bh-ku9n9.8's flake signal IS retry history across runs at the same
tree**: a store wiped per run destroys precisely the record that makes a flake visible. One
directory cannot be both cleared and retained, so there are two.

**Run manifests and triage detail are split, on measured grounds.** A compact execution manifest
is hundreds of bytes; a full per-test JUnit XML for this suite is ~577 KB. Per-test records in
git-private control metadata would cost tens of MiB per hive. So a manifest keeps rc/verdict,
tree, command hash, commit metadata, and `test_report.counts`; this module owns the larger detail.
The tree hash is already on every run, so it is the join key into this store without another
control-plane pointer.

**THE WRITE RULE — red and retried runs only, never every green.** :func:`_should_write` is the
single enforcement point, and it is one line:

    rc != 0 or dest.exists()

A tree that has only ever been green has no directory here and never gets one — the common case
costs zero bytes. A red run creates it. Any *later* run at that same tree — including a green one
— then writes, because the directory exists, and that is exactly the red→green transition
bh-ku9n9.8 reads as a flake. There is no third condition and no config knob to get wrong.

**What bh-ku9n9.8 can rely on**: `results.json` at `.bh/testreport/<tree>/`, a `{"tree", "runs"}`
object whose `runs` are append-ordered oldest→newest, capped at :data:`_MAX_RUNS`. Each run
carries `rc` (authoritative), `at`, `sha`, `cmd_hash`, `counts`, and `cases` — per-test records
in OTel `test.*` vocabulary. `cases` is deliberately **not** every test: it is every non-passing
test of this run, **plus** every test that was non-passing in an earlier retained run at this
tree (:func:`_cases`). That carry-forward is what makes a flake unambiguous — a name that is
`failed` in run N and explicitly `passed` in run N+1 is a flake at identical content, and absence
never has to be interpreted. Its size is bounded by the number of tests that have *ever* failed at
this tree, not by suite size: 4,934 green tests write nothing.

Alongside it, latest-run-wins: the runner's raw `*.xml` verbatim, and `gate.log` — the full
merged output of the validation command, which today survives **nowhere** (`main-push-gate.sh`
tees nothing), so a 6-minute red gate can currently only be examined by burning another 6.
`gate.log` needs no tier-1 support at all, so a hive whose runner emits no machine-readable
results still gets the log; it just gets `counts: null` and no `cases`.

**Tier 3 (`contexts.json` / `coverage.xml`) is STRUCK**, per the operator-approved amendment on
this bead: outside Python per-test coverage attribution is a different execution model, not a
slower path, so it cannot degrade gracefully to an un-optimised run the way every other tier
must. Not built, not stubbed.

**Location and hiding.** The store lives in the hive's **main clone** — `<hive>/.bh/testreport/`
— not in a worktree, because both writers must reach the same store: `work check` runs in the
seat worktree and `clean_checkout` runs in a throwaway verify worktree that is deleted seconds
later, and a per-run directory would lose the cross-run history that is the whole point. `.bh/`
is hidden with the existing per-worktree git-exclude mechanism (`observaloop_env._git_exclude`,
which resolves `git rev-parse --git-path info/exclude`), so **no `.gitignore` change**. A rename
to `.beadhive/` is tracked separately as bh-p75df and deliberately not coupled to here.

Every write is best-effort and swallows its errors: a triage store that cannot be written must
never fail, redden, or skew the validation it was observing — the same rule the ledger follows.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from . import observaloop_env, registry, test_report, validation_ledger

#: Under the same canonical root as raw per-run artifacts. `.summary` cannot
#: collide with random ``run-*`` directory names, and legacy `.bh/testreport`
#: is deliberately no longer written.
STORE_REL = ".bh/validation/runs/.summary"
_EXCLUDE_ENTRY = ".bh/"

#: Retained runs per tree. Capped like everything else here — a flake is visible in a handful of
#: runs at one tree, and an unbounded list is an unbounded file.
_MAX_RUNS = 20

#: Retained trees. The dominant cost here is the raw XML — 575 KB for this repo's own suite — so
#: an uncapped directory-per-red-tree store grows forever, which is the one thing the ledger
#: (200 entries, 20 shas, one TTL) refuses to do anywhere. Least-recently-written go first.
_MAX_TREES = 50

_RESULTS = "results.json"
_GATE_LOG = "gate.log"


@contextlib.contextmanager
def gate_log() -> Iterator[Path]:
    """A path to tee the validation command's output to, live, for the duration of one run.

    Its own temp dir, deliberately NOT the drop zone: the drop zone is bh's export to the hive's
    runner and must be empty at exec (bh-ku9n9.20, constraint 2), so bh writing a file into it
    would break the very property that makes a report trustworthy. Removed on exit; :func:`store`
    copies it into the tree's directory first, but only when the write rule fires."""
    with tempfile.TemporaryDirectory(prefix="bh-gatelog-", ignore_cleanup_errors=True) as d:
        yield Path(d) / _GATE_LOG


def tree_dir(entry, tree: str) -> Path | None:
    """`<hive>/.bh/testreport/<tree>` — where bh-ku9n9.8 reads by validation tree."""
    main = registry.hive_dir(entry)
    return Path(main) / STORE_REL / tree if main and tree else None


def runs(entry, rev) -> list[dict]:
    """The retained run records for the tree `rev` names, oldest first — the read-back half of
    this store, and bh-ku9n9.8's whole input for the flake signal. `[]` when this tree has never
    gone red (the common case: no directory, so nothing to read)."""
    dest = tree_dir(entry, validation_ledger.tree_of(entry, rev))
    return _runs(dest / _RESULTS) if dest is not None else []


def _should_write(dest: Path, rc: int) -> bool:
    """THE WRITE RULE: red, or retried (this tree already has triage detail). Never every green.

    The second clause is not an optimisation — it is the flake signal. A green run at a tree that
    previously went red is the single most informative record bh-ku9n9.8 can have, and it exists
    only because the earlier red left a directory behind to notice."""
    return rc != 0 or dest.exists()


def _runs(results: Path) -> list[dict]:
    """The retained run records, oldest first; `[]` on any read/shape problem (a corrupt store is
    an empty store — it must never fail the run that is trying to append to it)."""
    try:
        data = json.loads(results.read_text())
    except (OSError, ValueError):
        return []
    runs = data.get("runs") if isinstance(data, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []


def _cases(report: dict | None, watch: set[str]) -> list[dict]:
    """This run's per-test records: everything that did not pass, plus everything in `watch` (the
    names that did not pass in an earlier retained run at this tree) — so a red→green transition
    is recorded EXPLICITLY rather than inferred from a name's absence. Passing tests that have
    never failed here are dropped; that is what keeps the file bounded by failures, not suite
    size. The records are `test_report.ingest`'s own dicts, OTel `test.*` names untouched."""
    cases = (report or {}).get("cases") or []
    return [
        c
        for c in cases
        if c.get("test.case.result.status") != test_report.PASSED
        or c.get("test.case.name") in watch
    ]


def store(entry, rev, cmd, rc, report, drop, log, *, run_id: str | None = None) -> Path | None:
    """Persist triage detail for the tree `rev` names, iff the write rule fires. Never raises.

    Returns the directory written, or `None` when nothing was (the ordinary green case, a rev
    that names no tree, or any I/O problem). `rc` is passed through untouched and is never
    consulted for anything but the write rule — the run manifest owns the verdict, and this
    store holds detail only."""
    try:
        return _store(entry, rev, cmd, rc, report, drop, log, run_id=run_id)
    except (OSError, ValueError, TypeError):
        return None


def _store(entry, rev, cmd, rc, report, drop, log, *, run_id: str | None = None) -> Path | None:
    if not rev:
        return None
    dest = tree_dir(entry, validation_ledger.tree_of(entry, rev))
    if dest is None or not _should_write(dest, rc):
        return None
    dest.mkdir(parents=True, exist_ok=True)
    # Reuse the mechanism observaloop already uses for `.bh/` rather than touching .gitignore:
    # it resolves the exclude path git consults for THIS checkout, and is best-effort by design.
    observaloop_env._git_exclude(Path(registry.hive_dir(entry)), _EXCLUDE_ENTRY)

    results = dest / _RESULTS
    runs = _runs(results)
    watch = {
        c.get("test.case.name")
        for r in runs
        for c in r.get("cases", [])
        if c.get("test.case.result.status") != test_report.PASSED
    }
    summary = {
        "at": time.time(),
        "sha": rev,  # metadata, exactly as on the run manifest — later-upload join key
        "cmd_hash": validation_ledger.cmd_hash(cmd),
        "rc": int(rc),
        "counts": test_report.counts(report),  # None when the hive opts into no tier 1
        "cases": _cases(report, watch),
    }
    if run_id:
        summary["run_id"] = run_id
    runs.append(summary)
    # tmp + os.replace, as validation control records do. Two runs can be at one
    # tree at once (a seat's `check` and a `submit`'s clean checkout), and a torn results.json
    # reads back as `[]` from _runs — which does not just lose a row, it silently replaces the
    # whole cross-run history with a single run, i.e. exactly the retry record bh-ku9n9.8 is
    # built on. It also makes _prune's mtime rule true: a rename into `dest` bumps `dest`'s
    # mtime on EVERY run, where the in-place write it replaces bumped nothing (see _prune).
    tmp = results.with_name(f"{_RESULTS}.tmp{os.getpid()}")
    payload = {"tree": dest.name, "runs": runs[-_MAX_RUNS:]}
    if run_id:
        payload["latest_raw_run"] = run_id
    tmp.write_text(json.dumps(payload) + "\n")
    os.replace(tmp, results)

    # Raw XML and gate logs already live exactly once in the run directory. The
    # bounded summary index retains its latest raw run id, never another copy.
    _prune(dest.parent)
    return dest


def _prune(root: Path) -> None:
    """Keep the :data:`_MAX_TREES` most recently written tree directories, drop the rest.

    By mtime, which every run bumps on the tree it writes — so the tree being worked on is never
    the one evicted, and "least recently written" is honest for the older trees too. That holds
    only because `results.json` is written tmp + os.replace: the rename touches the directory, and
    `results.json` is the one file EVERY run writes. It was NOT true of the plain in-place write
    that preceded it — truncating a file does not touch its parent's mtime, so a tier-0 hive (no
    machine-readable results, an explicitly supported shape: nothing but results.json and an
    in-place gate.log, no `*.xml` unlink+copy to touch the directory) froze its mtime at first
    write and could evict the tree it had just filed. Measured before the fix: tier-1 repeat run
    bumped, tier-0 repeat run did not.

    An old tree's detail is the least valuable thing here — the content it describes is many
    commits behind — and a store that only grows is a store an operator eventually has to go and
    delete by hand."""
    trees = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for old in trees[:-_MAX_TREES]:
        shutil.rmtree(old, ignore_errors=True)
