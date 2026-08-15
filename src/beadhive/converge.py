"""Converge on failures cheaply, then EARN the verdict with one clean confirming run (bh-ku9n9.8).

This module is the epic's central hazard, made safe. Re-running only the failures until they
pass is exactly how a flaky suite gets laundered into green: a test that fails one run in three
will, under a naive converge loop, *always* eventually "pass", and the ledger would then record
a green verdict for a tree that never once passed cleanly. That is not hypothetical — in-session,
a merge went red on `test_read_fleet_miss_computes_and_persists` and the identical sha then
passed 4,868 tests. Settled decision 1 of `docs/design/attested-green-adr.md` therefore makes
**the confirming run mandatory**, and the whole of this module exists inside that rule:

    red run → record per-test outcomes → re-run ONLY the failures → repeat
      → **CANDIDATE**, which is explicitly not attestable
      → ONE clean full run, no retries → attestation
      → anything that passed only after a retry is recorded and surfaced as **FLAKY**

**So tier 2 saves the gate exactly nothing, and that is by design.** It is a *developer-loop*
capability: it gets a developer from red to knowing-why in seconds instead of ~6 minutes, and it
produces the retry history that makes flakes visible for the first time. The provider ADR reads
in places as though failure-scoped re-run were a gate optimisation; an implementer who follows
that reading and wires :func:`converge` into `clean_checkout` reintroduces precisely the
laundering this bead exists to prevent. It is called from `work check` and from nowhere else.

**The guarantee is structural, not a convention.** :func:`_subset_run` calls
:func:`validation_ledger.seal_subset_run` *before* it spawns anything, which latches the ledger
shut for the life of the process — so no attestation can be written after a subset command has
run, however a future caller wires this up. See that function for why failing in this direction
is safe (a withheld cache entry costs a re-run; a laundered green costs the trust in every
verdict bh has ever written).

**`work.validate_subset` — the only new operator-facing key in the epic.**

```yaml
work:
  validate_subset: "./scripts/hermetic.sh uv run pytest -n auto --pyargs {tests}"
```

* One required placeholder, :data:`PLACEHOLDER`, replaced with the shell-quoted, space-joined
  names bh already holds from the run's own report — never pytest's `--lf`, which reads implicit
  `.pytest_cache` state keyed to nothing and invisible to bh.
* **Absent is the default and fully supported**: no key ⇒ no converge loop ⇒ the phase runs
  whole ⇒ today's behaviour, byte for byte. A value missing the placeholder is an error at
  `bh config set` time (`config._validate`) and reads back as absent, so tier 2 fails **open**
  to the full run, never closed.
* It is `work.validate_subset`, **not** `work.validate.subset`: `work.validate` is a free-form
  `dict[str, str]` keyed by *phase*, so it cannot validate its own keys — a non-phase member
  would need a reserved-word guard forever. A declared field on an `extra="forbid"` model gets
  typo rejection free.

**bh never guesses a selector.** The names passed to the template are the ones the hive's own
runner reported, verbatim — JUnit `classname::name`, which for pytest is the *dotted module*
path (`tests.test_a::test_one`), not a file path. Translating that into a node id is the
template's job, not bh's (`--pyargs` does it for pytest); inventing file paths from dotted names
is exactly the "emulate subsetting by guessing test names" this bead forbids. A hive whose
runner reports nothing machine-readable therefore never converges — there is nothing to name.

Tier 3 (per-test coverage attribution) is **struck from v1**, so the subset is drawn from what
already failed and from nothing else.
"""

from __future__ import annotations

import shlex

import typer

from . import config, config_schema, otel, test_report, triage_store, validation_ledger
from .run import run

#: The single required placeholder in `work.validate_subset`. One source, shared with the schema
#: field's validator and `config._validate`'s write-path check.
PLACEHOLDER = config_schema.SUBSET_PLACEHOLDER

#: Rounds of failure-scoped re-run before the loop gives up. Deliberately small: the loop's value
#: is a fast answer, and a converge that needs many rounds is describing a suite whose failures
#: interact — which is a full-run question, not a subset one. The loop also stops the moment a
#: round makes no progress, so this is a ceiling, not a target.
MAX_ROUNDS = 3


def template(cfg, entry) -> str:
    """`work.validate_subset` for this hive (per-hive > global), or `""` when tier 2 is off.

    A value without :data:`PLACEHOLDER` is one bh cannot fill, so it reads back as absent —
    the fail-**open** rule: an operator's malformed template costs the converge loop, never a
    validation. The write path (`bh config set`) rejects it loudly; this is the load path, which
    a hand-edited `config.yaml` reaches without ever passing through validation."""
    value = str(config.work_value(cfg, entry, "validate_subset", "") or "")
    return value if PLACEHOLDER in value else ""


def failing(report) -> list[str]:
    """The names this run reported as anything but passing — the subset's entire input.

    `None` (a hive that opts into no machine-readable results) yields `[]`, which is what makes
    a tier-0 hive skip the converge loop instead of guessing."""
    seen = {
        name
        for case in (report or {}).get("cases") or []
        if case.get("test.case.result.status") != test_report.PASSED
        and (name := case.get("test.case.name"))
    }
    return sorted(seen)


def subset_cmd(tmpl: str, names) -> str:
    """`tmpl` with :data:`PLACEHOLDER` replaced by the shell-quoted, space-joined `names`."""
    return tmpl.replace(PLACEHOLDER, " ".join(shlex.quote(n) for n in names))


def _subset_run(entry, target, sha, cmd: str):
    """Run one subset command and file its result as triage detail. Returns the ingested report.

    **The seal comes first**, before the spawn: from this point the process can no longer write a
    verdict to the ledger, so a converged result cannot become an attestation even by a later
    caller's mistake. `triage_store.store` is not a verdict — it is the durable per-tree retry
    record (bh-ku9n9.6) that makes the red→green transition explicit, and so is the thing that
    lets a flake be *surfaced* rather than absorbed."""
    validation_ledger.seal_subset_run()
    with test_report.drop_zone() as drop, triage_store.gate_log() as log:
        res = run(
            shlex.split(cmd),
            cwd=str(target),
            check=False,
            env=test_report.export(otel.telemetry_neutral_env(), drop),
            tee=log,
        )
        report = test_report.ingest(drop, res.returncode)
        triage_store.store(entry, sha, cmd, res.returncode, report, drop, log)
    return report


def converge(entry, cfg, target, sha, report) -> dict | None:
    """Re-run only what failed, up to :data:`MAX_ROUNDS` times, to a CANDIDATE — never a verdict.

    Returns `None` when the loop does not apply at all — no `work.validate_subset`, or no named
    failures to re-run (a tier-0 hive, or a red gate whose failure was not a test at all: a lint
    leg, a missing binary). Both are the supported fall-back: run the phase whole.

    Otherwise returns `{"rounds", "candidate_green", "flaky", "still_failing"}` and prints it.
    `candidate_green` is the loud one: it means every named failure passed on a retry, which is
    **not** a pass. The caller's exit code is the full run's and is not touched here."""
    tmpl, names = template(cfg, entry), failing(report)
    if not tmpl or not names:
        return None
    typer.echo(
        f"  ⟳ converging on {len(names)} failure(s) — re-running only those, "
        f"via work.validate_subset. This is a DEVELOPER LOOP: it can never produce a verdict."
    )
    flaky: list[str] = []
    rounds = 0
    while rounds < MAX_ROUNDS:
        rounds += 1
        rerun = _subset_run(entry, target, sha, subset_cmd(tmpl, names))
        if rerun is None:
            # The subset command reported nothing machine-readable — it may not even have run
            # the tests we named. Stop rather than loop blind on an unchanged set.
            typer.echo("     the subset run reported no results — stopping (run the phase whole)")
            break
        still = failing(rerun)
        passed = [n for n in names if n not in still]
        flaky += [n for n in passed if n not in flaky]
        typer.echo(f"     round {rounds}: {len(passed)}/{len(names)} of them now pass")
        if not still:
            names = []
            break
        if set(still) == set(names):
            break  # no progress: re-running the same set again only burns time
        names = still
    result = {
        "rounds": rounds,
        "candidate_green": not names,
        "flaky": flaky,
        "still_failing": list(names),
    }
    _report(result)
    return result


def _report(result: dict) -> None:
    """The operator-facing half. A candidate is announced as loudly as it is refused: this is the
    exact point where a reader could otherwise conclude the tree is green."""
    if result["still_failing"]:
        typer.echo(
            f"  ✗ still failing after {result['rounds']} round(s): "
            + ", ".join(result["still_failing"])
        )
    if result["candidate_green"]:
        typer.echo(
            "  ⚑ CANDIDATE GREEN — NOT an attestation, and nothing was recorded green. Every "
            "failure passed only on RETRY, which is the signature of a flaky suite, not of a "
            "fixed one. Run the full gate once, clean, to earn a verdict."
        )
    if result["flaky"]:
        typer.echo(
            "  ⚑ FLAKY at identical content (failed, then passed with no change): "
            + ", ".join(result["flaky"])
        )


def flakes(entry, rev) -> list[str]:
    """Tests that passed only after a retry at the tree `rev` names, oldest evidence first.

    Read from bh-ku9n9.6's durable per-tree store, which records a red→green transition
    **explicitly**: its `cases` carry every name that was non-passing in an earlier retained run
    at this tree, so a name recorded `passed` in a later run at the same tree is a flake at
    byte-identical content — never inferred from an absence, and never a heuristic."""
    failed: set[str] = set()
    flaky: list[str] = []
    for r in triage_store.runs(entry, rev):
        for case in r.get("cases") or []:
            name = case.get("test.case.name")
            if not name:
                continue
            if case.get("test.case.result.status") != test_report.PASSED:
                failed.add(name)
            elif name in failed and name not in flaky:
                flaky.append(name)
    return flaky


def warn_flakes(entry, rev, rc: int) -> list[str]:
    """Surface this tree's flakes on a GREEN run — the confirming run that earns the attestation.

    This is where "recorded and surfaced as flaky rather than silently absorbed" actually
    happens: the run is green, the verdict is honest, and the operator is nevertheless told that
    some of it took a retry to get there. Red runs say nothing here — the failures are on screen.
    Best-effort like every other read of this store: a flake report must never fail a run."""
    if rc != 0:
        return []
    try:
        names = flakes(entry, rev)
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    if names:
        typer.echo(
            f"  ⚑ green — but {len(names)} test(s) here passed only after a retry at this exact "
            f"tree: {', '.join(names)}. FLAKY, not fixed."
        )
    return names
