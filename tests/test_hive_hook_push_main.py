"""`bh hive hook push-main` (bh-ku9n9.5) — the pre-push gate as a named phase that LOOKS UP a
verdict, and falls back to the full gate on anything at all.

`docs/design/attested-green-adr.md`, "The pre-push gate is a named phase". The outermost gate was
the only point that could reuse nothing: it `exec`d `just check-all` from a shell script rather
than resolving a phase. It now resolves `work.validate.push-main` and asks the tree-keyed ledger
whether this exact tree already passed that exact command.

THE ENTIRE POINT OF THIS FILE IS THE ASYMMETRY, so every test below is really one assertion:

    exit 0  ⇔  a fresh GREEN verdict exists for THIS tree under THIS command
    exit ≠0 ⇔  everything else, and it means "run the full gate, exactly as before"

A miss, a stale entry, a red verdict, a malformed record, an unconfigured phase, a phase naming
a *different* command, no hive, an unresolvable rev, a corrupt config, an exploding config, no
`bh` on PATH at all — each has a test here, because a single one of them leaking an exit 0 would
push `main` ungated while looking gated. That is a strictly worse failure than the ~371s it
saves, so "no attestation ⇒ run it" is not a fallback, it is the contract.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config, host, prepush, validation_ledger
from beadhive.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _minted_host_identity():
    """`validation_ledger.record` stamps `host.host_id()` (bh-ytbb.4) and the shared
    `_sandbox_bh_home` fixture seeds only `config.yaml` — mint `host.yaml` as `bh config init`
    would, same as tests/test_worktree.py."""
    host.mint_if_needed()


ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = ROOT / "scripts" / "main-push-gate.sh"
GATE_CMD = "just check-all"  # what the hook runs on a miss — and so what a verdict must be for
ZERO = "0" * 40


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def hive(tmp_path, monkeypatch):
    """A real one-commit clone registered as a hive, with `push-main` wired to the gate command.

    Real git, not a fake: the lookup's identity half is `local_sha^{tree}` resolved in the hive's
    own clone, so a stubbed resolver would prove nothing about the thing under test."""
    repo = tmp_path / "ws" / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
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
    return {"cfg": cfg, "entry": entry, "repo": repo, "sha": _git("rev-parse", "HEAD", cwd=repo)}


def _lookup(hive, rev: str | None = None, gate: str = GATE_CMD):
    args = ["hive", "hook", "push-main", rev or hive["sha"], "--hive", "mr"]
    if gate:
        args += ["--gate", gate]
    return runner.invoke(app, args)


def _ledger(hive) -> Path:
    return hive["repo"] / ".git" / validation_ledger.LEDGER_FILENAME


def _attest(hive, rc: int = 0, rev: str | None = None, cmd: str = GATE_CMD) -> None:
    validation_ledger.record(hive["entry"], rev or hive["sha"], cmd, rc)


# ---- the ONE way to exit 0 ------------------------------------------------------------------


def test_a_fresh_green_verdict_for_this_tree_skips_the_gate(hive):
    """The hit path, and the only exit 0 that exists: the land-time run already exercised this
    tree under this command, so the push has nothing left to prove."""
    _attest(hive)

    res = _lookup(hive)

    assert res.exit_code == 0, res.output
    assert "attested green" in res.output
    assert _git("rev-parse", "HEAD^{tree}", cwd=hive["repo"])[:12] in res.output


def test_a_verdict_earned_at_a_different_commit_over_the_same_tree_still_hits(hive):
    """Why tree-keying is the whole win (ADR "The load-bearing choice"): a `--no-ff` land onto an
    unmoved main is a NEW sha over a byte-identical tree. Keyed on the sha this re-ran ~371s for
    content that had just passed."""
    repo = hive["repo"]
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "g.txt").write_text("change\n")
    _git("add", "g.txt", cwd=repo)
    _git("commit", "-qm", "feat: change", cwd=repo)
    tip = _git("rev-parse", "HEAD", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    _git("merge", "--no-ff", "--no-edit", "-q", "feature", cwd=repo)
    merge_sha = _git("rev-parse", "HEAD", cwd=repo)
    assert merge_sha != tip
    assert _git("rev-parse", f"{merge_sha}^{{tree}}", cwd=repo) == _git(
        "rev-parse", f"{tip}^{{tree}}", cwd=repo
    )

    _attest(hive, rev=tip)  # only the branch tip was ever validated

    assert _lookup(hive, rev=merge_sha).exit_code == 0  # …and it covers the merge being pushed


# ---- MISS: nothing recorded, or recorded for other content ----------------------------------


def test_no_verdict_at_all_runs_the_gate(hive):
    """The commonest case and the one that must never be optimistic: nothing has ever been
    recorded for this hive."""
    res = _lookup(hive)

    assert res.exit_code == 1
    assert "no fresh green push-main verdict" in res.output
    assert not _ledger(hive).exists()


def test_a_verdict_for_a_different_tree_runs_the_gate(hive):
    """main moved since the verdict was earned — a different tree, so the lookup misses with no
    hand-written invalidation rule anywhere."""
    _attest(hive)
    repo = hive["repo"]
    (repo / "f.txt").write_text("edited\n")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "feat: move main", cwd=repo)

    assert _lookup(hive, rev=_git("rev-parse", "HEAD", cwd=repo)).exit_code == 1


def test_an_unresolvable_rev_is_used_verbatim_and_misses(hive):
    """bh-ku9n9.3's fail-safe property, re-asserted at the gate that matters most: a rev git
    cannot resolve is keyed literally, so a resolve/no-resolve mismatch between writer and
    reader misses and revalidates rather than serving a verdict for content it never saw."""
    _attest(hive)

    assert _lookup(hive, rev="deadbeef" * 5).exit_code == 1


def test_an_empty_rev_runs_the_gate(hive):
    """A hook that fumbled its stdin parse and passed nothing must not be answered "green"."""
    _attest(hive)

    assert runner.invoke(app, ["hive", "hook", "push-main", "", "--hive", "mr"]).exit_code == 1


# ---- STALE ----------------------------------------------------------------------------------


def test_a_stale_verdict_runs_the_gate(hive):
    """Past `work.ledger_ttl` a verdict is not evidence any more (ADR Decision 3). Aged past the
    P1D default here; the TTL itself is unit-tested in test_worktree.py."""
    _attest(hive)
    entries = json.loads(_ledger(hive).read_text())
    for e in entries:
        e["at"] = time.time() - validation_ledger.LEDGER_TTL_SECONDS - 60
    _ledger(hive).write_text(json.dumps(entries))

    assert _lookup(hive).exit_code == 1


def test_a_verdict_stale_only_under_a_tightened_ttl_runs_the_gate(hive):
    """The operator-tuned-DOWN case the ADR expects to be the norm: fresh under P1D, stale under
    the hive's own PT30M. The gate must honour the hive's window, not the default."""
    _attest(hive)
    entries = json.loads(_ledger(hive).read_text())
    for e in entries:
        e["at"] = time.time() - 31 * 60
    _ledger(hive).write_text(json.dumps(entries))

    assert _lookup(hive).exit_code == 0  # 31 min is fresh under the P1D default…
    hive["entry"]["work"]["ledger_ttl"] = "PT30M"
    assert _lookup(hive).exit_code == 1  # …and stale under the hive's own window


# ---- INVALID / UNREADABLE RECORDS -----------------------------------------------------------


def test_a_red_verdict_runs_the_gate(hive):
    """A recorded FAILURE is a record, not an attestation. `rc != 0` can never short-circuit."""
    _attest(hive, rc=1)

    assert _lookup(hive).exit_code == 1


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
def test_a_corrupt_ledger_file_runs_the_gate(hive, name, content):
    """Every shape of unreadable ledger reads as an EMPTY ledger — never as a pass. A torn write
    landing on the one file that can skip an 11-minute gate must cost a gate run, nothing else."""
    _attest(hive)
    _ledger(hive).write_text(content)

    assert _lookup(hive).exit_code == 1, name


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
def test_a_malformed_entry_runs_the_gate(hive, name, mutate):
    """Field-by-field: a record missing or lying about ANY part of its identity, freshness or
    outcome is not an attestation. `at: 1e30` is the nastiest — it passes the freshness compare
    (a future stamp never "expires") and would then explode in the timestamp formatting, so it
    proves the verb neither passes nor RAISES out of a git hook."""
    _attest(hive)
    entries = json.loads(_ledger(hive).read_text())
    mutate(entries[0])
    _ledger(hive).write_text(json.dumps(entries))

    res = _lookup(hive)

    assert res.exit_code == 1, name
    assert "Traceback" not in res.output, name


# ---- ERROR: no hive, no clone, unusable config ----------------------------------------------


def test_outside_any_managed_hive_the_gate_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load", lambda *a, **k: {"managed_repos": []})
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["hive", "hook", "push-main", "abc123"]).exit_code == 1


def test_an_unknown_hive_id_runs_the_gate(hive):
    """`resolve_hive` exits rather than returning None; swallowed, because from a hook an
    unhandled exit is a BLOCKED push, not a degraded one."""
    res = runner.invoke(app, ["hive", "hook", "push-main", hive["sha"], "--hive", "nope"])

    assert res.exit_code == 1


def test_a_hive_dir_that_is_not_there_runs_the_gate(hive):
    """No `.git` dir → no ledger to read (a moved or deleted clone). Still just a gate run."""
    _attest(hive)
    hive["entry"]["repo"] = "vanished"

    assert _lookup(hive).exit_code == 1


def test_an_exploding_config_runs_the_gate(hive, monkeypatch):
    """The catch-all. Anything at all that raises inside the lookup — a broken config file, an
    OS error, a bug in bh — degrades to a gate run, never to a pass, and never to a traceback
    escaping into git's hook."""
    _attest(hive)

    def boom(*_a, **_kw):
        raise RuntimeError("config on fire")

    monkeypatch.setattr(config, "load", boom)

    res = _lookup(hive)

    assert res.exit_code == 1
    assert "verdict lookup failed" in res.output and "RuntimeError" in res.output


# ---- AMBIGUOUS: the phase itself ------------------------------------------------------------


def test_an_unconfigured_push_main_phase_runs_the_gate(hive):
    """Without an explicit `work.validate.push-main`, `validate_cmd` falls back to the fast
    default (`just check`) — and honouring a verdict earned by the FAST gate would let a push
    skip the FULL one. So unconfigured looks nothing up: exactly today's behaviour."""
    hive["entry"]["work"] = {}
    _attest(hive, cmd="just check")  # a green verdict for the fallback command exists…

    res = _lookup(hive)

    assert res.exit_code == 1  # …and is deliberately unreachable
    assert "no `work.validate.push-main` configured" in res.output


def test_a_phase_naming_a_different_command_than_the_hook_runs_the_gate(hive):
    """The ambiguous-attestation case, and the most dangerous one: `push-main` set to a WEAKER
    command than the hook actually runs. A verdict for `just check` says nothing about
    `just check-all`, so the mismatch must read as a loud miss, never a quiet pass."""
    hive["entry"]["work"]["validate"]["push-main"] = "just check"
    _attest(hive, cmd="just check")

    res = _lookup(hive)

    assert res.exit_code == 1
    assert "but this gate runs" in res.output


def test_a_verdict_for_another_command_at_the_same_tree_runs_the_gate(hive):
    """The cmd hash is half the key: same tree, different gate command, no reuse."""
    _attest(hive, cmd="just check")

    assert _lookup(hive).exit_code == 1


# ---- the hook file: one line, and it can only ever REMOVE work -------------------------------


def _run_hook(tmp_path: Path, *, bh: str | None, just_exit: int = 0, refs: str | None = None):
    """Run the real hook with stubbed `bh`/`just`, so the assertion is about WHETHER the full
    suite would run rather than about running it (that is ~371s). `bh=None` installs no `bh`
    at all — the "not on PATH" case."""
    binder = tmp_path / "bin"
    binder.mkdir(exist_ok=True)
    (binder / "just").write_text(f'#!/bin/sh\necho JUST-RAN "$@"\nexit {just_exit}\n')
    (binder / "just").chmod(0o755)
    if bh is not None:
        (binder / "bh").write_text(f'#!/bin/sh\necho BH-RAN "$@" >&2\n{bh}\n')
        (binder / "bh").chmod(0o755)
    return subprocess.run(
        [str(GATE_SCRIPT)],
        input=refs if refs is not None else f"refs/heads/main abc123 refs/heads/main {ZERO}\n",
        text=True,
        capture_output=True,
        env={"PATH": f"{binder}:/usr/bin:/bin"},
        check=False,
    )


def test_the_hook_asks_bh_for_a_verdict_and_skips_the_gate_on_a_hit(tmp_path):
    res = _run_hook(tmp_path, bh="exit 0")

    assert res.returncode == 0
    assert "hive hook push-main abc123 --gate just check-all" in res.stderr
    assert "JUST-RAN" not in res.stdout  # the whole point: the gate did not run


def test_the_hook_runs_the_full_gate_when_bh_reports_a_miss(tmp_path):
    res = _run_hook(tmp_path, bh="exit 1")

    assert res.returncode == 0
    assert "JUST-RAN check-all" in res.stdout


def test_a_bh_that_is_not_installed_runs_the_full_gate(tmp_path):
    """127. The lookup is an optimisation, so an absent `bh` must cost a gate run — the
    alternative (delegating the whole gate to the verb) would silently push main UNGATED on
    exactly the hosts least likely to notice."""
    res = _run_hook(tmp_path, bh=None)

    assert res.returncode == 0
    assert "JUST-RAN check-all" in res.stdout


def test_a_bh_that_crashes_runs_the_full_gate(tmp_path):
    res = _run_hook(tmp_path, bh="echo 'Traceback (most recent call last):' >&2; exit 70")

    assert res.returncode == 0
    assert "JUST-RAN check-all" in res.stdout


def test_a_red_gate_after_a_missed_lookup_still_fails_the_push(tmp_path):
    """The fallback is the WHOLE gate, not a decorative one: its exit code still governs."""
    res = _run_hook(tmp_path, bh="exit 1", just_exit=3)

    assert res.returncode == 3
    assert "gate FAILED" in res.stderr


def test_a_push_that_touches_no_integration_ref_never_consults_bh(tmp_path):
    """A bead-branch push costs neither a gate nor a lookup — the ref filter still comes first."""
    res = _run_hook(tmp_path, bh="exit 0", refs="refs/heads/wt/bead/issue/x abc refs/heads/x d\n")

    assert res.returncode == 0
    assert "BH-RAN" not in res.stderr and "JUST-RAN" not in res.stdout


def test_a_branch_deletion_never_consults_bh(tmp_path):
    """`git push origin :main` pushes no tree, so there is nothing to attest OR to gate."""
    res = _run_hook(tmp_path, bh="exit 0", refs=f"(delete) {ZERO} refs/heads/main abc\n")

    assert res.returncode == 0
    assert "BH-RAN" not in res.stderr


def test_the_hook_passes_the_sha_being_pushed_not_the_local_ref_name(tmp_path):
    """`git push HEAD:main` from a side branch: the tree to attest is the one named by the sha
    on git's stdin. Sending the ref name instead would resolve to the wrong tree — or to
    nothing, which at least misses safely, but is not what a hit must be built on."""
    res = _run_hook(tmp_path, bh="exit 1", refs="HEAD cafe1234 refs/heads/main deadbeef\n")

    assert "push-main cafe1234" in res.stderr


# ---- the two things this must not quietly become ---------------------------------------------


def test_the_hook_is_still_a_lefthook_job_and_never_a_hand_installed_hook():
    """Standing lefthook policy: lefthook is the SINGLE git-hook entrypoint, so anything wanting
    a git lifecycle point is a JOB here. A tool writing `.git/hooks/pre-push` directly evicts
    the fence and this gate silently — so the push-main lookup is reached THROUGH the existing
    job, and bh gained no installer for it."""
    from ruamel.yaml import YAML

    jobs = YAML(typ="safe").load((ROOT / "lefthook.yml").read_text())["pre-push"]["jobs"]
    assert [j["name"] for j in jobs] == ["bh-fence", "main-gate"]  # no third, self-installed hook
    assert prepush.hook_script("mr").count("exec ") == 1  # the fence shim, still logic-free
    assert "push-main" not in prepush.hook_script("mr")


def test_the_ssh_keepalive_caveat_is_recorded_where_a_reader_will_hit_it():
    """bh-ku9n9.5 asks for this explicitly. The lookup makes the ~371s path RARER, and the
    obvious misreading is that it made the bh-53o8f transport failure go away. It did not: a
    miss still holds an idle socket for the whole gate. Pinned in the hook a pusher reads, in
    the ADR, and in the operator docs."""
    for path in (GATE_SCRIPT, ROOT / "docs" / "design" / "attested-green-adr.md"):
        text = path.read_text()
        assert "bh-53o8f" in text and "keepalive" in text, path
        assert "371s" in text, path
    assert "keepalive" in (ROOT / "docs" / "WORK.md").read_text()
