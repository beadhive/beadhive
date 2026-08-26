"""bh-ku9n9.8 — converge on failures cheaply, then EARN the verdict with one clean full run.

This is the epic's central hazard made safe, so the tests are written against the way it would
be *broken*, not the way it is meant to work:

1. **No attestation may EVER come from a run that consulted `work.validate_subset`** — the
   binding acceptance, and it is proved by trying to write one right after a subset run rather
   than by reading the call graph. Re-running only the failures until they pass is exactly how a
   flaky suite is laundered into green; the measured precedent is a merge that went red on
   `test_read_fleet_miss_computes_and_persists` whose identical sha then passed 4,868 tests.
2. **The gate never consults it at all.** Tier 2 saves the confirming run nothing (settled
   decision 1 makes that run mandatory), so it is a developer-loop capability. A test drives the
   real `clean_checkout` gate with a subset template configured and asserts the template was
   never even read.
3. **A flake is surfaced, not absorbed** — read from bh-ku9n9.6's durable per-tree store, where a
   red→green transition at identical content is recorded EXPLICITLY as `passed`, so it never has
   to be inferred from a name's absence.
4. **Absent, malformed, or unusable ⇒ run the phase whole.** No key, a template without
   `{tests}`, or a runner that names no failing test all fall open to today's behaviour, and bh
   never invents a selector it was not given.

Like `tests/test_triage_store.py`, the end-to-end cases drive the REAL seams (`work.check`,
`worktree.clean_checkout`) with REAL shell commands: bh never invokes a runner, it names a
directory and reads what appears.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import typer

from beadhive import config, converge, host, triage_store, validation_ledger, work, worktree
from harness.validation_state import pointer as verdict_pointer

_FULL_CMD = "the-full-gate"  # a stand-in validate_cmd; never actually spawned where it appears


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _hive(tmp_path, monkeypatch):
    """Minimal real hive — same shape as `tests/test_triage_store.py::_hive`."""
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


def _junit(passed=(), failed=()) -> str:
    """A runner's JUnit XML. `classname::name` is what bh will hold and hand back verbatim."""
    cases = [f'  <testcase classname="tests.test_a" name="{n}" time="0.1"/>' for n in passed]
    cases += [
        f'  <testcase classname="tests.test_a" name="{n}" time="0.1">'
        f'<failure message="boom"/></testcase>'
        for n in failed
    ]
    body = "\n".join(cases)
    return f'<?xml version="1.0"?>\n<testsuite name="pytest">\n{body}\n</testsuite>\n'


def _name(short: str) -> str:
    return f"tests.test_a::{short}"


def _full_runner(xml: Path, rc: int) -> str:
    """A hive's own `validate_cmd`: prints, drops a report, exits `rc`. bh never invokes a
    runner — it exports `BH_TEST_REPORT_DIR` and reads whatever turns up."""
    return f"sh -c 'echo gate output; cp {xml} \"$BH_TEST_REPORT_DIR\"/junit.xml; exit {rc}'"


def _subset(tmp_path: Path, rounds: list[str]) -> tuple[str, Path]:
    """A `work.validate_subset` template plus the file recording what it was asked to re-run.

    `rounds[i]` is the JUnit XML the i-th subset invocation reports; the last entry repeats for
    any further round. The script exits non-zero iff its report contains a failure, so rc and
    report agree the way a real runner's would."""
    d = tmp_path / "subset"
    d.mkdir(exist_ok=True)
    for i, body in enumerate(rounds, start=1):
        (d / f"round{i}.xml").write_text(body)
    (d / "last.xml").write_text(rounds[-1])
    script = d / "rerun.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{d}/n" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "{d}/n"\n'
        f'echo "$@" >> "{d}/calls"\n'
        f'xml="{d}/round$n.xml"; [ -f "$xml" ] || xml="{d}/last.xml"\n'
        'cp "$xml" "$BH_TEST_REPORT_DIR"/junit.xml\n'
        'if grep -q "<failure" "$xml"; then exit 1; else exit 0; fi\n'
    )
    return f"sh {script} {{tests}}", d / "calls"


def _calls(marker: Path) -> list[list[str]]:
    """One entry per subset invocation, each the list of test names it was handed."""
    return [line.split() for line in marker.read_text().splitlines()] if marker.exists() else []


def _results(repo: Path) -> dict:
    root = repo / triage_store.STORE_REL
    (d,) = [p for p in root.iterdir() if p.is_dir()]
    return json.loads((d / "results.json").read_text())


def _check(monkeypatch, entry, repo, cfg):
    """Drive the REAL `work.check` against `repo` as its (clean) seat worktree."""
    monkeypatch.setattr(worktree, "locate", lambda *_a, **_k: (entry, repo, repo, "b"))
    monkeypatch.setattr(work, "_batch_worktree", lambda *_a, **_k: ("", None))
    monkeypatch.setattr(worktree, "in_bead_worktree", lambda _p: True)
    monkeypatch.setattr(config, "load", lambda: cfg)
    with pytest.raises(typer.Exit) as exc:
        work.check(bead="mr-1", hive="myrepo")
    return exc.value.exit_code


# ---------------------------------------------------------------------------
# THE binding acceptance: a converged result can never become an attestation
# ---------------------------------------------------------------------------


def test_no_attestation_can_be_written_after_a_subset_run(tmp_path, monkeypatch):
    """THE test this bead exists for, and it is deliberately hostile: after converging to a
    candidate green, record a GREEN verdict for that very tree with the full gate's own command —
    the single most direct route to laundering a flaky suite into an attestation — and show the
    ledger refuses it. Proved by attempting the write, not by inspecting who calls what."""
    entry, repo = _hive(tmp_path, monkeypatch)
    sha = worktree.head_full_sha(repo)
    tmpl, _ = _subset(tmp_path, [_junit(passed=["test_flaky"])])
    cfg = {"work": {"validate_subset": tmpl}}
    red = {"cases": [{"test.case.name": _name("test_flaky"), "test.case.result.status": "failed"}]}

    result = converge.converge(entry, cfg, repo, sha, red)
    assert result["candidate_green"] is True, "the retry passed — this is the dangerous case"

    validation_ledger.record(entry, sha, _FULL_CMD, 0)  # the laundering attempt, made explicit

    assert validation_ledger.green_verdict(entry, sha, _FULL_CMD) is None
    assert not verdict_pointer(entry, sha, _FULL_CMD).exists(), (
        "a reusable verdict was written after a subset run"
    )


def test_check_converges_to_a_candidate_and_leaves_no_verdict_behind(tmp_path, monkeypatch):
    """End to end through the real `work.check`: the full gate goes red on one test, the converge
    loop re-runs ONLY that test, and it passes. Three things must all hold at once — the retry is
    RECORDED (it is the flake signal), `check` still EXITS RED (a candidate is not a pass), and
    the ledger holds NO verdict for the tree (it is not an attestation)."""
    entry, repo = _hive(tmp_path, monkeypatch)
    red_xml = tmp_path / "red.xml"
    red_xml.write_text(_junit(passed=["test_solid"], failed=["test_flaky"]))
    tmpl, calls = _subset(tmp_path, [_junit(passed=["test_flaky"])])
    cfg = {"work": {"validate_cmd": _full_runner(red_xml, 1), "validate_subset": tmpl}}

    assert _check(monkeypatch, entry, repo, cfg) == 1, "a converged candidate must still exit red"

    assert _calls(calls) == [[_name("test_flaky")]], "the subset run got the wrong selection"
    runs = _results(repo)["runs"]
    assert [r["rc"] for r in runs] == [1, 0], "the retry was not recorded at this tree"
    assert {c["test.case.name"]: c["test.case.result.status"] for c in runs[-1]["cases"]} == {
        _name("test_flaky"): "passed"
    }, "red→green at identical content was not recorded explicitly"
    assert not verdict_pointer(
        entry, worktree.head_full_sha(repo), cfg["work"]["validate_cmd"]
    ).exists()


def test_the_gate_never_consults_validate_subset(tmp_path, monkeypatch, capsys):
    """Tier 2 is a DEVELOPER LOOP, not a gate optimisation — the artifact reads otherwise and an
    implementer who follows that reading reintroduces the laundering. The real gate seam
    (`clean_checkout`, which is what submit/merge/finish run) must go red and stop: no subset
    command, and the ledger not even sealed, because it never got near the key."""
    entry, repo = _hive(tmp_path, monkeypatch)
    red_xml = tmp_path / "red.xml"
    red_xml.write_text(_junit(failed=["test_flaky"]))
    tmpl, calls = _subset(tmp_path, [_junit(passed=["test_flaky"])])
    cfg = {"work": {"validate_subset": tmpl}}

    assert worktree.clean_checkout(entry, "main", _full_runner(red_xml, 1), cfg=cfg) == 1

    assert _calls(calls) == [], "the GATE converged — a green could now be laundered from retries"
    assert validation_ledger._SEALED is False, "the gate reached the subset key at all"
    assert len(_results(repo)["runs"]) == 1, "the gate ran more than the one whole phase"


# ---------------------------------------------------------------------------
# the flake signal, read from bh-ku9n9.6's store
# ---------------------------------------------------------------------------


def test_a_flake_is_surfaced_on_the_confirming_run(tmp_path, monkeypatch, capsys):
    """The confirming run earns the verdict AND names what only passed on retry. Red then green
    at the identical tree is a flake, not a fix, and the operator hears about it from the run
    that attests — otherwise the retry is silently absorbed into a green nobody questions."""
    entry, repo = _hive(tmp_path, monkeypatch)
    red_xml, green_xml = tmp_path / "red.xml", tmp_path / "green.xml"
    red_xml.write_text(_junit(passed=["test_solid"], failed=["test_flaky"]))
    green_xml.write_text(_junit(passed=["test_solid", "test_flaky"]))

    assert worktree.clean_checkout(entry, "main", _full_runner(red_xml, 1)) == 1
    capsys.readouterr()
    assert worktree.clean_checkout(entry, "main", _full_runner(green_xml, 0) + " # again") == 0

    out = capsys.readouterr().out
    assert _name("test_flaky") in out and "FLAKY" in out, out
    assert _name("test_solid") not in out, "a test that never failed was called flaky"
    assert converge.flakes(entry, "main") == [_name("test_flaky")]


def test_a_tree_that_was_never_red_reports_no_flakes(tmp_path, monkeypatch):
    """The common case pays nothing and says nothing: a tree with no triage directory has no
    retry history to read, so the flake report is empty rather than absent-inferred."""
    entry, repo = _hive(tmp_path, monkeypatch)
    green = tmp_path / "green.xml"
    green.write_text(_junit(passed=["test_solid"]))

    assert worktree.clean_checkout(entry, "main", _full_runner(green, 0)) == 0

    assert converge.flakes(entry, "main") == []


# ---------------------------------------------------------------------------
# where the loop stops
# ---------------------------------------------------------------------------


def test_the_loop_stops_when_a_round_makes_no_progress(tmp_path, monkeypatch):
    """A genuinely broken test does not become a retry budget. The same failure coming back
    unchanged ends the loop after ONE re-run — re-running an identical set again only burns time,
    and 'keep going until it passes' is the laundering this bead refuses."""
    entry, repo = _hive(tmp_path, monkeypatch)
    tmpl, calls = _subset(tmp_path, [_junit(failed=["test_broken"])])
    red = {"cases": [{"test.case.name": _name("test_broken"), "test.case.result.status": "failed"}]}

    result = converge.converge(entry, {"work": {"validate_subset": tmpl}}, repo, "", red)

    assert len(_calls(calls)) == 1, "the loop re-ran an unchanged failure set"
    assert result == {
        "rounds": 1,
        "candidate_green": False,
        "flaky": [],
        "still_failing": [_name("test_broken")],
    }


def test_the_loop_is_capped_even_while_it_keeps_making_progress(tmp_path, monkeypatch):
    """`MAX_ROUNDS` is a hard ceiling, not a target: a suite that fixes one test per round would
    otherwise converge forever. Five failures, one clearing per round — the loop stops at the cap
    with work still outstanding, and reports it as still failing rather than as a candidate."""
    entry, repo = _hive(tmp_path, monkeypatch)
    names = [f"test_{i}" for i in range(5)]
    rounds = [_junit(passed=names[:i], failed=names[i:]) for i in range(1, 6)]
    tmpl, calls = _subset(tmp_path, rounds)
    red = {
        "cases": [{"test.case.name": _name(n), "test.case.result.status": "failed"} for n in names]
    }

    result = converge.converge(entry, {"work": {"validate_subset": tmpl}}, repo, "", red)

    assert len(_calls(calls)) == converge.MAX_ROUNDS
    assert result["rounds"] == converge.MAX_ROUNDS
    assert result["candidate_green"] is False
    assert result["still_failing"] == [_name(n) for n in names[converge.MAX_ROUNDS :]]
    assert result["flaky"] == [_name(n) for n in names[: converge.MAX_ROUNDS]]


def test_the_loop_re_runs_only_what_is_still_failing(tmp_path, monkeypatch):
    """The selection narrows with the evidence and nothing else — tier 3 is struck, so there is
    no coverage map and the subset is drawn from what already failed, never widened."""
    entry, repo = _hive(tmp_path, monkeypatch)
    tmpl, calls = _subset(
        tmp_path,
        [_junit(passed=["test_a1"], failed=["test_a2"]), _junit(passed=["test_a2"])],
    )
    red = {
        "cases": [
            {"test.case.name": _name(n), "test.case.result.status": "failed"}
            for n in ("test_a1", "test_a2")
        ]
    }

    result = converge.converge(entry, {"work": {"validate_subset": tmpl}}, repo, "", red)

    assert _calls(calls) == [[_name("test_a1"), _name("test_a2")], [_name("test_a2")]]
    assert result["candidate_green"] is True
    assert result["flaky"] == [_name("test_a1"), _name("test_a2")]


# ---------------------------------------------------------------------------
# absent / malformed / unusable ⇒ run the phase whole
# ---------------------------------------------------------------------------


def test_a_hive_with_no_validate_subset_never_converges(tmp_path, monkeypatch):
    """The default, and it must be exactly today's behaviour: one whole run, no retries, and a
    real green verdict recorded when it passes. Absent is fully supported, not degraded."""
    entry, repo = _hive(tmp_path, monkeypatch)
    red = {"cases": [{"test.case.name": _name("test_x"), "test.case.result.status": "failed"}]}

    assert converge.converge(entry, {"work": {}}, repo, "", red) is None

    sha = worktree.head_full_sha(repo)
    validation_ledger.record(entry, sha, _FULL_CMD, 0)
    assert validation_ledger.green_verdict(entry, sha, _FULL_CMD) is not None, (
        "a hive that never converged lost its ability to attest"
    )


def test_a_template_missing_the_placeholder_is_refused_at_config_set(tmp_path, monkeypatch):
    """The one failure mode an operator would never notice, because tier 2 correctly fails OPEN:
    a template bh cannot fill would just silently stop converging. So it is an ERROR at write
    time — the ADR's requirement, and the reason this key is a declared field rather than a
    member of the free-form `work.validate` phase map, which cannot validate its own keys."""
    cfg = {}
    bad = config.set_value("work.validate_subset", "pytest -n auto --lf", cfg=cfg)
    assert bad["ok"] is False
    assert "{tests}" in bad["problems"][0]["message"]
    assert "validate_subset" not in cfg.get("work", {}), "the bad value was written anyway"

    good = config.set_value("work.validate_subset", "pytest -n auto {tests}", cfg=cfg)
    assert good["ok"] is True
    assert cfg["work"]["validate_subset"] == "pytest -n auto {tests}"


def test_a_hand_edited_template_missing_the_placeholder_falls_open(tmp_path, monkeypatch):
    """A value can still arrive by hand-editing config.yaml, which never passes through
    `bh config set`. At READ time it must fail OPEN — treated as absent, phase runs whole. Tier 2
    is a convenience; a typo in it may cost the converge loop and must never cost a validation."""
    entry, _repo = _hive(tmp_path, monkeypatch)

    assert converge.template({"work": {"validate_subset": "pytest --lf"}}, entry) == ""
    assert converge.template({"work": {"validate_subset": "pytest {tests}"}}, entry) == (
        "pytest {tests}"
    )


def test_converge_never_guesses_names_when_the_runner_reports_none(tmp_path, monkeypatch):
    """A tier-0 hive — a red gate and no machine-readable results — has nothing to name, so
    nothing is re-run. bh does not translate, infer, or invent a selector: 'never emulate
    subsetting by guessing test names' is the rule, and a template alone is not evidence."""
    entry, repo = _hive(tmp_path, monkeypatch)
    tmpl, calls = _subset(tmp_path, [_junit(passed=["test_x"])])

    assert converge.converge(entry, {"work": {"validate_subset": tmpl}}, repo, "", None) is None

    assert _calls(calls) == []


def test_a_red_leg_that_is_not_a_test_failure_runs_the_phase_whole(tmp_path, monkeypatch):
    """`validate_cmd` is a PIPELINE — lint, licences, then tests. A gate that fails its lint leg
    reports zero failing tests, and re-running zero tests would 'converge' a red gate to a
    candidate green in one round. There is nothing to converge on: run the phase whole."""
    entry, repo = _hive(tmp_path, monkeypatch)
    tmpl, calls = _subset(tmp_path, [_junit(passed=["test_x"])])
    all_passed = {
        "cases": [{"test.case.name": _name("test_x"), "test.case.result.status": "passed"}]
    }

    result = converge.converge(entry, {"work": {"validate_subset": tmpl}}, repo, "", all_passed)

    assert result is None and _calls(calls) == []
