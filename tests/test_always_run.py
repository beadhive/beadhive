"""bh-ehmd8 — the always-run set runs on a cache HIT, or the hit is not honoured.

bh-ku9n9.3 labelled the tests a tree hash cannot vouch for (`always_run`, the ADR's git-metadata
asterisk) and nothing ever ran them: a hit short-circuited the whole validate command and took
them with it. `work.always_run` is the consumer. A hit now means "skip the expensive command,
still run the small set", and the decision lives in ONE seam — `validation_ledger.green_verdict`,
the single question every reuse boundary asks — rather than at `clean_checkout`, `check_push_main`
and the release pre-flight separately.

Written against the ways it would be broken:

1. **A failing set can never yield an attestation** — the binding one, and it is proved by
   *attempting* the write straight afterwards (`seal_subset_run`'s latch, bh-ku9n9.8's precedent),
   not by reading the call graph.
2. **A hive that declares nothing gets exactly today's behaviour** — verified by driving the same
   hit with no key at all and watching the whole checkout still be skipped.
3. **One seam, several boundaries** — the same declared command runs whether the hit is decided
   at `clean_checkout(reuse=True)` (submit's path, and every landing boundary's since
   bh-ku9n9.17) or at `check_push_main` (the pre-push hook, and the release pre-flight through it).
4. **bh learns nothing about a test framework** — every command here is a plain `sh -c`, spawned
   opaquely, exactly as an operator's `pytest -m always_run` would be.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from beadhive import config, host, prepush, registry, validation_ledger, worktree

GATE_CMD = "just check-all"  # what `push-main` would run on a miss


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _minted_host_identity():
    """`validation_ledger.record` stamps `host.host_id()` — mint it as `bh config init` would."""
    host.mint_if_needed()


@pytest.fixture
def hive(tmp_path, monkeypatch):
    """A real one-commit clone registered as a hive, with `push-main` wired to the gate command.

    Real git, because the ledger's identity half is `rev^{tree}` resolved in the hive's own clone
    and the always-run set is spawned there too — a stubbed resolver would prove nothing."""
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "f.txt").write_text("hi\n")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "chore: seed", cwd=repo)

    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    entry = {
        "provider": "github",
        "org": "myorg",
        "repo": "myrepo",
        "prefix": "mr",
        "work": {"validate": {"push-main": GATE_CMD}},
    }
    cfg = {"managed_repos": [entry]}
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    return {"cfg": cfg, "entry": entry, "repo": repo, "sha": _git("rev-parse", "HEAD", cwd=repo)}


def _logging_cmd(tmp_path: Path, name: str, rc: int = 0) -> tuple[Path, str]:
    """A command that appends one line to a log and exits `rc` — an observable run count."""
    log = tmp_path / f"{name}.log"
    return log, f"sh -c 'echo ran >> {log}; exit {rc}'"


def _runs(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def _declare(hive, cmd: str) -> None:
    """Set `work.always_run` for this hive — layered per-hive, like every other `work.*` key."""
    hive["entry"]["work"]["always_run"] = cmd


def _ledger(hive) -> Path:
    return hive["repo"] / ".git" / validation_ledger.LEDGER_FILENAME


# ---------------------------------------------------------------------------
# THE binding acceptance: a failing always-run set can never yield an attestation
# ---------------------------------------------------------------------------


def test_a_failing_always_run_set_refuses_the_hit_and_shuts_the_ledger(hive, tmp_path, capsys):
    """The hostile one. The set fails on a hit, so the hit is refused — and then we try to write
    the very verdict a fall-through full run would produce, for that exact tree under that exact
    command. The ledger must refuse it: the seal is latched at the moment the failure is observed,
    so 'not honoured' cannot quietly become 'attested a moment later' by any route at all.

    That is not hypothetical here. When the set fails, the caller falls through to a full run in a
    *verify checkout* — a different working directory whose git metadata may well answer
    differently, which is precisely the exposure the always-run set exists for."""
    always, always_cmd = _logging_cmd(tmp_path, "always", rc=1)
    _declare(hive, always_cmd)
    validation_ledger.record(hive["entry"], hive["sha"], GATE_CMD, 0)
    assert _ledger(hive).is_file(), "the green verdict under test was not recorded"

    assert validation_ledger.green_verdict(hive["entry"], hive["sha"], GATE_CMD) is None
    assert _runs(always) == 1, "the always-run set was not run on the hit"
    assert "always-run set FAILED" in capsys.readouterr().err

    before = _ledger(hive).read_text()
    validation_ledger.record(hive["entry"], hive["sha"], GATE_CMD, 0)  # the laundering attempt
    assert _ledger(hive).read_text() == before, "a verdict was written after a failing always-run"


def test_a_failing_always_run_set_stops_a_full_run_from_recording_a_verdict(
    hive, tmp_path, monkeypatch
):
    """The same guarantee driven end to end through the real gate seam rather than by hand: the
    hit is refused, `clean_checkout` runs the phase whole and it goes GREEN — and that green is
    still not written, because the process was shut the moment the set failed."""
    always, always_cmd = _logging_cmd(tmp_path, "always", rc=1)
    full, full_cmd = _logging_cmd(tmp_path, "full", rc=0)
    _declare(hive, always_cmd)
    validation_ledger.record(hive["entry"], hive["sha"], full_cmd, 0)
    entries_before = json.loads(_ledger(hive).read_text())

    assert worktree.clean_checkout(hive["entry"], "main", full_cmd, reuse=True) == 0

    assert _runs(always) == 1 and _runs(full) == 1, "the hit was honoured, or the set never ran"
    assert json.loads(_ledger(hive).read_text()) == entries_before, "the green run attested"


# ---------------------------------------------------------------------------
# the hit path: run the small set, still skip the expensive command
# ---------------------------------------------------------------------------


def test_a_hit_runs_the_always_run_set_and_still_skips_the_expensive_command(
    hive, tmp_path, capsys
):
    """The whole change in one assertion pair: the declared set runs, and the validate command
    does not. A hit is 'skip the expensive command', not 'skip everything'."""
    always, always_cmd = _logging_cmd(tmp_path, "always")
    full, full_cmd = _logging_cmd(tmp_path, "full")
    _declare(hive, always_cmd)

    assert worktree.clean_checkout(hive["entry"], "main", full_cmd) == 0  # earns the verdict
    assert (_runs(full), _runs(always)) == (1, 0), "the gate itself ran the always-run set"

    assert worktree.clean_checkout(hive["entry"], "main", full_cmd, reuse=True) == 0

    assert _runs(full) == 1, "the expensive command re-ran — the hit bought nothing"
    assert _runs(always) == 1, "the always-run set did not run on the hit"
    assert "validation verdict reused" in capsys.readouterr().out


def test_the_push_gate_shares_the_one_decision(hive, tmp_path):
    """A second boundary, no second implementation. `check_push_main` is the pre-push hook's
    lookup (and the release pre-flight's, through the same predicate) and it reaches
    `green_verdict` like `clean_checkout` does — so it inherits the set without knowing it
    exists. Both directions asserted: a passing set leaves the skip intact, a failing one refuses
    it the same way every other miss is refused."""
    ok_log, ok_cmd = _logging_cmd(hive["repo"].parent, "pass")
    _declare(hive, ok_cmd)
    validation_ledger.record(hive["entry"], hive["sha"], GATE_CMD, 0)

    assert prepush.check_push_main(hive["sha"], hive_id="mr", gate_cmd=GATE_CMD)[0] is True
    assert _runs(ok_log) == 1

    bad_log, bad_cmd = _logging_cmd(hive["repo"].parent, "fail", rc=1)
    _declare(hive, bad_cmd)
    ok, detail = prepush.check_push_main(hive["sha"], hive_id="mr", gate_cmd=GATE_CMD)
    assert ok is False, detail
    assert _runs(bad_log) == 1


def test_the_set_runs_in_the_hives_own_clone(hive, tmp_path):
    """Where it runs is the point: git METADATA — tags, `git describe`, commit counts — is a
    property of a repository, not of a tree, so the set is spawned in the hive's clone and can
    read the very history a verdict says nothing about."""
    out = tmp_path / "cwd.txt"
    _declare(hive, f"sh -c 'pwd > {out}'")
    validation_ledger.record(hive["entry"], hive["sha"], GATE_CMD, 0)

    assert validation_ledger.green_verdict(hive["entry"], hive["sha"], GATE_CMD) is not None
    assert Path(out.read_text().strip()).resolve() == registry.hive_dir(hive["entry"]).resolve()


# ---------------------------------------------------------------------------
# absent, unrunnable, layered
# ---------------------------------------------------------------------------


def test_a_hive_declaring_nothing_gets_todays_behaviour(hive, tmp_path):
    """Verified, not assumed: no `work.always_run` ⇒ the hit is honoured whole, the expensive
    command is skipped, and nothing at all is spawned in its place."""
    full, full_cmd = _logging_cmd(tmp_path, "full")
    assert "always_run" not in hive["entry"]["work"]

    assert worktree.clean_checkout(hive["entry"], "main", full_cmd) == 0
    assert worktree.clean_checkout(hive["entry"], "main", full_cmd, reuse=True) == 0

    assert _runs(full) == 1, "the hit was not honoured whole"
    assert validation_ledger._SEALED is False, "an absent key still reached the seal"


def test_an_unrunnable_always_run_command_refuses_the_hit_rather_than_raising(hive, capsys):
    """A command that cannot even start is not evidence of anything, so it refuses the hit and
    costs one re-validation — the ledger's answer to a miss. It must not raise: `clean_checkout`
    would propagate, and `check_push_main` runs inside a git hook."""
    _declare(hive, "definitely-not-a-real-binary-bh-ehmd8")
    validation_ledger.record(hive["entry"], hive["sha"], GATE_CMD, 0)

    assert validation_ledger.green_verdict(hive["entry"], hive["sha"], GATE_CMD) is None
    assert "could not RUN" in capsys.readouterr().err
    assert validation_ledger._SEALED is False, "an unrun set sealed the ledger anyway"


def test_always_run_is_layered_per_hive_over_global(hive, tmp_path):
    """Layered like every other `work.*` setting: the per-hive value wins over the global one."""
    _, global_cmd = _logging_cmd(tmp_path, "global")
    per_hive, per_hive_cmd = _logging_cmd(tmp_path, "perhive")
    hive["cfg"]["work"] = {"always_run": global_cmd}

    assert validation_ledger.always_run_cmd(hive["cfg"], hive["entry"]) == global_cmd
    _declare(hive, per_hive_cmd)
    assert validation_ledger.always_run_cmd(hive["cfg"], hive["entry"]) == per_hive_cmd

    validation_ledger.record(hive["entry"], hive["sha"], GATE_CMD, 0)
    assert validation_ledger.green_verdict(hive["entry"], hive["sha"], GATE_CMD) is not None
    assert _runs(per_hive) == 1


def test_a_red_or_missing_verdict_never_pays_for_the_always_run_set(hive, tmp_path):
    """The set gates the honouring of a hit, and nothing else. A miss and a red verdict were
    already going to run the full command, so spawning it there would be pure cost."""
    always, always_cmd = _logging_cmd(hive["repo"].parent, "always")
    _declare(hive, always_cmd)

    assert validation_ledger.green_verdict(hive["entry"], hive["sha"], GATE_CMD) is None  # miss
    validation_ledger.record(hive["entry"], hive["sha"], GATE_CMD, 1)  # red
    assert validation_ledger.green_verdict(hive["entry"], hive["sha"], GATE_CMD) is None

    assert _runs(always) == 0
