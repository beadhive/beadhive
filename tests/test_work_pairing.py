"""State/work pairing (condition 18, M14 — bh-cqvj6): rule P at the verb boundaries.

Every remote here is a local bare repo under ``tmp_path``; nothing is pushed anywhere real.
The bead store is the in-memory ``FakeBd`` from ``test_work``; its ``dolt push`` calls stand in
for "lifecycle state published to the remote".
"""

from __future__ import annotations

import json

import pytest
import typer

from beadhive import work, work_backup
from beadhive.run import run as real_run
from test_work import (
    _CLEAN_ENV,
    _CP,
    _batch_wt,
    _commit,
    _git,
    _minted_host_identity,
    _mol_branch,
    _wt,
    fakebd,
    hive,
)

__all__ = ["_minted_host_identity", "fakebd", "hive"]


def _remote_ref(remote, ref) -> str:
    res = real_run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=str(remote),
        check=False,
        capture=True,
        env=_CLEAN_ENV,
    )
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def _backup_refs(remote) -> list[str]:
    res = real_run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/bh/backup/"],
        cwd=str(remote),
        check=False,
        capture=True,
        env=_CLEAN_ENV,
    )
    return [line for line in (res.stdout or "").splitlines() if line]


def _published(fakebd) -> bool:
    """Whether any bead state was published (a managed `bd dolt push`)."""
    return fakebd.did("dolt", "push")


@pytest.fixture
def policy_rows(fakebd, monkeypatch):
    """Serve `bd config list/set/unset` from a dict — the hive's own policy rows."""
    rows: dict[str, str] = {}
    original = fakebd._dispatch

    def dispatch(actor, args):
        if args[:2] == ["config", "list"]:
            return _CP(0, json.dumps(rows), "")
        if args[:2] == ["config", "set"]:
            rows[args[2]] = args[3]
            return _CP(0, "", "")
        if args[:2] == ["config", "unset"]:
            rows.pop(args[2], None)
            return _CP(0, "", "")
        return original(actor, args)

    monkeypatch.setattr(fakebd, "_dispatch", dispatch)
    return rows


@pytest.fixture
def paired(policy_rows):
    policy_rows["bh.pairing.enabled"] = "true"
    # A scanner that always passes; the default (gitleaks) is not a test dependency.
    policy_rows["bh.pairing.secret_scan.command"] = "true {range}"
    return policy_rows


def _backup(bead, *, status=False, as_json=False):
    # Direct calls bypass Typer, so every option is passed explicitly.
    work.backup(
        bead=bead,
        status=status,
        as_json=as_json,
        from_hook=False,
        reap=False,
        dry_run=False,
        hive="myrepo",
    )


def _claimed_with_work(hive, fakebd, bead):
    fakebd.seed(bead, title="t")
    work.claim(bead=bead, as_="", hive="myrepo")
    _commit(_wt(hive, bead), "feat: the change")
    return _git("rev-parse", "HEAD", cwd=_wt(hive, bead)).stdout.strip()


# ---- off by default ---------------------------------------------------------------------------


def test_pairing_off_by_default_pushes_no_backup_and_records_no_frame(hive, fakebd, policy_rows):
    _claimed_with_work(hive, fakebd, "mr-off")
    work.submit(bead="mr-off", hive="myrepo")
    assert fakebd.states["mr-off"]["review"] == "pending"
    assert _backup_refs(hive.remote) == []
    assert "claim-frame" not in fakebd.states["mr-off"]
    assert not fakebd.did("set-state", "mr-off", "claim-frame")


# ---- rule P: the successful pair -------------------------------------------------------------


def test_paired_submit_lands_backup_before_state_and_both_reach_the_remote(
    hive, fakebd, paired, monkeypatch
):
    sha = _claimed_with_work(hive, fakebd, "mr-p")
    frame = work_backup.local_frame_id()
    ref = f"refs/bh/backup/mr-p/{frame}"
    assert fakebd.states["mr-p"]["claim-frame"] == frame  # D4: recorded at claim

    seen_at_gate = {}
    original_gate = work._open_submit_gate

    def gate(*args, **kwargs):
        # Rule P ordering: the backup is already on the remote when the first state is written.
        seen_at_gate["sha"] = _remote_ref(hive.remote, ref)
        seen_at_gate["published"] = _published(fakebd)
        return original_gate(*args, **kwargs)

    monkeypatch.setattr(work, "_open_submit_gate", gate)
    work.submit(bead="mr-p", hive="myrepo")

    assert seen_at_gate == {"sha": sha, "published": False}
    assert _remote_ref(hive.remote, ref) == sha  # the work is on the remote …
    assert fakebd.states["mr-p"]["review"] == "pending"  # … and so is the state
    assert _published(fakebd)
    # The CAS expectation mirror tracks what landed.
    assert _git("rev-parse", f"refs/bh/backup-pushed/mr-p/{frame}", cwd=hive.main).stdout.strip()


def test_paired_resubmit_after_refine_rewrites_own_backup_by_cas(hive, fakebd, paired):
    _claimed_with_work(hive, fakebd, "mr-rw")
    work.submit(bead="mr-rw", hive="myrepo")
    wt = _wt(hive, "mr-rw")
    _git("commit", "--amend", "-qm", "feat: the change, refined", cwd=wt)
    rewritten = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    work.submit(bead="mr-rw", hive="myrepo")
    ref = f"refs/bh/backup/mr-rw/{work_backup.local_frame_id()}"
    assert _remote_ref(hive.remote, ref) == rewritten


def test_paired_submit_to_a_configured_second_remote(hive, fakebd, paired, tmp_path):
    second = tmp_path / "backup.git"
    _git("init", "-q", "--bare", str(second), cwd=tmp_path)
    _git("remote", "add", "vault", str(second), cwd=hive.main)
    paired["bh.pairing.remote"] = "vault"
    sha = _claimed_with_work(hive, fakebd, "mr-vault")
    work.submit(bead="mr-vault", hive="myrepo")
    ref = f"refs/bh/backup/mr-vault/{work_backup.local_frame_id()}"
    assert _remote_ref(second, ref) == sha
    assert _backup_refs(hive.remote) == []


# ---- rule P: forced failures leave the remote state unchanged -------------------------------


def _assert_nothing_written(fakebd, bead):
    assert "review" not in fakebd.states.get(bead, {})
    assert not fakebd.did("gate", "create", "--blocks", bead)
    assert not _published(fakebd)


def test_unreachable_backup_remote_refuses_submit_with_nothing_written(
    hive, fakebd, paired, tmp_path, capsys
):
    _git("remote", "add", "gone", str(tmp_path / "missing.git"), cwd=hive.main)
    paired["bh.pairing.remote"] = "gone"
    _claimed_with_work(hive, fakebd, "mr-down")
    fakebd.calls.clear()
    with pytest.raises(typer.Exit) as exc:
        work.submit(bead="mr-down", hive="myrepo")
    assert exc.value.exit_code == 1
    _assert_nothing_written(fakebd, "mr-down")
    assert "backup push unreachable" in capsys.readouterr().err


def test_secret_scan_finding_refuses_submit_with_nothing_written(hive, fakebd, paired, capsys):
    paired["bh.pairing.secret_scan.command"] = "false {range}"
    _claimed_with_work(hive, fakebd, "mr-leak")
    fakebd.calls.clear()
    with pytest.raises(typer.Exit):
        work.submit(bead="mr-leak", hive="myrepo")
    _assert_nothing_written(fakebd, "mr-leak")
    assert _backup_refs(hive.remote) == []
    assert "backup push leaked" in capsys.readouterr().err


def test_missing_secret_scanner_refuses_unless_scan_disabled(hive, fakebd, paired, capsys):
    paired["bh.pairing.secret_scan.command"] = "bh-no-such-scanner-xyz {range}"
    _claimed_with_work(hive, fakebd, "mr-noscan")
    with pytest.raises(typer.Exit):
        work.submit(bead="mr-noscan", hive="myrepo")
    assert "not found" in capsys.readouterr().err
    paired["bh.pairing.secret_scan.enabled"] = "false"
    work.submit(bead="mr-noscan", hive="myrepo")
    assert fakebd.states["mr-noscan"]["review"] == "pending"


def test_zombie_cas_loss_refuses_and_leaves_the_foreign_ref_alone(hive, fakebd, paired, capsys):
    _claimed_with_work(hive, fakebd, "mr-z")
    ref = f"refs/bh/backup/mr-z/{work_backup.local_frame_id()}"
    # Another writer already put something on this frame's ref.
    foreign = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
    _git("push", "-q", "origin", f"{foreign}:{ref}", cwd=hive.main)
    fakebd.calls.clear()
    with pytest.raises(typer.Exit):
        work.submit(bead="mr-z", hive="myrepo")
    _assert_nothing_written(fakebd, "mr-z")
    assert _remote_ref(hive.remote, ref) == foreign
    assert "backup push refused" in capsys.readouterr().err


# ---- the recoverability query ---------------------------------------------------------------


def test_backup_status_reports_recoverable_for_the_claim_frame(hive, fakebd, paired, capsys):
    sha = _claimed_with_work(hive, fakebd, "mr-q")
    capsys.readouterr()
    _backup("mr-q", status=True, as_json=True)
    before = json.loads(capsys.readouterr().out)
    assert before["status"] == "unbacked"
    assert before["pairing_enabled"] is True

    _backup("mr-q")  # explicit checkpoint
    capsys.readouterr()
    _backup("mr-q", status=True, as_json=True)
    after = json.loads(capsys.readouterr().out)
    assert after["status"] == "recoverable"
    assert after["claim_frame"] == work_backup.local_frame_id()
    assert [r["sha"] for r in after["refs"]] == [sha]


def test_backup_checkpoint_is_a_noop_with_pairing_off(hive, fakebd, policy_rows, capsys):
    _claimed_with_work(hive, fakebd, "mr-cp")
    _backup("mr-cp")
    assert "pairing is off" in capsys.readouterr().out
    assert _backup_refs(hive.remote) == []


def test_abandon_marks_backed_up_work_resumable_and_clears_claim_frame(
    hive, fakebd, paired, monkeypatch
):
    sha = _claimed_with_work(hive, fakebd, "mr-ab")
    _backup("mr-ab")
    frame = work_backup.local_frame_id()
    fakebd.beads["mr-ab"]["labels"] = [f"claim-frame:{frame}"]
    monkeypatch.setenv("BH_DEV", "dev/default")
    work.abandon(bead="mr-ab", hive="myrepo", rm=False)
    assert fakebd.states["mr-ab"]["recovery"] == "resumable"
    assert fakebd.did("set-state", "mr-ab", "recovery=resumable")
    assert any(
        "label" in args and "remove" in args and f"claim-frame:{frame}" in args
        for _actor, args in fakebd.calls
    )
    assert any(sha in " ".join(args) for _actor, args in fakebd.calls if "recovery" in str(args))


# ---- batches and containers ------------------------------------------------------------------


def test_paired_group_submit_backs_up_every_member_then_container_merge_backs_up_the_epic(
    hive, fakebd, paired
):
    _mol_branch(hive, "mr-1")
    fakebd.seed("mr-1.1", title="a", parent="mr-1", labels=["batch:grp"])
    fakebd.seed("mr-1.2", title="b", parent="mr-1", labels=["batch:grp"])
    work.claim(bead="", as_="", group="mr-1.1,mr-1.2", hive="myrepo")
    frame = work_backup.local_frame_id()
    assert fakebd.states["mr-1.1"]["claim-frame"] == frame
    wt = _batch_wt(hive, "grp")
    _commit(wt, "feat: mr-1.1 work", fname="a.txt")
    _commit(wt, "feat: mr-1.2 work", fname="b.txt")
    tip = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()

    work.submit(bead="", group="mr-1.1,mr-1.2", hive="myrepo")
    for member in ("mr-1.1", "mr-1.2"):
        assert _remote_ref(hive.remote, f"refs/bh/backup/{member}/{frame}") == tip

    fakebd.resolve_review("mr-1.1")
    work.merge(bead="", group="mr-1.1,mr-1.2", hive="myrepo")
    container = _git("rev-parse", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip()
    # The members' work now lives in the container, which is backed up before they close.
    assert _remote_ref(hive.remote, f"refs/bh/backup/mr-1/{frame}") == container
    assert fakebd.beads["mr-1.1"]["status"] == "closed"


def test_container_merge_with_unreachable_backup_leaves_members_open(
    hive, fakebd, paired, tmp_path, capsys
):
    _mol_branch(hive, "mr-2")
    fakebd.seed("mr-2.1", title="a", parent="mr-2", labels=["batch:g2"])
    work.claim(bead="", as_="", group="mr-2.1", hive="myrepo")
    _commit(_batch_wt(hive, "g2"), "feat: mr-2.1 work", fname="a.txt")
    work.submit(bead="", group="mr-2.1", hive="myrepo")
    fakebd.resolve_review("mr-2.1")
    _git("remote", "add", "gone", str(tmp_path / "missing.git"), cwd=hive.main)
    paired["bh.pairing.remote"] = "gone"
    with pytest.raises(typer.Exit):
        work.merge(bead="", group="mr-2.1", hive="myrepo")
    assert fakebd.beads["mr-2.1"]["status"] != "closed"
    assert "no member was closed" in capsys.readouterr().err
