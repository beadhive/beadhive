"""bh-ku9n9.20 — `BH_TEST_REPORT_DIR`, the read/trust half of the built-in attestation provider.

The three binding constraints from `docs/design/attested-green-provider-adr.md` ARE the acceptance
of this bead, so each is tested as a HOSTILE case rather than a happy path:

1. **`rc` is authoritative** — a report claiming everything passed is planted alongside a NON-ZERO
   exit code, and the verdict must still be FAILURE. A report may never upgrade a verdict.
2. **The drop zone is fresh and empty immediately before exec** — a green report is planted at the
   exact path a previous run used, and must be unreachable to the next run. A stale green report is
   literally a pass a full run would not have produced.
3. **A missing report is not a failure** — the runner writes nothing at all, and the run must still
   pass with today's rc-only ledger entry, byte for byte.

The end-to-end tests drive the REAL `worktree.clean_checkout` seam with a REAL shell command as
`validate_cmd`, because that is exactly the contract: bh never invokes a runner, it exports a
directory and reads what the hive's own command left there.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from beadhive import config_schema, host, test_report, validation_ledger, worktree

# A pytest-shaped JUnit report claiming a clean sweep. Deliberately all-green: it is the payload
# that would launder a pass if `rc` were ever anything but authoritative.
_ALL_PASSED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="3" errors="0" failures="0" skipped="0" time="0.03">
    <testcase classname="tests.test_a" name="test_one" time="0.01"/>
    <testcase classname="tests.test_a" name="test_two" time="0.01"/>
    <testcase classname="tests.test_b" name="test_three" time="0.01"/>
  </testsuite>
</testsuites>
"""

_MIXED = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="nextest" tests="4">
  <testcase classname="crate::mod" name="ok" time="0.01"/>
  <testcase classname="crate::mod" name="bad" time="0.01"><failure message="boom"/></testcase>
  <testcase classname="crate::mod" name="broke" time="0.01"><error message="panic"/></testcase>
  <testcase name="unclassed" time="0.01"><skipped/></testcase>
</testsuite>
"""


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _hive(tmp_path, monkeypatch):
    """Minimal real hive for a `worktree.clean_checkout()` call — a git clone under
    `GIT_WORKSPACE` plus an isolated worktree root. Self-contained by the same convention
    test_sync_remote.py follows: this file owns its fixtures."""
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    host.mint_if_needed()  # the verify-dir liveness marker keys on host.host_id()
    return {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}, repo


def _runner(log: Path, rc: int = 0, writes: Path | None = None) -> str:
    """A `validate_cmd` standing in for a hive's own pipeline. It records the drop zone bh handed
    it (path + how many entries were already in it), optionally drops a report the way an opted-in
    hive's `--junitxml` would, and exits `rc`. bh is never told any of this."""
    drop = '"$BH_TEST_REPORT_DIR"'
    steps = [f"echo {drop} >> {log}", f"ls -A {drop} | wc -l >> {log}"]
    if writes is not None:
        steps.append(f"cp {writes} {drop}/junit.xml")
    steps.append(f"exit {rc}")
    return "sh -c '" + "; ".join(steps) + "'"


def _observed(log: Path) -> list[tuple[str, int]]:
    """[(drop zone path, entries it already contained), …] — one pair per validation run."""
    lines = log.read_text().split()
    return [(lines[i], int(lines[i + 1])) for i in range(0, len(lines), 2)]


def _entries(repo: Path) -> list[dict]:
    return json.loads((repo / ".git" / validation_ledger.LEDGER_FILENAME).read_text())


# ---------------------------------------------------------------------------
# constraint 1 — rc is authoritative; a report may NEVER upgrade a verdict
# ---------------------------------------------------------------------------


def test_all_passed_report_cannot_upgrade_a_nonzero_rc(tmp_path, monkeypatch, capsys):
    """HOSTILE: the runner drops a report claiming 3/3 passed and then exits 1. Everything the
    provider can see says green; the exit code says red. The verdict must be RED — returned red,
    recorded red, and never reusable — with the report kept only as detail. This is the exact
    laundering the epic exists to prevent: if the report could speak, a run could buy a pass it
    did not earn."""
    entry, repo = _hive(tmp_path, monkeypatch)
    src = tmp_path / "all-passed.xml"
    src.write_text(_ALL_PASSED)
    cmd = _runner(tmp_path / "runs.log", rc=1, writes=src)

    rc = worktree.clean_checkout(entry, "main", cmd)

    assert rc == 1, "a green report upgraded a failing run — the verdict must be the exit code"
    (recorded,) = _entries(repo)
    assert recorded["rc"] == 1
    assert recorded["report"] == {
        "tests": 3,
        "passed": 3,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }, "the report is kept as detail — it is what makes the discrepancy visible, not a verdict"
    assert validation_ledger.green_verdict(entry, "main", cmd) is None, (
        "a red entry carrying an all-passed report became reusable as green"
    )
    err = capsys.readouterr().err
    assert "claims 3 passed / 0 failed" in err and "exited 1" in err, (
        "the discrepancy must be surfaced — the report does not describe the whole gate"
    )


# ---------------------------------------------------------------------------
# constraint 2 — the drop zone is fresh and empty immediately before exec
# ---------------------------------------------------------------------------


def test_a_stale_report_from_a_previous_run_is_unreachable(tmp_path, monkeypatch):
    """HOSTILE: run 1 (green, with a report) is followed by re-planting that very report at the
    exact durable directory bh handed run 1 — then run 2 writes NOTHING. Run 2 must record an
    rc-only verdict: the planted green report is unreachable because run 2 receives a newly
    allocated report directory. A stale green report readable as this run's result would be a
    capability adding a pass a full run would not have produced."""
    entry, repo = _hive(tmp_path, monkeypatch)
    src = tmp_path / "all-passed.xml"
    src.write_text(_ALL_PASSED)
    log = tmp_path / "runs.log"

    assert worktree.clean_checkout(entry, "main", _runner(log, rc=0, writes=src)) == 0
    ((first_drop, _),) = _observed(log)
    assert Path(first_drop).is_dir(), "the completed run's durable raw artifact disappeared"

    # Plant the stale green report back at run 1's exact path, as hostilely as possible.
    (Path(first_drop) / "junit.xml").write_text(_ALL_PASSED)

    # Run 2 writes nothing. Different cmd string ⇒ different ledger key ⇒ it really runs.
    assert worktree.clean_checkout(entry, "main", _runner(log, rc=0) + " # run2") == 0

    (_first, (second_drop, preexisting)) = _observed(log)
    assert second_drop != first_drop, "run 2 was handed run 1's drop zone"
    assert preexisting == 0, "the drop zone bh exported was not empty at exec"
    second = _entries(repo)[-1]
    assert "report" not in second, f"run 2 ingested a report it never wrote: {second.get('report')}"


def test_drop_zones_are_unique_per_run(tmp_path):
    """Freshness is structural, not a clean-up step: two drop zones are never the same directory,
    so concurrent validations cannot read each other's reports either."""
    with test_report.drop_zone() as a, test_report.drop_zone() as b:
        assert a != b
        assert a.is_dir() and b.is_dir()
        assert not list(a.iterdir()) and not list(b.iterdir())
    assert not a.exists() and not b.exists()


# ---------------------------------------------------------------------------
# constraint 3 — a missing report is not a failure (and neither is a broken one)
# ---------------------------------------------------------------------------


def test_a_run_that_writes_no_report_still_passes_and_records_rc_only(tmp_path, monkeypatch):
    """HOSTILE the other way: the hive opts into nothing, so nothing is ever written to the drop
    zone. The run must pass, the verdict must be reusable, and the ledger entry must carry the
    exact key set it carried before this bead — no `report`, nothing new. This is the
    byte-for-byte-today's-behaviour case, which is every hive that has not opted in."""
    entry, repo = _hive(tmp_path, monkeypatch)
    cmd = _runner(tmp_path / "runs.log", rc=0)

    assert worktree.clean_checkout(entry, "main", cmd) == 0

    (recorded,) = _entries(repo)
    assert set(recorded) == {"tree", "cmd_hash", "rc", "at", "host", "sha", "shas"}
    assert recorded["rc"] == 0
    assert validation_ledger.green_verdict(entry, "main", cmd) is not None


def test_a_malformed_report_degrades_instead_of_failing(tmp_path):
    """A truncated or garbage report degrades to rc-only exactly like a missing one. A broken
    report must never turn a green run red — every tier degrades on its own to an un-optimised
    run, and ingest is best-effort by the same rule the ledger already follows."""
    with test_report.drop_zone() as drop:
        (drop / "junit.xml").write_text("<testsuite><testcase ")  # truncated mid-tag
        assert test_report.ingest(drop, 0) is None


def test_an_empty_drop_zone_ingests_to_none(tmp_path):
    with test_report.drop_zone() as drop:
        assert test_report.ingest(drop, 0) is None
        assert test_report.counts(None) is None


# ---------------------------------------------------------------------------
# the export itself, and the parse
# ---------------------------------------------------------------------------


def test_the_variable_is_exported_with_no_opt_in_and_no_bh_config(tmp_path, monkeypatch):
    """Tier 1 costs zero configuration: the variable is exported into every validation subprocess
    unconditionally (the runner above can only log it because bh set it), and NOTHING appears on
    `bh config` — a hive is never asked to describe its runner."""
    entry, _repo = _hive(tmp_path, monkeypatch)
    log = tmp_path / "runs.log"

    assert worktree.clean_checkout(entry, "main", _runner(log, rc=0)) == 0
    ((drop, _),) = _observed(log)
    assert drop and drop != "", "BH_TEST_REPORT_DIR was not exported into the validation child"

    fields = set(config_schema.WorkConfig.model_fields)
    assert not {f for f in fields if "report" in f or "test" in f}, (
        f"tier 1 added an operator-facing key: {fields}"
    )


def test_bh_never_invokes_a_test_runner(tmp_path, monkeypatch):
    """The one scope line of this bead: bh runs `validate_cmd` VERBATIM. No injected flag, no
    framework probe, no decision about HOW tests run — `validate_cmd` is a pipeline (`just check`
    is lint + lint-md + license-check + test), so anything that owned the run would drop legs."""
    entry, _repo = _hive(tmp_path, monkeypatch)
    spawns = []

    def _fake_run(cmd, **kw):
        spawns.append((list(cmd), kw))
        return subprocess.CompletedProcess(cmd, 0, None, None)

    monkeypatch.setattr(worktree, "run", _fake_run)
    worktree.clean_checkout(entry, "main", "just check")

    ((argv, kw),) = [(c, k) for c, k in spawns if c[:1] != ["git"]]
    assert argv == ["just", "check"], "bh altered the hive's validation command"
    assert kw["env"][test_report.ENV_VAR]
    # …and the provider module itself cannot spawn anything: it imports no process seam at all.
    assert not [n for n in ("subprocess", "os", "run") if hasattr(test_report, n)]


def test_ingest_classifies_cases_in_otel_vocabulary(tmp_path):
    """Counts come from classifying each `<testcase>`, and the per-test records use the
    OpenTelemetry `test.*` names (`test.case.name` / `test.case.result.status`) — the only naming
    standard in the space with a stable home. Both `<testsuites>` and a bare `<testsuite>` root
    parse, and multiple files in the drop zone merge into one report."""
    with test_report.drop_zone() as drop:
        (drop / "a.xml").write_text(_ALL_PASSED)
        (drop / "b.xml").write_text(_MIXED)
        report = test_report.ingest(drop, 0)

    assert test_report.counts(report) == {
        "tests": 7,
        "passed": 4,
        "failures": 1,
        "errors": 1,
        "skipped": 1,
    }
    by_name = {c["test.case.name"]: c["test.case.result.status"] for c in report["cases"]}
    assert by_name["tests.test_a::test_one"] == "passed"
    assert by_name["crate::mod::bad"] == "failed"
    assert by_name["crate::mod::broke"] == "error"
    assert by_name["unclassed"] == "skipped"  # classname absent — the id is just the name


def test_export_leaves_the_rest_of_the_environment_untouched(tmp_path):
    base = {"PATH": "/usr/bin", "NO_COLOR": "1"}
    with test_report.drop_zone() as drop:
        env = test_report.export(base, drop)
    assert env == base | {test_report.ENV_VAR: str(drop)}
    assert base == {"PATH": "/usr/bin", "NO_COLOR": "1"}, "export mutated its input"
