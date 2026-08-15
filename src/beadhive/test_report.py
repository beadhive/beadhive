"""`BH_TEST_REPORT_DIR` — the read/trust half of the built-in attestation provider (bh-ku9n9.20).

bh **never invokes a test runner**. `work.validate_cmd` is a *pipeline*, not a test invocation
(`justfile:53` — `check: lint lint-md license-check test`; `check-all` adds
`test-integration-land` + `demo-local-loop`, and roughly two thirds of the measured 371 s gate is
not pytest at all), so any adapter that owned the run would either drop legs — violating the
epic's INVARIANT — or have to be told the pipeline per hive. That is why Option A was rejected and
Option B ruled GO in `docs/design/attested-green-provider-adr.md`. All bh does is:

1. create an **empty directory**,
2. name it in `BH_TEST_REPORT_DIR` in the validation subprocess's environment,
3. parse whatever JUnit XML turns up there when the command returns.

**Zero bh config.** The variable is exported into every validation subprocess unconditionally.
A hive opts in from its own already-maintained test config — `addopts = --junitxml=…` in
`pyproject.toml`, `[profile.default.junit]` in `.config/nextest.toml`, a `reporters` entry in
`vitest.config.ts`. Nothing appears in `bh config`, and a hive that opts into nothing gets
today's behaviour byte for byte: the variable is exported, nothing writes to the directory,
:func:`ingest` returns ``None`` and the ledger entry stays rc-only.

The three binding constraints, which are this module's whole reason to exist:

1. **`rc` is authoritative.** The report is *detail*, never a verdict. A report claiming 4,877
   passed against a non-zero `rc` is a discrepancy to surface — the verdict stays FAILURE.
   **A report may never upgrade a verdict.** Nothing here returns or mutates an exit code;
   :func:`ingest` takes `rc` only so it can warn about the discrepancy.
2. **The drop zone is fresh and empty immediately before exec.** A stale green report from a
   previous run is literally a pass a full run would not have produced. :func:`drop_zone` is
   `tempfile.TemporaryDirectory`, so freshness is structural, not a clean-up step that can be
   skipped: each run gets its own brand-new directory that has never held a file, and it is
   removed afterwards. Concurrent validations therefore cannot see each other's reports either.
   The one-directory alternative (a fixed path with a unique filename per run) is worse — it
   forces ingest to decide *which* file this run wrote, which is exactly the trust hole this
   constraint closes. The durable per-tree store under `.bh/testreport/<tree>/` is a **different
   directory** and belongs to bh-ku9n9.6: clearing one and retaining the other are both required
   and cannot be the same path.
3. **A missing report is not a failure.** No report ⇒ ``None`` ⇒ rc-only ledger entry ⇒ today's
   behaviour. No warning, no nudge, no config. Every tier degrades on its own to an un-optimised
   run; a parse error degrades the same way.

**Stdlib `xml.etree`, and deliberately NOT `defusedxml`** — bh-ku9n9.4's reviewer weighed the
billion-laughs exposure and ruled the dependency theatre here: the report is written by a process
bh already executes as arbitrary shell via `validate_cmd`. Same trust domain, strictly less
powerful. Adding a parser hardening step against input from a shell you already ran buys nothing.

Cost, measured on this repo's own 573,785-byte report: **~24 ms / +4.4 MiB peak** to parse, against
a 371 s gate — 0.006 %. There is no hot path here to optimise, which is why the ADR ruled Python
against a stated Rust preference.
"""

from __future__ import annotations

import contextlib
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import typer

#: The one variable. Exported into every validation subprocess, always, with no opt-in.
ENV_VAR = "BH_TEST_REPORT_DIR"

#: JUnit `<testcase>` child tag → OTel `test.case.result.status`. Anything else is a pass.
_STATUS = {"failure": "failed", "error": "error", "skipped": "skipped"}

#: Count keys on an ingested report, in the order they are reported. `cases` is deliberately NOT
#: among them: per-test records in the 200-entry ledger would cost ~96 MiB per hive (bh-ku9n9.4,
#: Evidence 9), so callers persisting a verdict keep the counts and drop the list.
COUNT_KEYS = ("tests", "passed", "failures", "errors", "skipped")


@contextlib.contextmanager
def drop_zone() -> Iterator[Path]:
    """A fresh, empty, unique directory for the duration of one validation run (constraint 2).

    Freshness is structural: `mkdtemp` creates a directory that has never existed, so a report
    left by a previous run — or by a concurrent one — is not merely cleared, it is unreachable
    by construction. Removed on exit; clean-up failures are ignored because a drop zone that
    won't delete must never fail the validation it observed."""
    with tempfile.TemporaryDirectory(prefix="bh-testreport-", ignore_cleanup_errors=True) as d:
        yield Path(d)


def export(env: dict[str, str], drop: Path) -> dict[str, str]:
    """`env` plus `BH_TEST_REPORT_DIR` pointing at `drop`. The entire opt-in surface."""
    return {**env, ENV_VAR: str(drop)}


def ingest(drop: Path, rc: int) -> dict | None:
    """Parse every JUnit XML file in `drop` into one report, or ``None`` if the run wrote none.

    ``None`` is the *normal* answer for a hive that has not opted in, and is never a failure
    (constraint 3). A malformed or unreadable file is skipped for the same reason — degrading to
    an rc-only verdict is always available, and a broken report must not turn a green run red.

    `rc` is **not** consulted to build the report and is never modified (constraint 1): it is
    taken only to surface the one discrepancy worth an operator's attention — a report claiming
    everything passed while the command failed, which means the report does not describe the
    whole gate. The caller's exit code is untouched either way.
    """
    report = {k: 0 for k in COUNT_KEYS} | {"cases": []}
    for path in sorted(drop.glob("*.xml")):
        # Stdlib parse, deliberately: the file was written by a process bh already ran as
        # arbitrary shell, so defusedxml would harden against strictly less than it already
        # trusts (bh-ku9n9.4 — ruled theatre, not a dependency).
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        for case in root.iter("testcase"):
            status = next((s for tag, s in _STATUS.items() if case.find(tag) is not None), "passed")
            report["tests"] += 1
            report[{"failed": "failures", "error": "errors"}.get(status, status)] += 1
            report["cases"].append(
                {
                    "test.case.name": "::".join(
                        p for p in (case.get("classname"), case.get("name", "")) if p
                    ),
                    "test.case.result.status": status,
                }
            )
    if not report["tests"]:
        return None
    if rc != 0 and not report["failures"] and not report["errors"]:
        # Constraint 1 made visible. The verdict is already FAILURE and stays FAILURE; what the
        # operator needs to know is that the report does not account for it — another leg of the
        # pipeline failed, or the runner never reached the failing tests.
        typer.echo(
            f"  ⚠ test report claims {report['passed']} passed / 0 failed but the validation "
            f"command exited {rc} — the verdict is the exit code; the report describes only "
            f"part of the gate.",
            err=True,
        )
    return report


def counts(report: dict | None) -> dict | None:
    """`report` without its per-test `cases` list — the digest-sized part a verdict may carry."""
    return None if report is None else {k: report[k] for k in COUNT_KEYS}
