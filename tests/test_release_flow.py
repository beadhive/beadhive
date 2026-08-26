"""`bh release preflight / attest / await / recover` (bh-ku9n9.7) — the attestation as the
PRE-FLIGHT PROOF for the bump, the bump tree gated in the background, and the recovery branch
turned by one measured fact.

`docs/design/attested-green-adr.md` applied one level up, to the release itself. Three properties
are under test here and every test below is one of them:

1.  **THE INVERSION.** Green is proven BEFORE the bump, never inside the push. bh-67utw's rule is
    that a failed push is undoable iff the tag never left, so the bump is the last safely
    reversible moment. `preflight` READS a verdict and refuses without one; it never establishes
    green, because a place that establishes green is a place a red suite is discovered too late.

2.  **THE HOLE THAT BIT 0.11.5.** `cz bump` writes pyproject.toml + CHANGELOG.md + uv.lock, so
    the release commit is a NEW TREE WITH NO ATTESTATION. `attest --background` fires the gate on
    that tree the moment it exists and `await` blocks on the verdict — instead of the ~371s gate
    running inside a push holding an idle socket GitHub will close (bh-53o8f).

3.  **THE MEASURED BRANCH.** `recover` decides between bh-67utw's two cases from the TAG and every
    advertised remote ref, read with `ls-remote` against the actual remote. Never assumed, never
    from a local tracking ref, and "I could not look" is its own exit code rather than being
    folded into "it is not there". Its separately explicit apply path then proves exact clean
    local state, creates a backup, preserves merge shape, re-measures, and deletes only the local
    tag; every ambiguity is a mutation-free refusal.

THE SAFETY BAR IS bh-ku9n9.5's, unchanged: there is NO path where a missing, stale, red, corrupt,
or ambiguous attestation lets a bump or a push proceed as though green were proven. Every miss
shape that file drives against the pre-push lookup is driven here against the BUMP, because the
consequence is strictly worse — a push that skips its gate is a bad commit on main, while a bump
that skips its proof is a tag, and a tag is the point of no return.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config, host, prepush, release, validation_ledger, validation_records
from beadhive.cli import app
from harness.validation_state import age as age_verdict
from harness.validation_state import latest as latest_verdict
from harness.validation_state import pointer as verdict_pointer
from harness.validation_state import rewrite as rewrite_verdict

runner = CliRunner()

GATE_CMD = "just check-all"  # what `work.validate.push-main` must name for a verdict to count
ROOT = Path(__file__).resolve().parents[1]
PUSH_SCRIPT = ROOT / "scripts" / "push-main.sh"


@pytest.fixture(autouse=True)
def _minted_host_identity():
    """`validation_ledger.record` and the bump-gate marker both stamp `host.host_id()`."""
    host.mint_if_needed()


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def hive(tmp_path, monkeypatch):
    """A real registered hive with a real `origin` bare remote and `push-main` wired to the gate.

    Real git throughout, on purpose: the identity half of every lookup here is
    `rev^{tree}` resolved in the hive's own clone, and the recovery branch is a real `ls-remote`
    against a real remote. A stubbed resolver or a faked remote would prove nothing about the
    two things this bead is actually for."""
    repo = tmp_path / "ws" / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _git("config", "tag.gpgsign", "false", cwd=repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    (repo / "f.txt").write_text("hi\n")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "chore: seed", cwd=repo)

    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    entry = {
        "provider": "github",
        "org": "myorg",
        "repo": "myrepo",
        "prefix": "mr",
        "work": {"validate": {"push-main": GATE_CMD}},
    }
    cfg = {"managed_repos": [entry]}
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    return {
        "cfg": cfg,
        "entry": entry,
        "repo": repo,
        "remote": remote,
        "sha": _git("rev-parse", "HEAD", cwd=repo),
    }


def _run(hive, *args: str):
    return runner.invoke(app, ["release", *args, "--hive", "mr"])


def _preflight(hive, rev: str | None = None, gate: str = GATE_CMD):
    args = ["preflight", rev or hive["sha"]]
    if gate:
        args += ["--gate", gate]
    return _run(hive, *args)


def _await(hive, *extra: str, rev: str | None = None):
    return _run(hive, "await", rev or hive["sha"], "--gate", GATE_CMD, "--poll", "0.01", *extra)


def _ledger(hive, rev: str | None = None, cmd: str = GATE_CMD) -> Path:
    return verdict_pointer(hive["entry"], rev or hive["sha"], cmd)


def _marker(hive) -> Path:
    marker = release._marker_path(hive["entry"])
    assert marker is not None
    return marker


def _legacy_marker(hive) -> Path:
    marker = release._legacy_marker_path(hive["entry"])
    assert marker is not None
    return marker


def _log(hive, rev: str | None = None) -> Path:
    tree = validation_ledger.tree_of(hive["entry"], rev or hive["sha"])
    log = release._gate_log_path(hive["entry"], tree)
    assert log is not None
    return log


def _legacy_log(hive) -> Path:
    log = release._legacy_gate_log_path(hive["entry"])
    assert log is not None
    return log


def _attest(hive, rc: int = 0, rev: str | None = None, cmd: str = GATE_CMD) -> None:
    validation_ledger.record(hive["entry"], rev or hive["sha"], cmd, rc)


def _typed_attest(hive, verdict: str, rev: str | None = None) -> None:
    rev = rev or hive["sha"]
    tree = validation_ledger.tree_of(hive["entry"], rev)
    run = validation_records.begin_run(
        hive["repo"],
        bead=None,
        phase="release",
        branch="main",
        worktree=hive["repo"],
        sha=rev,
        tree=tree,
        command_hash=validation_ledger.cmd_hash(GATE_CMD),
        command=GATE_CMD,
    )
    assert run is not None
    validation_records.finish_run(
        hive["repo"],
        run["run_id"],
        exit_code=0,
        protocol={
            "protocol": validation_records.PROTOCOL_NAME,
            "version": 1,
            "verdict": verdict,
            "reason": "runner refused",
        },
    )
    validation_ledger.record(hive["entry"], rev, GATE_CMD, 0, run_id=run["run_id"])


def _fire(hive, rev: str | None = None, pid: int | None = None, cmd: str = GATE_CMD) -> None:
    """A bump-gate marker as `attest --background` writes one, without spawning a real gate."""
    rev = rev or hive["sha"]
    marker = _marker(hive)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "tree": validation_ledger.tree_of(hive["entry"], rev),
                "cmd": cmd,
                "sha": rev,
                "pid": pid if pid is not None else os.getpid(),  # this process — reliably alive
                "host": host.host_id(),
                "log": str(_log(hive, rev)),
                "started": time.time(),
            }
        )
    )


def _bump(hive, version: str = "0.1.1") -> str:
    """Stand in for `cz bump`: a NEW TREE (pyproject + changelog) plus its tag. The point of the
    fixture is that nothing has ever attested this tree — which is the whole defect."""
    repo = hive["repo"]
    (repo / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    (repo / "CHANGELOG.md").write_text(f"## {version}\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", f"bump: version {version}", cwd=repo)
    _git("tag", f"v{version}", cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


# ─── preflight: the ONE way a bump is allowed ────────────────────────────────────────────────


def test_a_fresh_green_verdict_for_this_tree_lets_the_bump_happen(hive):
    """The only exit 0 that exists. The land-time run already exercised this exact tree under
    this exact command, so the bump has nothing left to prove and pays nothing to prove it."""
    _attest(hive)

    res = _preflight(hive)

    assert res.exit_code == 0, res.output
    assert "attested green" in res.output


def test_preflight_reads_a_verdict_and_never_establishes_one(hive, monkeypatch):
    """THE INVERSION, asserted directly: pre-flight is a lookup, not a gate run. If it could run
    the suite it would be a second place green gets established, and the whole reason green moves
    before the bump is that establishing it late is what let 0.11.5 tag a red tree."""
    from beadhive import worktree

    monkeypatch.setattr(
        worktree, "clean_checkout", lambda *a, **k: pytest.fail("preflight ran the gate")
    )
    _attest(hive)
    assert _preflight(hive).exit_code == 0
    assert _preflight(hive, rev=_bump(hive)).exit_code == 1  # a miss must not run it either


def test_the_bump_tree_produced_by_cz_bump_has_no_attestation(hive):
    """THE HOLE, stated as a test. A green main is not a green bump: `cz bump` rewrites three
    tracked files, so the release commit is a tree nothing has ever validated. Before this bead
    that miss was discovered inside the push, ~371s in, with a tag already on the machine."""
    _attest(hive)
    assert _preflight(hive).exit_code == 0

    bumped = _bump(hive)

    assert _preflight(hive, rev=bumped).exit_code == 1
    assert "no fresh green push-main verdict" in _preflight(hive, rev=bumped).output


# ─── preflight: every way a bad or missing attestation must NOT let the bump proceed ─────────


def test_no_verdict_at_all_refuses_the_bump(hive):
    res = _preflight(hive)

    assert res.exit_code == 1
    assert "refusing to bump" in res.output
    assert not _ledger(hive).exists()


def test_a_red_verdict_refuses_the_bump(hive):
    """A recorded FAILURE is a record, not an attestation — exactly the 0.11.5 shape, where the
    suite was genuinely red and the bump happened anyway."""
    _attest(hive, rc=1)

    assert _preflight(hive).exit_code == 1


def test_a_stale_verdict_refuses_the_bump(hive):
    """Past `work.ledger_ttl` a verdict is not evidence (ADR Decision 3). A release must not ride
    a green from a day ago; the tree may be identical but the world it was proved in is not."""
    _attest(hive)
    age_verdict(
        hive["repo"],
        hive["entry"],
        hive["sha"],
        GATE_CMD,
        validation_ledger.LEDGER_TTL_SECONDS + 60,
    )

    assert _preflight(hive).exit_code == 1


def test_a_verdict_for_a_different_tree_refuses_the_bump(hive):
    """main moved since the verdict was earned. No hand-written invalidation rule anywhere —
    the tree hash differs and the lookup simply misses."""
    _attest(hive)
    repo = hive["repo"]
    (repo / "f.txt").write_text("edited\n")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "feat: move main", cwd=repo)

    assert _preflight(hive, rev=_git("rev-parse", "HEAD", cwd=repo)).exit_code == 1


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("not json", "not json {"),
        ("truncated mid-write", '[{"tree": "abc", "cmd_hash"'),
        ("an object, not a list", '{"tree": "abc"}'),
        ("a list of non-objects", '["green", 0]'),
        ("empty file", ""),
    ],
)
def test_a_corrupt_ledger_refuses_the_bump(hive, name, content):
    """Every shape of unreadable ledger reads as an EMPTY ledger. A torn write on the one file
    that can authorise a release must cost a gate run, never a tag."""
    _attest(hive)
    _ledger(hive).write_text(content)

    assert _preflight(hive).exit_code == 1, name


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("no timestamp", lambda e: e.pop("at")),
        ("timestamp is a string", lambda e: e.update(at="yesterday")),
        ("timestamp is null", lambda e: e.update(at=None)),
        ("timestamp is in the far future", lambda e: e.update(at=1e30)),
        ("no rc", lambda e: e.pop("rc")),
        ("rc is the STRING zero", lambda e: e.update(rc="0")),
        ("rc is null", lambda e: e.update(rc=None)),
        ("no tree", lambda e: e.pop("tree")),
        ("tree is null", lambda e: e.update(tree=None)),
        ("no command_hash", lambda e: e.pop("command_hash")),
    ],
)
def test_a_malformed_entry_refuses_the_bump(hive, name, mutate):
    """Field by field: a record missing or lying about any part of its identity, freshness or
    outcome is not an attestation. `at: 1e30` is the nastiest — it survives the freshness compare
    (a future stamp never expires) and would then explode in the timestamp formatting, so this
    proves the verb neither passes NOR raises."""
    _attest(hive)
    entry = json.loads(_ledger(hive).read_text())
    mutate(entry)
    _ledger(hive).write_text(json.dumps(entry))

    assert _preflight(hive).exit_code == 1, name


def test_an_unconfigured_push_main_phase_refuses_the_bump(hive):
    """Unset, `config.validate_cmd` falls back to `work.validate_cmd` (`just check` — the FAST
    gate). Honouring a verdict earned by the fast gate as though it were the full one is exactly
    the ambiguity this epic refuses, so an absent key resolves to nothing at all."""
    hive["entry"]["work"] = {"validate": {}, "validate_cmd": "just check"}
    validation_ledger.record(hive["entry"], hive["sha"], "just check", 0)

    res = _preflight(hive, gate="")

    assert res.exit_code == 1
    assert "no `work.validate.push-main` configured" in res.output


def test_a_phase_naming_a_different_command_refuses_the_bump(hive):
    """The ambiguous-attestation case. A `push-main` pointing at a weaker command than the gate
    actually runs is a verdict about some other gate, and must read as a loud miss."""
    hive["entry"]["work"]["validate"]["push-main"] = "just check"
    validation_ledger.record(hive["entry"], hive["sha"], "just check", 0)

    res = _preflight(hive)

    assert res.exit_code == 1
    assert "but this gate runs" in res.output


def test_an_unresolvable_rev_refuses_the_bump(hive):
    """bh-ku9n9.3's fail-safe: a rev git cannot resolve is keyed literally, so it can only ever
    match itself and a writer/reader mismatch misses rather than serving someone else's verdict."""
    _attest(hive)

    assert _preflight(hive, rev="deadbeef" * 5).exit_code == 1


def test_an_empty_rev_refuses_the_bump(hive):
    _attest(hive)

    assert _run(hive, "preflight", "", "--gate", GATE_CMD).exit_code == 1


def test_no_managed_hive_refuses_the_bump(hive, monkeypatch):
    monkeypatch.setattr(config, "load", lambda *a, **k: {"managed_repos": []})

    assert runner.invoke(app, ["release", "preflight", hive["sha"]]).exit_code == 1


def test_an_exploding_config_refuses_the_bump(hive, monkeypatch):
    """Any exception at all lands on the safe side. The predicate's whole failure mode is "you
    pay for a gate run you would have paid for anyway"; a release's is "you tag a red tree"."""

    def boom(*a, **k):
        raise RuntimeError("config is on fire")

    monkeypatch.setattr(config, "load", boom)

    assert runner.invoke(app, ["release", "preflight", hive["sha"]]).exit_code == 1


@pytest.mark.parametrize("verb", ["preflight", "attest", "await", "recover"])
def test_every_verb_refuses_rather_than_raising_when_config_explodes(hive, monkeypatch, verb):
    """The blanket-except bar from bh-ku9n9.5, applied to all four. An exception is already the
    safe direction (a crash publishes nothing), but a release is the worst place to hand an
    operator a traceback instead of a sentence — and the exit code must still be non-zero."""

    def boom(*a, **k):
        raise RuntimeError("config is on fire")

    monkeypatch.setattr(config, "load", boom)

    res = runner.invoke(app, ["release", verb, hive["sha"]])

    assert res.exit_code != 0
    assert res.exception is None or isinstance(res.exception, SystemExit)


def test_there_is_no_flag_that_turns_a_refusal_into_a_pass(hive):
    """The bar from bh-ku9n9.5, restated where it matters more. A `--force` here would be the one
    path that lets a release proceed on no proof, so there must not be one to find."""
    opts = runner.invoke(app, ["release", "preflight", "--help"]).output

    for escape in ("--force", "--no-verify", "--skip", "--allow-unproven", "--yes"):
        assert escape not in opts


def test_the_refusal_says_why_the_order_matters_and_how_to_get_a_verdict(hive):
    """A refusal an operator cannot act on becomes a refusal an operator routes around."""
    res = _preflight(hive)

    assert "bh-67utw" in res.output  # WHY the proof is here rather than in the push
    assert "release attest" in res.output  # and the one way to establish one
    # …named as the RECIPE the ladder teaches, not only as the verb (bh-k5te9). Both, because the
    # recipe is what the operator was taught and the verb is what anyone not using `just` needs.
    assert "just attest" in res.output


# ─── attest: the confirming run that produces a bump-tree verdict ────────────────────────────


def test_attest_runs_the_gate_from_a_clean_checkout_and_records_it(hive, monkeypatch):
    """The sound writer, reused rather than reinvented: `worktree.clean_checkout` runs the hive's
    `verify: true` init rules against the checked-out tree first, so the environment is
    established FROM THE TREE — which is what makes a bump-tree verdict the same object a
    land-time run produces, and so readable by preflight and the pre-push hook alike."""
    from beadhive import worktree

    seen = {}

    def fake(entry, rev, cmd, *a, **k):
        seen.update(rev=rev, cmd=cmd)
        validation_ledger.record(entry, rev, cmd, 0)
        return 0

    monkeypatch.setattr(worktree, "clean_checkout", fake)
    bumped = _bump(hive)

    assert _run(hive, "attest", bumped, "--gate", GATE_CMD).exit_code == 0
    assert seen == {"rev": bumped, "cmd": GATE_CMD}
    assert _preflight(hive, rev=bumped).exit_code == 0  # the bump tree is now attested


def test_a_red_attest_records_the_failure_and_still_refuses_the_bump(hive, monkeypatch):
    """Recording is not attesting. A red run writes an entry so the failure is remembered, and
    that entry can never satisfy the thing that gates a release."""
    from beadhive import worktree

    monkeypatch.setattr(
        worktree,
        "clean_checkout",
        lambda entry, rev, cmd, *a, **k: (validation_ledger.record(entry, rev, cmd, 1), 1)[1],
    )

    assert _run(hive, "attest", hive["sha"], "--gate", GATE_CMD).exit_code == 1
    assert _preflight(hive).exit_code == 1


def test_attest_refuses_an_unconfigured_phase_rather_than_gating_under_the_fast_command(hive):
    """Same resolver, same refusal as preflight — `prepush.push_main_cmd`. An attest that fell
    back to `just check` would MANUFACTURE the ambiguous verdict the lookup exists to refuse."""
    hive["entry"]["work"] = {"validate": {}, "validate_cmd": "just check"}

    res = _run(hive, "attest", hive["sha"])

    assert res.exit_code == 1
    assert "no `work.validate.push-main` configured" in res.output
    assert not _ledger(hive).exists()


def test_attest_refuses_an_unresolvable_rev(hive):
    res = _run(hive, "attest", "deadbeef" * 5, "--gate", GATE_CMD)

    assert res.exit_code == 1
    assert "cannot resolve" in res.output and "to a commit" in res.output


@pytest.fixture
def spawned(monkeypatch):
    """Records the detached gate's argv while letting a REAL process be spawned in its place
    (`true`), so the marker gets a real pid and every other `subprocess` call in the verb — the
    rev-parse, the ls-remotes — passes through untouched. Substituting a fake Popen wholesale
    breaks `subprocess.run`, which builds on the same symbol."""
    calls: dict = {}
    real = subprocess.Popen

    def wrapper(cmdline, **kwargs):
        if list(cmdline)[1:2] == ["release"]:
            calls.update(cmdline=list(cmdline), kwargs=kwargs)
            cmdline = ["true"]
        return real(cmdline, **kwargs)

    monkeypatch.setattr(release.subprocess, "Popen", wrapper)
    return calls


def test_background_attest_fires_the_gate_on_the_bump_tree_and_records_a_marker(hive, spawned):
    """THE FIX FOR THE HOLE. The gate starts against the NEW tree the instant `cz bump` makes it,
    detached, so the ~371s is spent while the release does everything else — not inside a push
    holding a connection GitHub closes after ~5 idle minutes (bh-53o8f)."""
    bumped = _bump(hive)

    res = _run(hive, "attest", bumped, "--gate", GATE_CMD, "--background")

    assert res.exit_code == 0, res.output
    assert spawned["kwargs"]["start_new_session"] is True  # detached: outlives this process
    assert spawned["cmdline"][1:] == [
        "release",
        "attest",
        bumped,
        "--gate",
        GATE_CMD,
        "--hive",
        "mr",
    ]
    marker = json.loads(_marker(hive).read_text())
    assert marker["tree"] == _git("rev-parse", f"{bumped}^{{tree}}", cwd=hive["repo"])
    assert (marker["cmd"], marker["sha"]) == (GATE_CMD, bumped)
    assert isinstance(marker["pid"], int)
    assert marker["log"] == str(_log(hive, bumped))
    assert _log(hive, bumped).is_file()
    assert not _legacy_marker(hive).exists()


def test_readers_prefer_canonical_marker_but_fall_back_to_legacy(hive):
    """Migration never changes a pending gate's answer, and canonical wins if both exist."""
    bumped = _bump(hive)
    _fire(hive, rev=bumped)
    canonical = json.loads(_marker(hive).read_text())
    legacy = {**canonical, "pid": 12345}
    _legacy_marker(hive).write_text(json.dumps(legacy))

    assert release._marker_for_tree(hive["entry"], bumped)[0] == canonical
    _marker(hive).unlink()
    assert release._marker_for_tree(hive["entry"], bumped)[0] == legacy


def test_reading_legacy_marker_does_not_create_canonical_release_roots(hive):
    """The preview/await read seam must not silently migrate state."""
    bumped = _bump(hive)
    canonical = _marker(hive)
    _legacy_marker(hive).parent.mkdir(parents=True, exist_ok=True)
    _legacy_marker(hive).write_text(
        json.dumps(
            {
                "tree": validation_ledger.tree_of(hive["entry"], bumped),
                "cmd": GATE_CMD,
                "pid": os.getpid(),
                "host": host.host_id(),
            }
        )
    )

    assert not canonical.exists()
    assert _await(hive, "--if-pending", "--timeout", "0", rev=bumped).exit_code == 1
    assert not canonical.exists()


def test_diagnostic_log_resolution_is_canonical_first_then_legacy(hive):
    bumped = _bump(hive)
    _fire(hive, rev=bumped)
    marker = json.loads(_marker(hive).read_text())
    tree = validation_ledger.tree_of(hive["entry"], bumped)
    _legacy_log(hive).write_text("legacy gate output\n")

    assert release._marker_log(hive["entry"], marker, tree) == str(_legacy_log(hive))
    _log(hive, bumped).parent.mkdir(parents=True, exist_ok=True)
    _log(hive, bumped).write_text("canonical gate output\n")
    assert release._marker_log(hive["entry"], marker, tree) == str(_log(hive, bumped))


def test_the_background_child_gates_the_exact_sha_not_a_moving_ref(hive, spawned):
    """`HEAD` is spelled out to a sha before the child is spawned. A child re-resolving `HEAD`
    would gate whatever the branch happens to point at when it gets scheduled — and during a
    release the branch is exactly the thing that just moved."""
    bumped = _bump(hive)

    _run(hive, "attest", "HEAD", "--gate", GATE_CMD, "--background")

    assert spawned["cmdline"][3] == bumped
    assert "HEAD" not in spawned["cmdline"]


def test_the_marker_holds_no_verdict_at_all(hive, spawned):
    """The marker says only "a run was started". The VERDICT is always the ledger's, so nothing
    a background process writes about itself can ever read as a pass."""
    _run(hive, "attest", _bump(hive), "--gate", GATE_CMD, "--background")

    marker = json.loads(_marker(hive).read_text())

    assert "rc" not in marker and "green" not in marker and "verdict" not in marker
    assert _preflight(hive, rev=marker["sha"]).exit_code == 1  # firing a gate proves nothing


def test_firing_a_background_gate_does_not_let_the_push_go_out(hive, spawned):
    """The end-to-end version of the same point, at the seam that matters: `just push`'s
    `--if-pending` check sees a pending gate with no verdict and refuses."""
    bumped = _bump(hive)
    _run(hive, "attest", bumped, "--gate", GATE_CMD, "--background")

    assert _await(hive, "--if-pending", "--timeout", "0", rev=bumped).exit_code == 1


# ─── await: the push waits on the verdict instead of establishing green ──────────────────────


def test_await_returns_when_the_background_gate_lands_green(hive):
    bumped = _bump(hive)
    _fire(hive, rev=bumped)
    _attest(hive, rev=bumped)

    res = _await(hive, rev=bumped)

    assert res.exit_code == 0, res.output
    assert "safe to push" in res.output


def test_await_refuses_immediately_on_a_red_bump_tree(hive):
    """The GOOD failure: nothing has left the machine, so bh-67utw's undo is fully available.
    It must not wait out the timeout to say so — a release blocked for 30 minutes on a verdict
    that already exists is how an operator learns to bypass the check."""
    bumped = _bump(hive)
    _fire(hive, rev=bumped)
    _attest(hive, rev=bumped, rc=1)

    started = time.monotonic()
    res = _await(hive, "--timeout", "600", rev=bumped)

    assert res.exit_code == 1
    assert "DO NOT PUSH" in res.output
    assert "release recover" in res.output
    assert time.monotonic() - started < 10  # answered from the record, not by waiting


def test_await_refuses_when_the_gate_died_without_recording_a_verdict(hive):
    """A gate that crashed is not a gate that passed. Told apart from "still running" by the
    pid — the only thing the marker is for."""
    bumped = _bump(hive)
    dead = subprocess.Popen(["true"])
    dead.wait()
    _fire(hive, rev=bumped, pid=dead.pid)

    res = _await(hive, rev=bumped)

    assert res.exit_code == 1
    assert "WITHOUT recording a verdict" in res.output


def test_await_treats_a_timeout_as_a_refusal_not_a_pass(hive):
    """The subtlest way a wait could authorise a release: give up and shrug. It cannot."""
    bumped = _bump(hive)
    _fire(hive, rev=bumped)  # pid = this live process, so it never reads as dead

    res = _await(hive, "--timeout", "0", rev=bumped)

    assert res.exit_code == 1
    assert "NOT a pass" in res.output


def test_await_refuses_when_no_gate_was_ever_fired(hive):
    """A verdict nobody asked for cannot arrive, and waiting for it forever is not the answer."""
    res = _await(hive, rev=_bump(hive))

    assert res.exit_code == 1
    assert "no background gate was fired" in res.output


def test_await_accepts_a_green_verdict_earned_with_no_marker_at_all(hive):
    """bh-d3u1o: THE FIX. A tree proven by a foreground `just attest` — no background gate ever
    fired for it — is exactly as green as one a background gate proved. Before this bead, `await`
    demanded the marker first and refused a tree `preview` had already called GO on; now the
    ledger alone decides, which is the same lookup `preview` makes."""
    bumped = _bump(hive)
    _attest(hive, rev=bumped)  # foreground attest — no `_fire`, no marker, ever

    res = _await(hive, rev=bumped)

    assert res.exit_code == 0, res.output
    assert "attested green" in res.output


def test_await_still_refuses_a_red_verdict_earned_with_no_marker(hive):
    """The green case above does not become "no verdict required at all" — a RED foreground
    verdict with no marker refuses exactly as a red backgrounded one does."""
    bumped = _bump(hive)
    _attest(hive, rev=bumped, rc=1)

    res = _await(hive, rev=bumped)

    assert res.exit_code == 1
    assert "DO NOT PUSH" in res.output


@pytest.mark.parametrize("typed_verdict", ["red", "none"])
def test_await_refuses_typed_non_green_even_when_exit_code_is_zero(hive, typed_verdict):
    bumped = _bump(hive)
    _typed_attest(hive, typed_verdict, rev=bumped)
    raw = validation_ledger.verdict(hive["entry"], bumped, GATE_CMD)
    assert raw is not None and raw["rc"] == 0 and raw["verdict"] == typed_verdict

    res = _await(hive, rev=bumped)

    assert res.exit_code == 1
    assert "DO NOT PUSH" in res.output


@pytest.mark.parametrize(
    "contradiction",
    (
        {"signal": 15},
        {"schema": True},
        {"exit_code": False},
        {"reason": "setup_failure"},
    ),
)
def test_await_refuses_a_contradictory_green_manifest(hive, contradiction):
    bumped = _bump(hive)
    _attest(hive, rev=bumped)
    run = latest_verdict(hive["repo"], hive["entry"], bumped, GATE_CMD)
    run.update(contradiction)
    rewrite_verdict(hive["repo"], run)

    res = _await(hive, rev=bumped)

    assert res.exit_code == 1
    assert "safe to push" not in res.output


def test_await_refuses_a_stale_verdict_even_with_the_marker_present(hive):
    """The marker says a run happened; the ledger says its verdict has expired. The ledger wins —
    the marker is never evidence about greenness."""
    bumped = _bump(hive)
    dead = subprocess.Popen(["true"])
    dead.wait()
    _fire(hive, rev=bumped, pid=dead.pid)
    _attest(hive, rev=bumped)
    age_verdict(
        hive["repo"],
        hive["entry"],
        bumped,
        GATE_CMD,
        validation_ledger.LEDGER_TTL_SECONDS + 60,
    )

    assert _await(hive, rev=bumped).exit_code == 1


def test_await_refuses_a_verdict_earned_under_a_different_command(hive):
    bumped = _bump(hive)
    _fire(hive, rev=bumped)
    _attest(hive, rev=bumped, cmd="just check")  # the FAST gate, not this one

    res = _await(hive, "--timeout", "0", rev=bumped)

    assert res.exit_code == 1


def test_if_pending_skips_only_when_nothing_is_in_flight(hive):
    """The one leniency, and its exact width. `just push` must stay unchanged for an ordinary
    integration push, so `--if-pending` exits 0 when NO gate is pending. It cannot widen further:
    with a marker present it enforces in full."""
    assert _await(hive, "--if-pending").exit_code == 0  # nothing in flight — ordinary push

    bumped = _bump(hive)
    _fire(hive, rev=bumped)
    _attest(hive, rev=bumped, rc=1)

    assert _await(hive, "--if-pending", rev=bumped).exit_code == 1  # red still refuses


def test_if_pending_cannot_wave_through_a_still_running_gate(hive):
    bumped = _bump(hive)
    _fire(hive, rev=bumped)

    assert _await(hive, "--if-pending", "--timeout", "0", rev=bumped).exit_code == 1


def test_a_marker_for_another_tree_leaves_that_push_to_the_full_pre_push_gate(hive):
    """Amend after firing the gate and `--if-pending` sees nothing pending for the NEW tree — as
    it should, since that tree's gate was never started. Nothing is lost: the pre-push hook then
    misses on it and runs the full gate, exactly as before any of this existed."""
    _fire(hive, rev=_bump(hive))
    repo = hive["repo"]
    (repo / "extra.txt").write_text("late\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "fix: after the gate started", cwd=repo)
    other = _git("rev-parse", "HEAD", cwd=repo)

    assert _await(hive, "--if-pending", rev=other).exit_code == 0
    ok, _ = prepush.check_push_main(other, hive_id="mr", gate_cmd=GATE_CMD)
    assert ok is False  # …and the gate that actually guards main still refuses it


@pytest.mark.parametrize(
    ("name", "content"),
    [("not json", "{oops"), ("a list", "[]"), ("empty", ""), ("null", "null")],
)
def test_a_corrupt_marker_is_a_marker_that_is_not_there(hive, name, content):
    """And "not there" is never permission — bare `await` refuses, it does not assume green."""
    bumped = _bump(hive)
    _marker(hive).parent.mkdir(parents=True, exist_ok=True)
    _marker(hive).write_text(content)

    assert _await(hive, rev=bumped).exit_code == 1, name


def test_a_fifo_at_the_marker_path_reads_as_not_there(hive):
    """A FIFO makes `Path.read_text()` block forever rather than raise, which the OSError/
    ValueError clauses in `_read_marker` never catch (bh-0jgdz, inherited from bh-ku9n9.7).
    Calling the function directly, not through the CLI: an unfixed regression here would hang
    the whole test run rather than fail it."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is POSIX-only")
    _marker(hive).parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(_marker(hive))

    assert release._read_marker(hive["entry"]) == {}


def test_await_refuses_an_unconfigured_phase(hive):
    hive["entry"]["work"] = {"validate": {}, "validate_cmd": "just check"}

    assert _run(hive, "await", hive["sha"], "--if-pending").exit_code == 1


def test_a_marker_from_another_host_never_reads_as_a_dead_process(hive):
    """`os.kill(pid, 0)` answers about THIS host's pid space. A marker minted elsewhere would
    make an unrelated live pid look like our gate, or a recycled one look dead — so liveness
    becomes "cannot tell", and cannot-tell keeps waiting rather than concluding."""
    marker = {"pid": os.getpid(), "host": "some-other-host-uuid"}

    assert release._still_running(marker) is None
    assert release._still_running({"pid": "not an int"}) is None
    assert release._still_running({"pid": os.getpid(), "host": host.host_id()}) is True


# ─── pending: `just push`'s refuse-not-wait pre-flight (bh-8c2yo) ────────────────────────────
#
# `bh release pending` answers a narrower question than `await`: not "is it green yet" but "is a
# release in flight for this tree at all" — and it answers immediately, without waiting, because
# `just push` must refuse rather than land main while the gate is still running or red. Every
# test here is one of the FAIL-OPEN shapes: everything except a marker that names REV's own tree
# must read as "not pending", identically to how `await` treats the same shapes as nothing to
# wait on.


def _pending(hive, rev: str | None = None, gate: str = GATE_CMD):
    return _run(hive, "pending", rev or hive["sha"], "--gate", gate)


def test_pending_is_true_only_once_a_marker_names_this_tree(hive):
    bumped = _bump(hive)

    assert _pending(hive, rev=bumped).exit_code == 1  # nothing fired yet — an ordinary push

    _fire(hive, rev=bumped)

    assert _pending(hive, rev=bumped).exit_code == 0


def test_pending_does_not_care_whether_the_verdict_is_green_red_or_absent(hive):
    """`await` tells green from red from still-running; `pending` collapses all three into one
    answer, because `just push` must refuse the moment a release is in flight, not once it knows
    how that release turns out."""
    bumped = _bump(hive)
    _fire(hive, rev=bumped)

    assert _pending(hive, rev=bumped).exit_code == 0  # still running, no verdict yet

    _attest(hive, rev=bumped, rc=1)
    assert _pending(hive, rev=bumped).exit_code == 0  # red

    _attest(hive, rev=bumped, rc=0)
    assert _pending(hive, rev=bumped).exit_code == 0  # green


def test_pending_is_false_for_a_marker_naming_a_different_tree(hive):
    """A marker fired for the bump tree says nothing about a LATER tree — exactly the shape
    `await --if-pending` already leaves to the full pre-push gate."""
    _fire(hive, rev=_bump(hive))
    repo = hive["repo"]
    (repo / "extra.txt").write_text("late\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "fix: after the gate started", cwd=repo)
    other = _git("rev-parse", "HEAD", cwd=repo)

    assert _pending(hive, rev=other).exit_code == 1


@pytest.mark.parametrize(
    ("name", "content"),
    [("not json", "{oops"), ("a list", "[]"), ("empty", ""), ("null", "null")],
)
def test_pending_fails_open_on_a_corrupt_marker(hive, name, content):
    bumped = _bump(hive)
    _marker(hive).parent.mkdir(parents=True, exist_ok=True)
    _marker(hive).write_text(content)

    assert _pending(hive, rev=bumped).exit_code == 1, name


def test_pending_fails_open_with_no_marker_file_at_all(hive):
    """The ordinary case, proven directly: a fresh hive that has never bumped has no marker file
    on disk whatsoever, and that must read as "not pending", never as an error."""
    assert not _marker(hive).exists()

    assert _pending(hive).exit_code == 1


def test_pending_fails_open_on_an_unconfigured_phase(hive):
    hive["entry"]["work"] = {"validate": {}, "validate_cmd": "just check"}

    assert _pending(hive).exit_code == 1


def test_pending_fails_open_when_no_hive_is_managed(hive, monkeypatch):
    monkeypatch.setattr(config, "load", lambda *a, **k: {"managed_repos": []})

    assert runner.invoke(app, ["release", "pending", hive["sha"]]).exit_code == 1


def test_pending_fails_open_when_config_explodes(hive, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config is on fire")

    monkeypatch.setattr(config, "load", boom)

    res = runner.invoke(app, ["release", "pending", hive["sha"]])

    assert res.exit_code != 0
    assert res.exception is None or isinstance(res.exception, SystemExit)


def test_pending_and_await_agree_about_what_is_pending(hive):
    """The acceptance bar, stated as a behavioral invariant rather than by reading source, for the
    states this test exercises: none fired, fired-with-no-verdict-yet, and red. Across those three,
    the two verbs share `_marker_for_tree` (bh-8c2yo), so either both see a pending gate for this
    tree or neither does.

    GREEN is deliberately excluded, not merely untested: there both `pending` (a marker is not
    consumed once attested, so it still names this tree) and `await --if-pending --timeout 0`
    (now safe to push) exit 0 — but for different reasons, one answering "is a gate in flight for
    this tree" and the other "is it safe to stop waiting and push". `in_flight()`'s XOR-style
    proxy below (`pending`'s 0 vs. `await`'s 1) does not extend to that case, and the divergence
    is the whole point of bh-8c2yo: `just push`'s pre-flight must refuse a push mid-release even
    once the bump is green, which is exactly why it asks `pending` directly rather than proxying
    through `await`."""
    bumped = _bump(hive)

    def in_flight() -> bool:
        was_pending = _pending(hive, rev=bumped).exit_code == 0
        was_waited_on = _await(hive, "--if-pending", "--timeout", "0", rev=bumped).exit_code == 1
        assert was_pending == was_waited_on
        return was_pending

    assert in_flight() is False  # nothing fired

    _fire(hive, rev=bumped)
    assert in_flight() is True  # fired, no verdict — await would refuse to wave it through

    _attest(hive, rev=bumped, rc=1)
    assert in_flight() is True  # red


# ─── recover: ONE measured fact, never an assumption ─────────────────────────────────────────


def _publish(hive, *refs: str) -> None:
    _git("push", "-q", "origin", *refs, cwd=hive["repo"])


def _recover(hive, rev: str, *extra: str):
    return _run(hive, "recover", rev, *extra)


def test_case_A_the_tag_never_left_so_the_undo_is_safe(hive):
    """bh-67utw case A, and the premise of the entire ordering: nothing reached the remote, so
    the bump can be rewritten away completely."""
    bumped = _bump(hive)

    res = _recover(hive, bumped)

    assert res.exit_code == 0, res.output
    assert "SAFE TO UNDO" in res.output
    assert "ls-remote" in res.output  # measured, and it says so


@pytest.mark.parametrize("legacy", [False, True])
def test_recover_identifies_pending_canonical_or_legacy_gate_without_writing(hive, legacy):
    bumped = _bump(hive)
    _fire(hive, rev=bumped)
    if legacy:
        _legacy_marker(hive).parent.mkdir(parents=True, exist_ok=True)
        _legacy_marker(hive).write_text(_marker(hive).read_text())
        _marker(hive).unlink()

    repo_private = hive["repo"] / ".bh"
    before = sorted(repo_private.rglob("*")) if repo_private.exists() else []
    res = _recover(hive, bumped)

    assert res.exit_code == 1
    assert "background gate" in res.output
    after = sorted(repo_private.rglob("*")) if repo_private.exists() else []
    assert after == before


def test_case_A_names_bh_67utw_rather_than_performing_the_rewrite(hive):
    """The recovery RULE is bh-67utw's. This verb measures and decides; two copies of a history
    rewrite is two recipes to disagree — and it must not mutate a ref while deciding."""
    bumped = _bump(hive)

    res = _recover(hive, bumped)

    assert "bh-67utw" in res.output
    assert "rebase --rebase-merges --onto" in res.output  # named, not run
    assert _git("rev-parse", "HEAD", cwd=hive["repo"]) == bumped
    assert _git("tag", "--list", cwd=hive["repo"]) == "v0.1.1"


def test_case_B_a_published_tag_is_left_alone(hive):
    """The point of no return, measured on the remote. A published tag may already have fired
    .github/workflows/release.yml and been consumed downstream — a failed release rolls FORWARD."""
    bumped = _bump(hive)
    _publish(hive, "main", "v0.1.1")

    res = _recover(hive, bumped)

    assert res.exit_code == 1
    assert "IS ON origin" in res.output
    assert "DO NOT delete or move it" in res.output
    assert _git("tag", "--list", cwd=hive["repo"]) == "v0.1.1"  # and it did not touch it


def test_the_branch_turns_on_the_remote_tag_not_the_local_one(hive):
    """THE LOAD-BEARING MEASUREMENT. A local tag looks identical in both cases; only the remote
    distinguishes them. This is the fact the 0.11.5 recovery had to reason out by hand."""
    bumped = _bump(hive)
    assert _recover(hive, bumped).exit_code == 0  # local tag exists, remote has nothing → case A

    _publish(hive, "v0.1.1")

    assert _recover(hive, bumped).exit_code == 1  # same local state, opposite answer


def test_a_local_remote_tracking_ref_is_never_the_evidence(hive):
    """`git branch -r --contains` reads refs a FAILED push may never have updated — which is
    exactly how the incident's first push was reported as a success. Fabricate a lying tracking
    ref and the decision must be unmoved, because it never looked at one."""
    bumped = _bump(hive)
    _git("update-ref", "refs/remotes/origin/main", bumped, cwd=hive["repo"])
    _git("update-ref", "refs/remotes/origin/tags/v0.1.1", bumped, cwd=hive["repo"])

    res = _recover(hive, bumped)

    assert res.exit_code == 0  # the real remote is still empty, so case A stands
    assert "SAFE TO UNDO" in res.output


def test_main_landed_without_its_tag_is_reported_as_half_done_not_undoable(hive):
    """bh-zfvbp's worst state: main published (undo closed) and nothing released (the workflow
    fires on the tag). The atomic push is what makes it unreachable; it is measured for anyway,
    because a state that "cannot happen" is the one nobody checks."""
    bumped = _bump(hive)
    _publish(hive, "main")

    res = _recover(hive, bumped)

    assert res.exit_code == 2
    assert "HALF-DONE RELEASE" in res.output
    assert "git push origin v0.1.1" in res.output  # finish it — never undo it


def test_an_unreadable_remote_concludes_nothing(hive):
    """The third answer, kept apart on purpose (bh-dt2d9). Folding "I could not look" into "it is
    not there" would rewrite history on a network blip — the one outcome worse than doing
    nothing."""
    res = _recover(hive, _bump(hive), "--remote", "no-such-remote")

    assert res.exit_code == 3
    assert "COULD NOT MEASURE" in res.output
    assert "SAFE TO UNDO" not in res.output


def test_an_unreadable_remote_branch_also_concludes_nothing(hive, monkeypatch):
    """Half the fact is not the fact: with the tag absent, whether main already carries the bump
    decides between "undoable" and "half-done", so an unreadable branch is unmeasurable too."""
    real = release._ls_remote

    def only_tags_readable(main, remote, pattern):
        return real(main, remote, pattern) if pattern.startswith("refs/tags/") else (128, [])

    monkeypatch.setattr(release, "_ls_remote", only_tags_readable)

    res = _recover(hive, _bump(hive))

    assert res.exit_code == 3
    assert "COULD NOT MEASURE main" in res.output


def test_a_remote_head_this_clone_does_not_hold_concludes_nothing(hive, monkeypatch):
    """`merge-base --is-ancestor` against an unknown object exits 128, not 1. Reading that as
    "not an ancestor" would call a landed bump undoable."""
    monkeypatch.setattr(
        release,
        "_ls_remote",
        lambda main, remote, pattern: (
            0,
            [] if "tags" in pattern else [f"{'ab' * 20} refs/heads/main"],
        ),
    )

    res = _recover(hive, _bump(hive))

    assert res.exit_code == 3
    assert "not an object this clone" in res.output


def test_recover_refuses_to_guess_which_tag_it_is_about(hive):
    """Two tags at the bump, or none, and the decision has no subject. Guessing is not acceptable
    when the answer authorises a history rewrite."""
    bumped = _bump(hive)
    _git("tag", "v0.1.1-rc1", cwd=hive["repo"])

    ambiguous = _recover(hive, bumped)
    assert ambiguous.exit_code == 3
    assert "more than one tag" in ambiguous.output

    untagged = _recover(hive, f"{bumped}~1")
    assert untagged.exit_code == 3
    assert "no tag at" in untagged.output


def test_recover_cannot_resolve_an_unknown_rev(hive):
    assert _recover(hive, "deadbeef" * 5).exit_code == 3


def _recovery_backups(hive) -> list[str]:
    out = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=hive["repo"])
    return [line for line in out.splitlines() if line.startswith("refs/bh/release-recovery/")]


def _staged_recoveries(hive) -> list[str]:
    out = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=hive["repo"])
    return [
        line for line in out.splitlines() if line.startswith("refs/bh/release-recovery-staged/")
    ]


def test_recover_dry_run_proves_the_exact_plan_without_changing_any_ref(hive):
    bumped = _bump(hive)
    refs_before = _git("show-ref", cwd=hive["repo"])

    res = _recover(hive, bumped, "--dry-run")

    assert res.exit_code == 0, res.output
    assert "DRY RUN ONLY" in res.output
    assert "rebase --rebase-merges" in res.output
    assert _git("show-ref", cwd=hive["repo"]) == refs_before
    assert _git("rev-parse", "HEAD", cwd=hive["repo"]) == bumped
    assert _git("tag", "--list", cwd=hive["repo"]) == "v0.1.1"
    assert not _recovery_backups(hive)


def test_recover_apply_removes_a_tip_bump_and_keeps_an_exact_backup(hive):
    bumped = _bump(hive)
    pre_bump = _git("rev-parse", f"{bumped}^", cwd=hive["repo"])

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 0, res.output
    assert "RECOVERED v0.1.1" in res.output
    assert _git("rev-parse", "HEAD", cwd=hive["repo"]) == pre_bump
    assert _git("tag", "--list", cwd=hive["repo"]) == ""
    assert _git("status", "--porcelain", cwd=hive["repo"]) == ""
    assert "branch=" in res.output
    assert "backup/staged refs exact, tag absent, no rebase" in res.output
    backups = _recovery_backups(hive)
    assert len(backups) == 1
    assert backups[0].endswith(f" {bumped}")
    assert len(_staged_recoveries(hive)) == 1
    assert _staged_recoveries(hive)[0].endswith(f" {pre_bump}")
    assert not (hive["repo"] / "pyproject.toml").exists()
    assert not (hive["repo"] / "CHANGELOG.md").exists()
    assert "bh-release-recovery-" not in _git("worktree", "list", "--porcelain", cwd=hive["repo"])


def test_recover_apply_preserves_a_merge_buried_above_the_bump(hive):
    repo = hive["repo"]
    bumped = _bump(hive)
    pre_bump = _git("rev-parse", f"{bumped}^", cwd=repo)
    _git("checkout", "-qb", "feature", cwd=repo)
    (repo / "feature.txt").write_text("feature\n")
    _git("add", "feature.txt", cwd=repo)
    _git("commit", "-qm", "feat: after bump", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "main.txt").write_text("main\n")
    _git("add", "main.txt", cwd=repo)
    _git("commit", "-qm", "fix: after bump", cwd=repo)
    _git("merge", "-q", "--no-ff", "feature", "-m", "chore: merge feature", cwd=repo)
    _git("branch", "-d", "feature", cwd=repo)
    old_head = _git("rev-parse", "HEAD", cwd=repo)
    old_merges = _git("rev-list", "--count", "--merges", f"{pre_bump}..HEAD", cwd=repo)

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 0, res.output
    assert _git("rev-list", "--count", "--merges", f"{pre_bump}..HEAD", cwd=repo) == old_merges
    assert (repo / "feature.txt").read_text() == "feature\n"
    assert (repo / "main.txt").read_text() == "main\n"
    assert not (repo / "pyproject.toml").exists()
    assert not (repo / "CHANGELOG.md").exists()
    assert _recovery_backups(hive)[0].endswith(f" {old_head}")


def test_recover_apply_refuses_dirty_main_without_mutating_refs(hive):
    bumped = _bump(hive)
    (hive["repo"] / "f.txt").write_text("dirty\n")
    refs_before = _git("show-ref", cwd=hive["repo"])

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 1
    assert "worktree is dirty" in res.output
    assert _git("show-ref", cwd=hive["repo"]) == refs_before
    assert not _recovery_backups(hive)


def test_recover_apply_refuses_a_non_release_file_in_the_bump(hive):
    repo = hive["repo"]
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.1.1"\n')
    (repo / "CHANGELOG.md").write_text("## 0.1.1\n")
    (repo / "payload.txt").write_text("not release metadata\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "bump: version 0.1.1", cwd=repo)
    _git("tag", "v0.1.1", cwd=repo)
    bumped = _git("rev-parse", "HEAD", cwd=repo)
    refs_before = _git("show-ref", cwd=repo)

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 1
    assert "non-release file(s): payload.txt" in res.output
    assert _git("show-ref", cwd=repo) == refs_before
    assert not _recovery_backups(hive)


def test_recover_apply_refuses_when_any_remote_ref_contains_the_bump(hive):
    bumped = _bump(hive)
    _git("push", "-q", "origin", f"{bumped}:refs/heads/recovery-trap", cwd=hive["repo"])
    refs_before = _git("show-ref", cwd=hive["repo"])

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 1
    assert "refs/heads/recovery-trap" in res.output
    assert "IS CONTAINED" in res.output
    assert _git("show-ref", cwd=hive["repo"]) == refs_before
    assert not _recovery_backups(hive)


def test_recover_apply_refuses_an_unknown_remote_ref_as_unmeasurable(hive, tmp_path):
    bumped = _bump(hive)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git("init", "-q", "-b", "other", cwd=foreign)
    _git("config", "user.email", "foreign@example.com", cwd=foreign)
    _git("config", "user.name", "foreign", cwd=foreign)
    _git("config", "commit.gpgsign", "false", cwd=foreign)
    _git("remote", "add", "origin", str(hive["remote"]), cwd=foreign)
    (foreign / "foreign.txt").write_text("unknown here\n")
    _git("add", "foreign.txt", cwd=foreign)
    _git("commit", "-qm", "feat: foreign", cwd=foreign)
    _git("push", "-q", "origin", "other:refs/heads/other", cwd=foreign)
    refs_before = _git("show-ref", cwd=hive["repo"])

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 3
    assert "object this clone does not hold" in res.output
    assert _git("show-ref", cwd=hive["repo"]) == refs_before
    assert not _recovery_backups(hive)


def test_recover_apply_leaves_main_untouched_when_the_staged_rewrite_conflicts(hive):
    repo = hive["repo"]
    bumped = _bump(hive)
    (repo / "CHANGELOG.md").write_text("rewritten after bump\n")
    _git("add", "CHANGELOG.md", cwd=repo)
    _git("commit", "-qm", "docs: rewrite changelog", cwd=repo)
    old_head = _git("rev-parse", "HEAD", cwd=repo)

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 1
    assert "staged rewrite failed" in res.output
    assert "main and v0.1.1 were never changed" in res.output
    assert _git("rev-parse", "HEAD", cwd=repo) == old_head
    assert _git("tag", "--list", cwd=repo) == "v0.1.1"
    assert _git("status", "--porcelain", cwd=repo) == ""
    backups = _recovery_backups(hive)
    assert len(backups) == 1
    assert backups[0].endswith(f" {old_head}")
    assert "bh-release-recovery-" not in _git("worktree", "list", "--porcelain", cwd=repo)


def test_rollback_never_deletes_concurrent_untracked_content(hive, monkeypatch):
    """The reproduced review failure: rollback must not hard-reset over a concurrent file."""
    repo = hive["repo"]
    bumped = _bump(hive)

    def fail_final_with_untracked(*args, **kwargs):
        (repo / "pyproject.toml").write_text("concurrent and untracked\n")
        return release.REFUSED, "simulated final-proof failure"

    monkeypatch.setattr(release, "_final_recovery_proof", fail_final_with_untracked)

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 1
    assert "ROLLBACK FAILED/incomplete" in res.output
    assert "untracked content blocks safe branch restore: pyproject.toml" in res.output
    assert (repo / "pyproject.toml").read_text() == "concurrent and untracked\n"
    assert _git("tag", "--list", cwd=repo) == ""
    backups = _recovery_backups(hive)
    assert len(backups) == 1
    assert backups[0].endswith(f" {bumped}")
    staged = _staged_recoveries(hive)
    assert len(staged) == 1
    assert staged[0].endswith(f" {_git('rev-parse', f'{bumped}^', cwd=repo)}")


def test_a_concurrent_clean_branch_move_cannot_be_reported_as_success(hive, monkeypatch):
    """Move main after the tag CAS, keep it clean, and point it back through the bump."""
    repo = hive["repo"]
    bumped = _bump(hive)
    (repo / "after.txt").write_text("after bump\n")
    _git("add", "after.txt", cwd=repo)
    _git("commit", "-qm", "fix: after bump", cwd=repo)
    old_head = _git("rev-parse", "HEAD", cwd=repo)
    real_install = release._install_recovery_transaction

    def move_branch_after_cas(main, branch, tag, new_head, backup, staged_ref, plan):
        result = real_install(main, branch, tag, new_head, backup, staged_ref, plan)
        assert result.returncode == 0
        _git("reset", "--hard", bumped, cwd=repo)  # concurrent actor; leaves a clean worktree
        return result

    monkeypatch.setattr(release, "_install_recovery_transaction", move_branch_after_cas)

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 1
    assert "final clean-slate proof failed" in res.output
    assert "main no longer equals rewritten head" in res.output
    assert "ROLLBACK FAILED/incomplete" in res.output
    assert "RECOVERED" not in res.output
    assert _git("rev-parse", "HEAD", cwd=repo) == bumped
    assert _git("merge-base", "--is-ancestor", bumped, "main", cwd=repo) == ""
    assert _git("status", "--porcelain", cwd=repo) == ""
    assert _git("tag", "--list", cwd=repo) == ""
    backups = _recovery_backups(hive)
    assert len(backups) == 1
    assert backups[0].endswith(f" {old_head}")
    assert len(_staged_recoveries(hive)) == 1


def test_concurrent_pre_rewrite_main_advance_survives_install_refusal(hive, monkeypatch):
    """Stage from plan.old_head, then refuse CAS without touching a clean concurrent advance."""
    repo = hive["repo"]
    bumped = _bump(hive)
    planned_head = _git("rev-parse", "HEAD", cwd=repo)
    real_stage = release._stage_recovery
    concurrent_head = ""

    def advance_then_stage(main, bump_sha, backup, plan):
        nonlocal concurrent_head
        (repo / "concurrent.txt").write_text("must survive\n")
        _git("add", "concurrent.txt", cwd=repo)
        _git("commit", "-qm", "fix: concurrent main advance", cwd=repo)
        concurrent_head = _git("rev-parse", "HEAD", cwd=repo)
        return real_stage(main, bump_sha, backup, plan)

    monkeypatch.setattr(release, "_stage_recovery", advance_then_stage)

    res = _recover(hive, bumped, "--apply")

    assert res.exit_code == 1
    assert "advanced from planned head" in res.output
    assert "concurrent main state was left intact" in res.output
    assert "RECOVERED" not in res.output
    assert "restored" not in res.output.lower()
    assert _git("rev-parse", "HEAD", cwd=repo) == concurrent_head
    assert _git("show", f"{concurrent_head}:concurrent.txt", cwd=repo) == "must survive"
    assert (repo / "concurrent.txt").read_text() == "must survive\n"
    assert _git("status", "--porcelain", cwd=repo) == ""
    assert _git("tag", "--list", cwd=repo) == "v0.1.1"
    backups = _recovery_backups(hive)
    staged = _staged_recoveries(hive)
    assert len(backups) == len(staged) == 1
    assert backups[0].endswith(f" {planned_head}")
    staged_head = staged[0].split()[-1]
    assert _git("merge-base", bumped, staged_head, cwd=repo) != bumped
    assert "bh-release-recovery-" not in _git("worktree", "list", "--porcelain", cwd=repo)


# ─── the atomic push: "main without its tag" is not a reachable state ────────────────────────


def _push_script(hive, *args: str):
    return subprocess.run(
        ["bash", str(PUSH_SCRIPT), *args],
        cwd=str(hive["repo"]),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_SSH_COMMAND": "ssh -o ServerAliveInterval=30"},
    )


def test_the_release_push_lands_main_and_its_tag_together(hive):
    """One `git push --atomic`: git updates both refs or neither. That is what removes the state
    bh-zfvbp measured, where `just push` reported verified success on a half-done release."""
    _bump(hive)

    res = _push_script(hive, "origin", "main", "v0.1.1")

    assert res.returncode == 0, res.stderr
    assert _git("ls-remote", "--tags", "origin", "v0.1.1", cwd=hive["repo"])
    assert "atomic push" in res.stderr


def test_the_push_verifies_the_tag_against_the_remote_not_a_local_ref(hive):
    """Same discipline the branch already had, and the discipline that caught every failure in
    the incident: the success line is earned from `ls-remote`, not from git's exit code."""
    _bump(hive)

    res = _push_script(hive, "origin", "main", "v0.1.1")

    assert "v0.1.1 is on origin — verified with ls-remote" in res.stderr
    assert "point of no return" in res.stderr


def test_the_push_refuses_rather_than_landing_main_without_the_named_tag(hive):
    """Asked to publish a tag that does not exist locally, the answer is to push NOTHING. Pushing
    main alone would close the undo path and release nothing — the worst of the two states."""
    _bump(hive)

    res = _push_script(hive, "origin", "main", "v9.9.9")

    assert res.returncode == 2
    assert "no local tag" in res.stderr
    assert _git("ls-remote", "--heads", "origin", "main", cwd=hive["repo"]) == ""  # nothing left


def test_an_ordinary_push_with_no_tag_is_unchanged(hive):
    """The general path stays exactly as it was — no `--atomic`, no tag talk. bh-zfvbp owns
    teaching it to DISCOVER an unpushed bump tag; this bead only adds the capability."""
    res = _push_script(hive, "origin", "main")

    assert res.returncode == 0, res.stderr
    assert "atomic push" not in res.stderr
    assert _git("ls-remote", "--heads", "origin", "main", cwd=hive["repo"])


# ─── the ledger read this bead added ─────────────────────────────────────────────────────────


def test_verdict_surfaces_a_red_entry_that_green_verdict_hides(hive):
    """`await` must tell "finished and failed" from "not finished yet"; `green_verdict` collapses
    both to None. The raw read exists for that one distinction and nothing consumes a non-green
    return except a caller that refuses harder because of it."""
    _attest(hive, rc=1)

    assert validation_ledger.verdict(hive["entry"], hive["sha"], GATE_CMD)["rc"] == 1
    assert validation_ledger.green_verdict(hive["entry"], hive["sha"], GATE_CMD) is None


def test_verdict_hides_a_stale_entry_from_both_reads(hive):
    _attest(hive, rc=1)
    age_verdict(
        hive["repo"],
        hive["entry"],
        hive["sha"],
        GATE_CMD,
        validation_ledger.LEDGER_TTL_SECONDS + 60,
    )

    assert validation_ledger.verdict(hive["entry"], hive["sha"], GATE_CMD) is None


def test_a_string_rc_makes_the_derived_pointer_a_miss(hive):
    """A corrupt pointer is not run authority; both reads fail closed."""
    _attest(hive)
    entry = json.loads(_ledger(hive).read_text())
    entry["rc"] = "0"
    _ledger(hive).write_text(json.dumps(entry))

    assert validation_ledger.verdict(hive["entry"], hive["sha"], GATE_CMD) is None
    assert validation_ledger.green_verdict(hive["entry"], hive["sha"], GATE_CMD) is None


# ─── one resolver, so a bump and a push can never disagree ───────────────────────────────────


def test_the_bump_and_the_push_resolve_the_gate_through_the_same_function(hive):
    """`prepush.push_main_cmd` is shared, not copied. Two resolvers is how a bump gets proven
    against a command the push would not accept — and that divergence would be invisible until a
    release."""
    _attest(hive)
    assert _preflight(hive).exit_code == 0
    assert prepush.check_push_main(hive["sha"], hive_id="mr", gate_cmd=GATE_CMD)[0] is True

    hive["entry"]["work"]["validate"]["push-main"] = "just check"

    assert _preflight(hive).exit_code == 1
    assert prepush.check_push_main(hive["sha"], hive_id="mr", gate_cmd=GATE_CMD)[0] is False


def test_future_legacy_none_cannot_shadow_canonical_release_or_prepush(hive):
    """A real canonical green repairs both outer boundaries after a future legacy anomaly."""
    repo = hive["repo"]
    tree = _git("rev-parse", f"{hive['sha']}^{{tree}}", cwd=repo)
    legacy = repo / ".git" / validation_ledger.LEGACY_LEDGER_FILENAME
    legacy.write_text(
        json.dumps(
            [
                {
                    "tree": tree,
                    "cmd_hash": validation_ledger.cmd_hash(GATE_CMD),
                    "rc": 0,
                    "at": time.time() + 10 * 365 * 24 * 60 * 60,
                }
            ]
        )
    )
    assert validation_ledger.verdict(hive["entry"], hive["sha"], GATE_CMD) is None

    validation_ledger.record(hive["entry"], hive["sha"], GATE_CMD, 0)
    assert validation_ledger.rebuild_verdict_index(hive["entry"]) == 1
    assert prepush.check_push_main(hive["sha"], hive_id="mr", gate_cmd=GATE_CMD)[0] is True
    assert _await(hive).exit_code == 0


# ─── attest --if-needed: the one flag that makes attest idempotent (bh-0jndj) ────────────────


def _clean_checkout_spy(monkeypatch, rc: int = 0) -> dict:
    """Capture what `attest` asks `worktree.clean_checkout` for, without running a gate."""
    from beadhive import worktree

    seen: dict = {}

    def fake(entry, rev, cmd, cfg=None, reuse=False):
        seen.update(rev=rev, cmd=cmd, reuse=reuse)
        validation_ledger.record(entry, rev, cmd, rc)
        return rc

    monkeypatch.setattr(worktree, "clean_checkout", fake)
    return seen


def test_if_needed_asks_for_prove_or_skip_rather_than_a_second_lookup(hive, monkeypatch):
    """`--if-needed` IS `clean_checkout(reuse=True)` — the same prove-or-skip every landing
    boundary already uses (bh-ku9n9.17). Not a parallel code path, not a second reader of the
    ledger: one flag threaded into the one writer, so "is this tree proven?" can only ever be
    answered in one place."""
    seen = _clean_checkout_spy(monkeypatch)

    assert _run(hive, "attest", "--if-needed", "--gate", GATE_CMD).exit_code == 0

    assert seen["reuse"] is True


def test_attest_still_defaults_to_running_the_gate_for_real(hive, monkeypatch):
    """The default must stay OFF. `attest`'s designed job is the tree `cz bump` just wrote, which
    by construction has no verdict to reuse — a reusing default would make a bump-tree attestation
    look established when nothing had run."""
    seen = _clean_checkout_spy(monkeypatch)

    assert _run(hive, "attest", "--gate", GATE_CMD).exit_code == 0

    assert seen["reuse"] is False


def test_a_red_run_is_still_red_when_it_was_asked_for_if_needed(hive, monkeypatch):
    """Idempotence is about skipping work already done, never about softening the answer."""
    _clean_checkout_spy(monkeypatch, rc=1)

    res = _run(hive, "attest", "--if-needed", "--gate", GATE_CMD)

    assert res.exit_code == 1
    assert _preflight(hive).exit_code == 1


# ─── preview: the read-only forward view (bh-0jndj) ──────────────────────────────────────────


def _preview(hive, *extra: str):
    return _run(hive, "preview", "--gate", GATE_CMD, *extra)


def test_preview_reports_an_unattested_tree_instead_of_refusing_it(hive):
    """The difference from `preflight`, which is the whole reason this verb exists: `preflight`
    exits 1 on a miss because it GATES a bump. A preview that did the same would hide the tag and
    artifact answers behind the first bad one — exactly what you do not want in front of a
    one-way door."""
    res = _preview(hive)

    assert res.exit_code == 0, res.output
    assert "not attested" in res.output
    assert "just attest" in res.output
    assert "tag" in res.output  # the other checks still ran and were reported


def test_preview_reports_a_green_tree_as_green(hive):
    _attest(hive)

    res = _preview(hive)

    assert res.exit_code == 0
    assert "attested green" in res.output


def test_preview_and_await_agree_on_the_fix_forward_shape(hive):
    """bh-d3u1o, end to end: `just bump`'s background gate fires on the bump tree, goes RED, gets
    fixed forward in more commits (a NEW tree the marker never named), and that new tree is proven
    by hand with `just attest` — no marker, genuinely green. `preview` (the dry run) and `await`
    (what the real release waits on) must give the SAME answer for it; before this bead `preview`
    said GO and `await` refused."""
    repo = hive["repo"]
    bumped = _bump(hive)
    _fire(hive, rev=bumped)
    _attest(hive, rev=bumped, rc=1)  # the background gate's own verdict: RED

    (repo / "fix.txt").write_text("fix\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "fix: lint", cwd=repo)
    fixed = _git("rev-parse", "HEAD", cwd=repo)
    _git("tag", "-f", "v0.1.1", fixed, cwd=repo)
    _attest(hive, rev=fixed)  # `just attest` in the foreground — no marker for this tree

    prev = _preview(hive, fixed)
    aw = _await(hive, "--if-pending", rev=fixed)

    assert prev.exit_code == 0
    assert "attested green" in prev.output
    assert aw.exit_code == 0, aw.output
    assert "attested green" in aw.output


def test_preview_measures_the_tag_against_the_actual_remote(hive):
    """The same fact bh-67utw's undo rule turns on, measured the same way `recover` measures it:
    `git ls-remote` against the real remote, never a local tracking ref."""
    _bump(hive)
    _git("push", "-q", "origin", "main", "v0.1.1", cwd=hive["repo"])

    res = _preview(hive, "--tag", "v0.1.1")

    assert res.exit_code == 0
    assert "IS ALREADY ON origin" in res.output


def test_preview_says_the_tag_never_left_when_it_never_left(hive):
    _bump(hive)

    res = _preview(hive, "--tag", "v0.1.1")

    assert "v0.1.1 is not on origin" in res.output
    assert "fully reversible" in res.output


def test_preview_keeps_could_not_measure_apart_from_not_there(hive):
    """bh-dt2d9, in the forward direction. "I could not look" is never folded into "it is not
    there" — that collapse is how the 0.11.5 incident produced a confident wrong sentence."""
    _bump(hive)
    _git("remote", "set-url", "origin", str(hive["repo"] / "nope.git"), cwd=hive["repo"])

    res = _preview(hive, "--tag", "v0.1.1")

    assert res.exit_code == 0
    assert "COULD NOT MEASURE" in res.output
    assert "is not on origin" not in res.output


def test_preview_derives_the_tag_from_pyproject_rather_than_asking_for_it(hive):
    """One source of truth for the version, the same one `scripts/release-pin.sh` reads."""
    _bump(hive, version="0.4.2")

    assert "v0.4.2" in _preview(hive).output


def test_preview_writes_nothing_and_pushes_nothing(hive):
    """READ-ONLY, asserted rather than asserted-in-a-docstring: no verdict established, no ref
    moved. A preview that could establish green would be a second `preflight` with the opposite
    contract, which is the confusion this bead exists to remove."""
    _bump(hive)
    before = _git("ls-remote", "origin", cwd=hive["repo"])

    _preview(hive)

    assert not _ledger(hive).exists()
    assert _git("ls-remote", "origin", cwd=hive["repo"]) == before


def test_preview_concludes_nothing_when_there_is_no_clone_to_read(hive, monkeypatch):
    monkeypatch.setattr(config, "load", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))

    res = _preview(hive)

    assert res.exit_code == 3
    assert "COULD NOT MEASURE" in res.output


# ─── --next, and the released-version state it exists for (bh-k5te9) ─────────────────────────
#
# The pin comes from pyproject, which is the version ALREADY SHIPPED until `cz bump` runs — so
# before a bump the report answered a question nobody asked, with two ✗ marks that read as
# failures. Two fixes that compose: say so when that is the state, and let `--next` ask about the
# version the bump WOULD create.


def _next_script(hive, out: str, rc: int = 0) -> None:
    """Stand in for `scripts/next-version.sh` — the ONE lookup, which is `uv run cz bump
    --dry-run` and nothing else. Stubbed here for the same reason `_bump` stands in for `cz bump`:
    what is under test is which number this verb reports, not commitizen's own arithmetic."""
    (hive["repo"] / "scripts").mkdir(exist_ok=True)
    script = hive["repo"] / "scripts" / "next-version.sh"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{out}\nEOF\nexit {rc}\n")
    script.chmod(0o755)


def test_next_previews_the_version_the_bump_would_create(hive):
    """The question people actually bring to a preview is a BEFORE-bump one — "is the path
    clear?" — and answering it about the version already shipped is worse than not answering."""
    _bump(hive, version="0.1.1")
    _next_script(hive, "bump: version 0.1.1 → 0.2.0\ntag to create: v0.2.0\nincrement: MINOR")

    res = _preview(hive, "--next")

    assert res.exit_code == 0, res.output
    assert "release preview --next" in res.output
    assert "tag v0.2.0" in res.output  # every check below asks about the NEXT version
    assert "v0.2.0 is not on origin" in res.output


def test_next_takes_the_number_from_commitizen_rather_than_computing_it(hive):
    """Criterion 7, and the reason for it: this repo sets `major_version_zero`, so a `feat` bumps
    MINOR (0.11.5 → 0.12.0). A hand-rolled semver guess says 0.11.6, confidently and wrongly — so
    the number must come from the one place that owns the increment."""
    _bump(hive, version="0.11.5")
    _next_script(hive, "bump: version 0.11.5 → 0.12.0\nincrement detected: MINOR")

    out = _preview(hive, "--next").output

    assert "0.12.0" in out
    assert "0.11.6" not in out


@pytest.mark.parametrize(
    "script",
    [None, ("", 21), ("nothing to bump", 0)],
    ids=["no-script", "cz-refused", "no-bump-line"],
)
def test_next_degrades_to_could_not_determine_and_never_guesses(hive, script):
    """Criterion 8 — the same could-not-measure discipline the tag and artifact checks already
    keep. A preview that guessed the next version would be wrong exactly when it mattered, and a
    preview that refused would be a gate wearing a report's name."""
    _bump(hive, version="0.1.1")
    if script is not None:
        _next_script(hive, script[0], rc=script[1])

    res = _preview(hive, "--next")

    assert res.exit_code == 0, res.output
    assert "COULD NOT DETERMINE" in res.output
    assert "0.1.2" not in res.output and "0.2.0" not in res.output  # no invented number


def test_next_writes_no_version_anywhere(hive):
    """READ-ONLY is the property that lets this be run in front of a one-way door: it establishes
    nothing, pushes nothing, refuses nothing, and above all does not bump."""
    _bump(hive, version="0.1.1")
    _next_script(hive, "bump: version 0.1.1 → 0.2.0")
    pyproject = (hive["repo"] / "pyproject.toml").read_bytes()
    before = _git("ls-remote", "origin", cwd=hive["repo"])

    assert _preview(hive, "--next").exit_code == 0

    assert (hive["repo"] / "pyproject.toml").read_bytes() == pyproject
    assert not _ledger(hive).exists()
    assert _git("ls-remote", "origin", cwd=hive["repo"]) == before


def _already_shipped(hive, monkeypatch, *, next_out: str | None) -> str:
    """The state the operator was actually in: the pin's tag is on the remote AND its artifact is
    on PyPI, because that version was released."""
    (hive["repo"] / "pyproject.toml").write_text(
        '[project]\nname = "beadhive"\nversion = "0.11.5"\n'
    )
    _git("add", "-A", cwd=hive["repo"])
    _git("commit", "-qm", "bump: version 0.11.5", cwd=hive["repo"])
    _git("tag", "v0.11.5", cwd=hive["repo"])
    _git("push", "-q", "origin", "main", "v0.11.5", cwd=hive["repo"])
    monkeypatch.setattr(
        release,
        "_published_artifact",
        lambda *a, **k: (True, "✗ beadhive 0.11.5 IS ALREADY ON PyPI"),
    )
    if next_out is not None:
        _next_script(hive, next_out)
    return _preview(hive).output


def test_preview_leads_with_the_released_version_rather_than_two_failure_marks(hive, monkeypatch):
    """Criterion 6. "Tag on the remote" and "artifact on PyPI" both mean ALREADY SHIPPED here, and
    two ✗ marks read as "blocked" when they lead — the operator ran it twice, which is what a
    confusing report looks like from the outside. So: say which state this is, first."""
    out = _already_shipped(hive, monkeypatch, next_out="bump: version 0.11.5 → 0.12.0")

    assert "YOU ARE ON THE RELEASED VERSION v0.11.5" in out
    assert "just bump" in out and "0.12.0" in out
    assert out.index("RELEASED VERSION") < out.index("IS ALREADY ON origin")  # LEADS with it


def test_the_released_state_is_only_claimed_when_a_bump_is_actually_pending(hive, monkeypatch):
    """The same discipline again, one level up: "run `just bump` first" is a claim about pending
    commits, so an undeterminable next version must not produce it. The ✗ lines still print."""
    out = _already_shipped(hive, monkeypatch, next_out=None)

    assert "RELEASED VERSION" not in out
    assert "IS ALREADY ON origin" in out


# ─── the published-artifact check: the only one that needs the network ───────────────────────


def _urlopen(monkeypatch, raises):
    import urllib.request

    def fake(*a, **k):
        raise raises

    monkeypatch.setattr(urllib.request, "urlopen", fake)


@pytest.mark.parametrize(
    "boom",
    [
        TimeoutError("timed out"),
        OSError("[Errno -3] Temporary failure in name resolution"),
        __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "https://pypi.org", 503, "Service Unavailable", {}, None
        ),
    ],
    ids=["timeout", "dns", "5xx"],
)
def test_a_network_failure_is_could_not_check_and_never_a_refusal(monkeypatch, boom):
    """The one check here that can be wrong because a wifi blinked. A preview that turned a blip
    into "the path is blocked" would be a gate wearing a report's name, and an operator learns to
    ignore exactly that."""
    _urlopen(monkeypatch, boom)

    published, detail = release._published_artifact("beadhive", "9.9.9")

    assert published is None
    assert "COULD NOT CHECK" in detail


def test_a_404_is_the_one_negative_answer_pypi_actually_asserts(monkeypatch):
    """404 is measured, not inferred — so it is the only failure read as "not published"."""
    import urllib.error

    _urlopen(monkeypatch, urllib.error.HTTPError("https://pypi.org", 404, "Not Found", {}, None))

    published, detail = release._published_artifact("beadhive", "9.9.9")

    assert published is False
    assert "not on PyPI" in detail


def test_an_already_published_version_is_reported_as_spent(monkeypatch):
    import contextlib
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: contextlib.nullcontext())

    published, detail = release._published_artifact("beadhive", "0.11.5")

    assert published is True
    assert "IS ALREADY ON PyPI" in detail
