"""bh-ijd4 — `work.enforce_signing`: the merge gate that checks EVERY commit, not just the tip.

Before this, grepping the whole merge path (worktree_merge / work_logic / prepush / guard) found
exactly one signing reference — `worktree_merge.py` SETTING `commit.gpgsign` on the merge commit
it creates — and nothing verifying a signature anywhere. A branch of unsigned agent commits
merged to main with no check at all.

The `%G?` semantics asserted here were MEASURED (git 2.54), not assumed, because one of them is
counter-intuitive and load-bearing: with no usable `gpg.ssh.allowedSignersFile`, git reports a
perfectly signed commit as `N` (key unset) or `U` (set but the file is missing) — never `G`. That
is why the gate defaults off and why `allowed_signers` had to be given a home rather than left as
a "later" problem.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from beadhive import config, work_logic, worktree

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=_CLEAN_ENV
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real repo on `main` with a real SSH signing key, wired through repo-LOCAL git config.

    Local (not global) on purpose: `worktree._run_git` scrubs every `GIT_*` variable so an
    explicit `-C` always wins, which also scrubs `GIT_CONFIG_GLOBAL` — the repo's own config is
    the only isolation that survives the seam under test."""
    work = tmp_path / "repo"
    work.mkdir()
    key = tmp_path / "signer"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "dev@example.com", "-f", str(key), "-q"],
        check=True,
    )
    signers = tmp_path / "allowed_signers"
    signers.write_text(f"dev@example.com {(key.with_suffix('.pub')).read_text().strip()}\n")

    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.name", "Dev", cwd=work)
    _git("config", "user.email", "dev@example.com", cwd=work)
    _git("config", "gpg.format", "ssh", cwd=work)
    _git("config", "user.signingkey", str(key.with_suffix(".pub")), cwd=work)
    _git("config", "gpg.ssh.allowedsignersfile", str(signers), cwd=work)
    _git("commit", "-q", "--allow-empty", "-m", "chore: base", cwd=work)

    monkeypatch.setattr(worktree.registry, "hive_dir", lambda _e: work)
    return work, signers


def _commit(work: Path, subject: str, *, sign: bool) -> None:
    args = ["commit", "-q", "--allow-empty", "-m", subject]
    _git(*args, "-S" if sign else "--no-gpg-sign", cwd=work)


# ---- what git actually reports (measured, not assumed) -------------------------


def test_signature_status_reports_every_commit_in_the_range(repo):
    work, _signers = repo
    _git("branch", "feature", cwd=work)
    _git("checkout", "-q", "feature", cwd=work)
    _commit(work, "feat: signed one", sign=True)
    _commit(work, "feat: unsigned two", sign=False)
    _commit(work, "feat: signed three", sign=True)

    rows = worktree.signature_status({}, "feature", "main")

    # THE WHOLE RANGE, not just the tip — the operator was explicit: "all of them if enabled".
    assert [(status, subject) for _sha, status, subject in rows] == [
        ("G", "feat: signed three"),
        ("N", "feat: unsigned two"),
        ("G", "feat: signed one"),
    ]


def test_a_signed_commit_is_unverifiable_without_a_trust_anchor(repo):
    """The measurement that shaped the whole design (git 2.54). A trust anchor that is
    CONFIGURED but points at a missing file — the origin Mac's actual measured state — turns a
    perfectly signed commit into `U`, not `G`.

    The sibling case, `gpg.ssh.allowedSignersFile` never configured at all, yields `N`: byte
    identical to an unsigned commit. It is measured out of band rather than asserted here
    because `worktree._run_git` deliberately scrubs every `GIT_*` variable (so an explicit `-C`
    always wins), which means git falls through to the real user `~/.gitconfig` — whatever the
    machine running the suite happens to carry — the moment the repo-local key is removed.

    Either way a signed commit does not read as `G`, so a gate turned on for a fleet with no
    working `allowed_signers` would refuse every merge. That is why the flag defaults to false,
    and why the file had to be given a home rather than deferred."""
    work, signers = repo
    _git("branch", "feature", cwd=work)
    _git("checkout", "-q", "feature", cwd=work)
    _commit(work, "feat: signed", sign=True)

    signers.unlink()  # configured, file gone — the origin Mac's measured state

    assert [s for _sha, s, _sub in worktree.signature_status({}, "feature", "main")] == ["U"]


def test_a_key_absent_from_allowed_signers_reads_as_U(repo):
    """Signed, but by a key nobody trusts. `U` is not `G` and is refused — checking only that a
    signature EXISTS would wave this straight through, which is the presence-only verification
    this bead refuses to ship."""
    work, signers = repo
    _git("branch", "feature", cwd=work)
    _git("checkout", "-q", "feature", cwd=work)
    _commit(work, "feat: signed by a stranger", sign=True)
    signers.write_text("# nobody is trusted\n")

    assert [s for _sha, s, _sub in worktree.signature_status({}, "feature", "main")] == ["U"]


# ---- the gate's verdict ---------------------------------------------------------


def test_all_trusted_passes():
    rows = [("aaa1111", "G", "feat: one"), ("bbb2222", "G", "fix: two")]

    ok, msg = work_logic._signing_ok(rows, "wt/bead/issue/x-1", "main")

    assert ok
    assert msg == ""


def test_offenders_are_named_individually():
    rows = [
        ("aaa1111", "G", "feat: fine"),
        ("bbb2222", "N", "feat: unsigned"),
        ("ccc3333", "U", "feat: untrusted key"),
    ]

    ok, msg = work_logic._signing_ok(rows, "wt/bead/issue/x-1", "main")

    assert not ok
    assert "bbb2222" in msg and "ccc3333" in msg
    assert "aaa1111" not in msg  # only the offenders, so the list stays actionable
    assert "2 of 3" in msg


def test_unsigned_and_untrusted_are_not_collapsed_into_one_diagnosis():
    """ "You never signed this" and "you signed it with a key nobody trusts yet" have completely
    different fixes; flattening both to "unsigned" sends the operator the wrong way."""
    rows = [("bbb2222", "N", "a"), ("ccc3333", "U", "b")]

    _ok, msg = work_logic._signing_ok(rows, "b", "main")

    assert "no signature" in msg
    assert "not in allowed_signers" in msg


def test_a_bad_signature_is_refused_like_an_absent_one():
    ok, msg = work_logic._signing_ok([("bbb2222", "B", "feat: tampered")], "b", "main")

    assert not ok
    assert "BAD signature" in msg


def test_an_unreadable_range_refuses_rather_than_assuming_signed():
    """Fail closed: `signature_status` returns [] when the range can't be computed, and a gate
    that read that as "no offenders" would merge unverified work on a git hiccup."""
    ok, msg = work_logic._signing_ok([], "wt/bead/issue/x-1", "main")

    assert not ok
    assert "refusing to merge" in msg


def test_the_refusal_states_the_no_grandfathering_decision_and_both_ways_out():
    """The bead asked for an explicit answer on commits that predate the flag. It is: refused,
    like any other — a commit date is trivially rewritable, so a date-scoped exemption would let
    exactly the unsigned commits this gate exists to stop through, unauditably. The message has
    to say so, and name both escapes, or it reads like a bug."""
    ok, msg = work_logic._signing_ok([("bbb2222", "N", "feat: from last year")], "b", "main")

    assert not ok
    assert "no grandfathering" in msg
    assert "rebase" in msg  # re-sign
    assert "work.enforce_signing: false" in msg  # or turn it off
    assert "bh host identity" in msg  # or your allowed_signers was never wired up


# ---- the flag itself -------------------------------------------------------------


def test_enforce_signing_defaults_to_false():
    """Default off, load-bearingly: turning it on for an unprepared fleet blocks every merge."""
    assert config.enforce_signing({}, None) is False


def test_enforce_signing_is_settable_globally_and_per_hive():
    cfg = {"work": {"enforce_signing": True}}

    assert config.enforce_signing(cfg, None) is True
    assert config.enforce_signing(cfg, {"work": {"enforce_signing": False}}) is False


def test_enforce_signing_is_fleet_classified():
    """A signing policy the whole fleet must agree on — two hosts disagreeing about whether
    signatures are required would mean one of them merging what the other refuses."""
    from beadhive import config_partition

    assert config_partition.partition_of("work.enforce_signing") == config_partition.FLEET


# ---- the guard, in the merge path ------------------------------------------------


def test_the_gate_is_a_no_op_when_off(monkeypatch):
    """Behaviour with the flag off must be byte-identical to before this bead — including not
    paying for the `git log` at all."""
    from beadhive import work

    def _never(*_a, **_k):
        raise AssertionError("must not read signatures when the flag is off")

    monkeypatch.setattr(work.worktree, "signature_status", _never)

    work._guard_signed_history({}, "wt/bead/issue/x-1", "main", {})  # no raise


def test_the_gate_refuses_the_merge_when_on(monkeypatch, capsys):
    import typer

    from beadhive import work

    monkeypatch.setattr(
        work.worktree, "signature_status", lambda *_a: [("bbb2222", "N", "feat: unsigned")]
    )

    with pytest.raises(typer.Exit):
        work._guard_signed_history(
            {}, "wt/bead/issue/x-1", "main", {"work": {"enforce_signing": True}}
        )

    err = capsys.readouterr().err
    assert "bbb2222" in err  # named, not just refused
    assert "feat: unsigned" in err


def test_every_merge_path_consults_the_gate():
    """`merge` (single bead), `finish` (molecule land) and `merge --group` (batch bubble) all
    put the same commits on the integration branch, so a gate wired into only one of them is a
    gate with two documented bypasses."""
    import inspect

    from beadhive import work, work_group

    assert "_guard_signed_history" in inspect.getsource(work._merge_bead)
    assert "_guard_signed_history" in inspect.getsource(work._merge_molecule)
    assert "_signing_ok" in inspect.getsource(work_group.merge_group)
