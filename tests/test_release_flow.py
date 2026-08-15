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

3.  **THE MEASURED BRANCH.** `recover` decides between bh-67utw's two cases on whether the TAG
    REACHED THE REMOTE, read with `ls-remote` against the actual remote. Never assumed, never
    from a local tracking ref, and "I could not look" is its own exit code rather than being
    folded into "it is not there".

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

from beadhive import config, host, prepush, release, validation_ledger
from beadhive.cli import app

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


def _ledger(hive) -> Path:
    return hive["repo"] / ".git" / validation_ledger.LEDGER_FILENAME


def _marker(hive) -> Path:
    return hive["repo"] / ".git" / release.BUMP_GATE_FILENAME


def _attest(hive, rc: int = 0, rev: str | None = None, cmd: str = GATE_CMD) -> None:
    validation_ledger.record(hive["entry"], rev or hive["sha"], cmd, rc)


def _fire(hive, rev: str | None = None, pid: int | None = None, cmd: str = GATE_CMD) -> None:
    """A bump-gate marker as `attest --background` writes one, without spawning a real gate."""
    rev = rev or hive["sha"]
    _marker(hive).write_text(
        json.dumps(
            {
                "tree": validation_ledger.tree_of(hive["entry"], rev),
                "cmd": cmd,
                "sha": rev,
                "pid": pid if pid is not None else os.getpid(),  # this process — reliably alive
                "host": host.host_id(),
                "log": str(_marker(hive).with_name(release.BUMP_GATE_LOG)),
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
    entries = json.loads(_ledger(hive).read_text())
    for e in entries:
        e["at"] = time.time() - validation_ledger.LEDGER_TTL_SECONDS - 60
    _ledger(hive).write_text(json.dumps(entries))

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
        ("no cmd_hash", lambda e: e.pop("cmd_hash")),
    ],
)
def test_a_malformed_entry_refuses_the_bump(hive, name, mutate):
    """Field by field: a record missing or lying about any part of its identity, freshness or
    outcome is not an attestation. `at: 1e30` is the nastiest — it survives the freshness compare
    (a future stamp never expires) and would then explode in the timestamp formatting, so this
    proves the verb neither passes NOR raises."""
    _attest(hive)
    entries = json.loads(_ledger(hive).read_text())
    mutate(entries[0])
    _ledger(hive).write_text(json.dumps(entries))

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


def test_await_refuses_a_stale_verdict_even_with_the_marker_present(hive):
    """The marker says a run happened; the ledger says its verdict has expired. The ledger wins —
    the marker is never evidence about greenness."""
    bumped = _bump(hive)
    dead = subprocess.Popen(["true"])
    dead.wait()
    _fire(hive, rev=bumped, pid=dead.pid)
    _attest(hive, rev=bumped)
    entries = json.loads(_ledger(hive).read_text())
    for e in entries:
        e["at"] = time.time() - validation_ledger.LEDGER_TTL_SECONDS - 60
    _ledger(hive).write_text(json.dumps(entries))

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
    _marker(hive).write_text(content)

    assert _await(hive, rev=bumped).exit_code == 1, name


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
    entries = json.loads(_ledger(hive).read_text())
    for e in entries:
        e["at"] = time.time() - validation_ledger.LEDGER_TTL_SECONDS - 60
    _ledger(hive).write_text(json.dumps(entries))

    assert validation_ledger.verdict(hive["entry"], hive["sha"], GATE_CMD) is None


def test_a_string_rc_is_not_green(hive):
    """`!= 0` rather than truthiness, so a corrupt record cannot read as a pass — asserted at the
    function rather than only through the CLI, since both callers depend on it."""
    _attest(hive)
    entries = json.loads(_ledger(hive).read_text())
    entries[0]["rc"] = "0"
    _ledger(hive).write_text(json.dumps(entries))

    assert validation_ledger.verdict(hive["entry"], hive["sha"], GATE_CMD) is not None
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
