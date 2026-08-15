"""bh-ku9n9.6 — the durable per-tree triage store, `.bh/testreport/<tree>/`.

The write side of the attestation provider. bh-ku9n9.20 (`tests/test_attestation_provider.py`)
owns the read/trust side and its three binding constraints; this file owns what is PERSISTED,
where, and what reads it back. Its acceptance is four claims, each tested against the case that
would break it rather than the one that flatters it:

1. **The verdict row stays small.** The measured split is ~242 B for a digest-only green
   attestation against ~577 KB for one run's per-test JUnit XML — ~2,000×, which at the ledger's
   200-entry cap is ~96 MiB per hive. So the ledger row must gain NOTHING from this bead, on a
   green run and on a red one alike. The join is the `tree` field it already carries.
2. **Red and retried runs only.** A tree that has only ever been green must leave no directory
   at all; a red run must create one; and a green run at a tree that has previously gone red
   must write, because that transition IS bh-ku9n9.8's flake signal.
3. **The gate log survives.** Today nothing tees it, so a red 6-minute gate can only be examined
   by burning another 6. It must be captured even for a hive with no machine-readable results —
   `gate.log` needs no tier 1 — and the run's output must still stream while it happens.
4. **OTel `test.*` vocabulary**, and per-test records that make a flake unambiguous without
   having to interpret a name's absence.

Like `test_attestation_provider.py`, the end-to-end cases drive the REAL `worktree.clean_checkout`
seam with a REAL shell command as `validate_cmd`: bh never invokes a runner, it exports a
directory, reads what appears, and files it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import beadhive
from beadhive import host, test_report, triage_store, validation_ledger, worktree

#: The child's pause between its two lines in the streaming test. Long enough that a
#: flush-at-exit regression collapses the gap to ~0 and fails, short enough to stay cheap.
_STREAM_SLEEP = 1.0

_GREEN = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2">
  <testcase classname="tests.test_a" name="test_one" time="0.25"/>
  <testcase classname="tests.test_a" name="test_flaky" time="1.50"/>
</testsuite>
"""

_RED = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2">
  <testcase classname="tests.test_a" name="test_one" time="0.25"/>
  <testcase classname="tests.test_a" name="test_flaky" time="1.50">
    <failure message="boom"/>
  </testcase>
</testsuite>
"""


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _hive(tmp_path, monkeypatch):
    """Minimal real hive for a `worktree.clean_checkout()` call (same shape as
    `tests/test_attestation_provider.py::_hive` — each file owns its fixtures)."""
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

    host.mint_if_needed()
    return {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}, repo


def _runner(rc: int, writes: Path | None = None, says: str = "gate output line") -> str:
    """A hive's own `validate_cmd`: prints (so there is something to tee), optionally drops a
    JUnit report the way an opted-in hive's `--junitxml` would, and exits `rc`."""
    steps = [f"echo {says}"]
    if writes is not None:
        steps.append(f'cp {writes} "$BH_TEST_REPORT_DIR"/junit.xml')
    steps.append(f"exit {rc}")
    return "sh -c '" + "; ".join(steps) + "'"


def _store(repo: Path) -> Path:
    return repo / triage_store.STORE_REL


def _dirs(repo: Path) -> list[Path]:
    root = _store(repo)
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


def _results(repo: Path) -> dict:
    (d,) = _dirs(repo)
    return json.loads((d / "results.json").read_text())


def _ledger(repo: Path) -> list[dict]:
    return json.loads((repo / ".git" / validation_ledger.LEDGER_FILENAME).read_text())


def _xml(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# claim 2 — the write rule: red and retried runs only, never every green
# ---------------------------------------------------------------------------


def test_a_green_run_at_a_never_red_tree_writes_no_triage_detail(tmp_path, monkeypatch):
    """The common case, and the one the whole split exists for: a passing gate leaves NOTHING
    durable behind. Not an empty directory, not a results.json with a green run in it — nothing.
    A store that recorded every green would be the ~96 MiB-per-hive problem in a new location."""
    entry, repo = _hive(tmp_path, monkeypatch)
    src = _xml(tmp_path, "green.xml", _GREEN)

    assert worktree.clean_checkout(entry, "main", _runner(0, writes=src)) == 0

    assert not _store(repo).exists(), "a green run at a clean tree left triage detail behind"


def test_a_red_run_writes_the_full_triage_set(tmp_path, monkeypatch):
    """Red is the run worth keeping. One directory named for the TREE, holding the accumulating
    results.json, the runner's raw XML verbatim, and the gate log that today survives nowhere."""
    entry, repo = _hive(tmp_path, monkeypatch)
    src = _xml(tmp_path, "red.xml", _RED)

    assert worktree.clean_checkout(entry, "main", _runner(1, writes=src)) == 1

    (d,) = _dirs(repo)
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main^{tree}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert d.name == tree, "the store is keyed by the TREE, the same key the ledger row carries"
    assert (d / "junit.xml").read_text() == _RED, "the raw runner output was not kept verbatim"
    assert "gate output line" in (d / "gate.log").read_text()
    assert _results(repo)["runs"][-1]["rc"] == 1


def test_a_green_run_after_a_red_one_at_the_same_tree_is_recorded(tmp_path, monkeypatch):
    """THE FLAKE SIGNAL, and the reason a per-run store cannot serve bh-ku9n9.8: identical
    content, red then green. The retry must be appended to the SAME tree's record — and
    `test_flaky` must appear in the second run explicitly as `passed`, not merely be missing
    from it, so a flake never has to be inferred from an absence."""
    entry, repo = _hive(tmp_path, monkeypatch)
    red, green = _xml(tmp_path, "red.xml", _RED), _xml(tmp_path, "green.xml", _GREEN)

    assert worktree.clean_checkout(entry, "main", _runner(1, writes=red)) == 1
    assert worktree.clean_checkout(entry, "main", _runner(0, writes=green) + " # retry") == 0

    runs = _results(repo)["runs"]
    assert [r["rc"] for r in runs] == [1, 0], "the retry did not land in the same tree's record"
    flaky = "tests.test_a::test_flaky"
    assert {c["test.case.name"]: c["test.case.result.status"] for c in runs[0]["cases"]} == {
        flaky: "failed"
    }, "the red run recorded more than the tests that needed triage"
    assert {c["test.case.name"]: c["test.case.result.status"] for c in runs[1]["cases"]} == {
        flaky: "passed"
    }, "red→green at identical content was not recorded explicitly — the flake is invisible"
    assert len(_dirs(repo)) == 1, "the retry was filed under a second directory"


# ---------------------------------------------------------------------------
# claim 1 — the verdict row stays small: the ledger gains nothing from this bead
# ---------------------------------------------------------------------------


def test_the_ledger_row_is_unchanged_by_the_triage_store(tmp_path, monkeypatch):
    """The split, enforced: after a red run that produced a full triage directory, the ledger
    entry carries exactly the keys bh-ku9n9.20 left it with — no path, no pointer, no marker.
    `tree` is already the join key into the store, so the growth this bead adds is ZERO bytes."""
    entry, repo = _hive(tmp_path, monkeypatch)
    src = _xml(tmp_path, "red.xml", _RED)

    assert worktree.clean_checkout(entry, "main", _runner(1, writes=src)) == 1

    (row,) = _ledger(repo)
    assert set(row) == {"tree", "cmd_hash", "rc", "at", "host", "sha", "shas", "report"}
    assert row["report"] == {"tests": 2, "passed": 1, "failures": 1, "errors": 0, "skipped": 0}
    assert "cases" not in row["report"], "per-test records leaked into the 200-entry ledger"
    assert (
        _store(
            repo,
        )
        .joinpath(row["tree"])
        .is_dir()
    ), "the ledger's tree does not join"


# ---------------------------------------------------------------------------
# claim 3 — the gate log survives, with or without machine-readable results
# ---------------------------------------------------------------------------


def test_a_hive_with_no_machine_readable_results_still_gets_its_gate_log(tmp_path, monkeypatch):
    """Tier-1 degradation, from the store's side: the runner writes no report at all, so the
    ledger stays rc-only exactly as before — but the operator still gets the full output of the
    red gate instead of having to re-run it. `gate.log` requires no opt-in from the hive."""
    entry, repo = _hive(tmp_path, monkeypatch)

    assert worktree.clean_checkout(entry, "main", _runner(1, says="boom-and-no-report")) == 1

    (d,) = _dirs(repo)
    assert "boom-and-no-report" in (d / "gate.log").read_text()
    assert not list(d.glob("*.xml"))
    run = _results(repo)["runs"][-1]
    assert run["counts"] is None and run["cases"] == []
    assert "report" not in _ledger(repo)[0], "an absent report added a ledger key"


def test_the_teed_gate_streams_line_by_line_when_bhs_own_stdout_is_a_pipe(tmp_path):
    """Teeing must not turn a minutes-long gate silent — and the seat that matters is a PIPE, not
    a tty: an agent running `bh work check` through a tool, `bh work submit > log`, `| tee`. On a
    pipe Python BLOCK-buffers stdout, so a flush-only-at-exit prints nothing for the gate's whole
    duration and then dumps at once — immediately after clean_checkout has told the operator to
    wait for it synchronously rather than background it.

    A `capfd`-style "did the text eventually appear" assertion cannot see that, so this drives
    `run(tee=…)` from a real subprocess whose stdout is a pipe and times the ARRIVALS: the child
    prints, sleeps, prints. Under per-line flush the lines arrive a sleep apart; under
    flush-at-exit they arrive together at the end. Timing the GAP between the two lines rather
    than the first line's absolute latency keeps interpreter startup out of the measurement."""
    driver = tmp_path / "driver.py"
    driver.write_text(
        "from beadhive.run import run\n"
        f"run(['sh', '-c', 'echo START; sleep {_STREAM_SLEEP}; echo END'],"
        f" check=False, tee={str(tmp_path / 'gate.log')!r})\n"
    )
    # PYTHONUNBUFFERED would defeat the very buffering under test if the ambient seat set it.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env["PYTHONPATH"] = str(Path(beadhive.__file__).resolve().parent.parent)

    seen = {}
    with subprocess.Popen(
        [sys.executable, str(driver)],
        stdout=subprocess.PIPE,  # a PIPE, deliberately: this is the condition under test
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    ) as proc:
        for line in proc.stdout:
            seen[line.strip()] = time.monotonic()

    assert "START" in seen and "END" in seen, f"the tee'd child's output never arrived: {seen}"
    gap = seen["END"] - seen["START"]
    assert gap > _STREAM_SLEEP / 2, (
        f"the two lines arrived {gap:.3f}s apart though the child slept {_STREAM_SLEEP}s between "
        "them — the gate was buffered up and dumped at exit, not streamed"
    )
    assert "START" in (tmp_path / "gate.log").read_text(), "streaming cost the tee its content"


def test_the_store_is_hidden_by_the_git_exclude_mechanism(tmp_path, monkeypatch):
    """`.bh/` is hidden the way observaloop already hides it — the worktree's git exclude, not
    the tracked .gitignore — so a hive gains an untracked store and no repo diff at all."""
    entry, repo = _hive(tmp_path, monkeypatch)

    assert worktree.clean_checkout(entry, "main", _runner(1)) == 1

    assert ".bh/" in (repo / ".git" / "info" / "exclude").read_text()
    assert not (repo / ".gitignore").exists(), "the store touched the tracked .gitignore"
    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert dirty == "", f"the triage store shows up in git status: {dirty!r}"


# ---------------------------------------------------------------------------
# claim 4 — OTel vocabulary and durations; and the unit-level rules
# ---------------------------------------------------------------------------


def test_per_test_records_use_otel_names_and_carry_durations(tmp_path):
    """The records are `test_report.ingest`'s own dicts — `test.case.name` /
    `test.case.result.status`, plus the duration in the same namespace rather than a second
    vocabulary. A runner that emits no `time=` degrades to `None`, never to an error."""
    with test_report.drop_zone() as drop:
        (drop / "j.xml").write_text(_RED)
        (drop / "k.xml").write_text(
            '<testsuite><testcase name="notime"><failure/></testcase></testsuite>'
        )
        report = test_report.ingest(drop, 1)

    by_name = {c["test.case.name"]: c for c in report["cases"]}
    assert by_name["tests.test_a::test_flaky"]["test.case.duration"] == 1.5
    assert by_name["notime"]["test.case.duration"] is None


def test_the_run_history_is_capped(tmp_path, monkeypatch):
    """Bounded like everything else here: a tree that keeps failing does not grow an unbounded
    file. The newest runs are the ones kept."""
    entry, repo = _hive(tmp_path, monkeypatch)
    for i in range(triage_store._MAX_RUNS + 3):
        worktree.clean_checkout(entry, "main", _runner(1) + f" # run{i}")

    runs = _results(repo)["runs"]
    assert len(runs) == triage_store._MAX_RUNS
    assert runs[-1]["cmd_hash"] == validation_ledger.cmd_hash(
        _runner(1) + f" # run{triage_store._MAX_RUNS + 2}"
    )


def test_the_number_of_retained_trees_is_capped(tmp_path, monkeypatch):
    """The dominant cost is the raw XML (575 KB for this repo's suite), so a directory per red
    tree that is never evicted grows forever. Oldest-written go first, and the tree the current
    run is filing under is never the one evicted."""
    entry, repo = _hive(tmp_path, monkeypatch)
    dest = _store(repo)
    dest.mkdir(parents=True)
    for i in range(triage_store._MAX_TREES + 5):
        stale = dest / f"{i:040d}"
        stale.mkdir()
        os.utime(stale, (0, i))  # oldest first, deterministically

    assert worktree.clean_checkout(entry, "main", _runner(1)) == 1

    kept = _dirs(repo)
    assert len(kept) == triage_store._MAX_TREES
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main^{tree}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert (dest / tree).is_dir(), "the run evicted the very tree it was filing"
    assert dest / f"{0:040d}" not in kept, "the oldest tree survived the cap"


def test_a_repeat_run_bumps_its_tree_dir_even_with_no_machine_readable_results(
    tmp_path, monkeypatch
):
    """The cap evicts by mtime, so "the tree being worked on is never evicted" holds only if
    every run actually bumps it. The case above uses a FRESH dest, where `mkdir` bumps it for
    free; this is the one that caught a real hole. On a REPEAT run at an existing tree, a tier-0
    hive (no machine-readable results) rewrites results.json and gate.log IN PLACE — and
    truncating a file does not touch its parent's mtime — so before results.json became
    tmp + os.replace, such a hive froze its dir mtime at first write and past _MAX_TREES red
    trees could evict the tree it had just filed. The rename is what bumps it."""
    entry, repo = _hive(tmp_path, monkeypatch)
    assert worktree.clean_checkout(entry, "main", _runner(1)) == 1
    (d,) = _dirs(repo)
    assert not list(d.glob("*.xml")), "tier 0: nothing but results.json and gate.log"
    os.utime(d, (0, 0))

    assert worktree.clean_checkout(entry, "main", _runner(1)) == 1

    assert d.stat().st_mtime > time.time() - 300, (
        "a repeat tier-0 run left its tree dir's mtime frozen — the cap can evict the tree the "
        "run just filed"
    )
    assert len(_results(repo)["runs"]) == 2, "the second run was not appended"


def test_a_corrupt_results_file_never_fails_the_run_it_is_appending_to(tmp_path, monkeypatch):
    """Best-effort, like the ledger: a store that cannot be read is an empty store, and the
    validation it was merely observing is unaffected."""
    entry, repo = _hive(tmp_path, monkeypatch)
    assert worktree.clean_checkout(entry, "main", _runner(1)) == 1
    (d,) = _dirs(repo)
    (d / "results.json").write_text("{not json")

    assert worktree.clean_checkout(entry, "main", _runner(1) + " # again") == 1
    assert len(_results(repo)["runs"]) == 1, "the corrupt file was not replaced from scratch"


def test_store_is_a_no_op_for_a_rev_that_names_no_tree(tmp_path, monkeypatch):
    """No rev, no tree, no directory — and no exception. Nothing here may raise into a caller
    that is in the middle of reporting a validation result."""
    entry, repo = _hive(tmp_path, monkeypatch)
    assert triage_store.store(entry, "", "just check", 1, None, None, None) is None
    assert not _store(repo).exists()
