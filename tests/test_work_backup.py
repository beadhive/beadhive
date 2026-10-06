"""Backup refs, recoverability and retention for state/work pairing (M14 D1/D3/D7, bh-cqvj6).

Real git against scratch bare remotes under ``tmp_path`` only — nothing leaves the test dir.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess

import pytest

from beadhive import work_backup as wb
from beadhive import work_pairing_policy as wpp

_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
NOW = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)


def git(*args, cwd) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_ENV, capture_output=True, text=True, check=True
    )
    return res.stdout.strip()


def commit(repo, name: str) -> str:
    (repo / name).write_text(name)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", f"feat: {name}", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    clone = tmp_path / "clone"
    clone.mkdir()
    git("init", "-q", "-b", "main", cwd=clone)
    git("config", "user.email", "t@example.com", cwd=clone)
    git("config", "user.name", "t", cwd=clone)
    git("remote", "add", "origin", str(remote), cwd=clone)
    commit(clone, "base")
    git("push", "-q", "origin", "main", cwd=clone)
    git("fetch", "-q", "origin", cwd=clone)
    return clone, remote


POLICY = wpp.parse({"bh.pairing.enabled": "true", "bh.pairing.secret_scan.command": "true {range}"})


def push(clone, bead, frame, sha, **kw):
    return wb.push_backup(clone, "origin", bead, frame, sha, policy=POLICY, **kw)


# ---- policy ----------------------------------------------------------------------------------


def test_policy_defaults_are_all_off_and_configurable():
    policy = wpp.parse({})
    assert policy == wpp.DEFAULT
    assert not policy.enabled
    assert policy.checkpoint_interval_seconds == 0  # sub-keys inert under the master switch
    assert not policy.checkpoint_on_commit
    assert policy.signature_policy == "off"
    assert policy.reclaim_mode("failover") == "off"
    assert policy.retention_days("orphan") is None  # 0 = never
    assert policy.retention_days("superseded") == 14
    assert policy.retention_days("unlanded") == 30
    assert wpp.KEYS["pairing.secret_scan.command"].default.startswith("gitleaks git")


def test_policy_refuses_bad_values_never_clamps_and_reads_them_as_off():
    policy = wpp.parse(
        {
            "bh.pairing.enabled": "maybe",
            "bh.pairing.checkpoint.interval_seconds": "-5",
            "bh.pairing.retention.unlanded_days": "soon",
            "bh.pairing.remote": "upstream",
            "bh.pairing.bogus": "1",
            "bh.other.namespace": "ignored",
            "issue_prefix": "bh",
        }
    )
    assert not policy.enabled
    assert policy.get("pairing.checkpoint.interval_seconds") == 0
    assert policy.retention_days("unlanded") is None  # a typo never means "delete now"
    assert policy.remote == ""
    refused = {r.key for r in policy.refused}
    assert refused == {
        "pairing.enabled",
        "pairing.checkpoint.interval_seconds",
        "pairing.retention.unlanded_days",
        "pairing.remote",
        "pairing.bogus",
    }
    rows = {row["key"]: row for row in wpp.rows(policy)}
    assert rows["pairing.enabled"]["source"] == "refused"
    assert rows["pairing.bogus"]["refused"]["reason"] == "unknown key"


def test_policy_write_validation_and_reclaim_apply_needs_pairing():
    assert wpp.validate("pairing.enabled", "ON") == "true"
    assert wpp.validate("reclaim.sweep.mode", "Apply") == "apply"
    with pytest.raises(wpp.PolicyError):
        wpp.validate("pairing.secret_scan.command", "gitleaks git")  # no {range}
    with pytest.raises(wpp.PolicyError):
        wpp.validate("pairing.nope", "1")
    assert wpp.parse({"bh.reclaim.failover.mode": "apply"}).reclaim_mode("failover") == "report"
    both = wpp.parse({"bh.reclaim.failover.mode": "apply", "bh.pairing.enabled": "true"})
    assert both.reclaim_mode("failover") == "apply"


# ---- naming ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("factory-1", "factory-1"),
        ("xeno-mac.lan", "xeno-mac.lan"),
        ("host/with space", "host-with-space"),
        (".hidden", "-hidden"),
        ("a..b", "a-b"),
        ("frame.lock", "frame-lock"),
    ],
)
def test_frame_segment_is_always_a_valid_ref_component(raw, safe, repo):
    clone, _ = repo
    assert wb.segment(raw) == safe
    ref = wb.backup_ref("bh-x", raw)
    assert subprocess.run(["git", "check-ref-format", ref], env=_ENV).returncode == 0


# ---- push: CAS single writer -----------------------------------------------------------------


def test_push_creates_fast_forwards_and_rewrites_by_cas(repo):
    clone, remote = repo
    a = commit(clone, "a")
    out = push(clone, "bh-x", "fA", a)
    assert out.ok and out.status == wb.PUSHED
    assert git("rev-parse", "refs/bh/backup/bh-x/fA", cwd=remote) == a
    assert wb.last_pushed(clone, "bh-x", "fA") == a
    b = commit(clone, "b")
    assert push(clone, "bh-x", "fA", b).ok
    git("commit", "--amend", "-qm", "feat: b refined", cwd=clone)
    b2 = git("rev-parse", "HEAD", cwd=clone)
    assert push(clone, "bh-x", "fA", b2).ok  # refine rewrite with the right lease
    assert git("rev-parse", "refs/bh/backup/bh-x/fA", cwd=remote) == b2
    assert push(clone, "bh-x", "fA", b2).ok  # idempotent re-run at the same sha


def test_stale_lease_is_refused_never_retried(repo):
    clone, remote = repo
    a = commit(clone, "a")
    assert push(clone, "bh-x", "fA", a).ok
    b = commit(clone, "b")
    out = push(clone, "bh-x", "fA", b, expected="0" * 40)
    assert out.status == wb.REFUSED
    assert "stale info" in out.detail or "rejected" in out.detail
    assert git("rev-parse", "refs/bh/backup/bh-x/fA", cwd=remote) == a


def test_upstream_and_unreachable_remotes(repo, tmp_path):
    clone, _ = repo
    a = commit(clone, "a")
    assert wb.push_backup(clone, "upstream", "bh-x", "fA", a, policy=POLICY).status == wb.REFUSED
    git("remote", "add", "gone", str(tmp_path / "nowhere.git"), cwd=clone)
    out = wb.push_backup(clone, "gone", "bh-x", "fA", a, policy=POLICY)
    assert out.status == wb.UNREACHABLE
    assert wb.last_pushed(clone, "bh-x", "fA") == ""


def test_secret_scan_runs_over_only_the_new_commits(repo, tmp_path):
    clone, _ = repo
    log = tmp_path / "scan.log"
    policy = wpp.parse(
        {
            "bh.pairing.enabled": "true",
            "bh.pairing.secret_scan.command": f"sh -c 'echo \"$0\" >> {log}' {{range}}",
        }
    )
    a = commit(clone, "a")
    assert wb.push_backup(clone, "origin", "bh-x", "fA", a, policy=policy).ok
    b = commit(clone, "b")
    assert wb.push_backup(clone, "origin", "bh-x", "fA", b, policy=policy).ok
    base = git("rev-parse", "origin/main", cwd=clone)
    assert log.read_text().splitlines() == [f"{base}..{a}", f"{a}..{b}"]
    failing = wpp.parse(
        {"bh.pairing.enabled": "true", "bh.pairing.secret_scan.command": "false {range}"}
    )
    c = commit(clone, "c")
    out = wb.push_backup(clone, "origin", "bh-x", "fA", c, policy=failing)
    assert out.status == wb.LEAKED


def test_pair_is_a_noop_when_pairing_is_off(repo):
    clone, remote = repo
    a = commit(clone, "a")
    assert (
        wb.pair(
            cfg={},
            entry={},
            main=clone,
            beads=["bh-x"],
            sha=a,
            verb="submitted",
            policy=wpp.DEFAULT,
        )
        == []
    )
    assert git("for-each-ref", "refs/bh/", cwd=remote) == ""


def test_container_of_names_only_epic_container_branches():
    assert wb.container_of("wt/bead/epic/bh-16347") == "bh-16347"
    assert wb.container_of("main") == ""
    assert wb.container_of("wt/bead/issue/bh-x") == ""


# ---- recoverability (D3) ---------------------------------------------------------------------


def test_recoverability_table(repo, tmp_path):
    clone, remote = repo
    check = lambda frame, **kw: wb.recoverability(clone, "origin", "bh-x", frame, **kw)  # noqa: E731
    assert check("fA").status == wb.UNBACKED  # no ref at all
    a = commit(clone, "a")
    assert push(clone, "bh-x", "fA", a).ok
    got = check("fA")
    assert got.status == wb.RECOVERABLE
    assert [(r.frame, r.sha) for r in got.refs] == [("fA", a)]
    assert check("").status == wb.UNBACKED  # unattributed claim
    assert check("fB").status == wb.UNBACKED  # another frame's ref is orphan work, listed
    assert [r.frame for r in check("fB").refs] == ["fA"]
    # Strict signatures: unsigned work is suspect, never recoverable.
    assert check("fA", signature_policy="strict").status == wb.SUSPECT
    # Once the work lands in the remote base, the backup carries nothing new.
    git("push", "-q", "origin", f"{a}:refs/heads/main", cwd=clone)
    assert check("fA").status == wb.UNBACKED
    # A failed fetch is unknown — never a licence to rewind.
    git("remote", "add", "gone", str(tmp_path / "nowhere.git"), cwd=clone)
    assert wb.recoverability(clone, "gone", "bh-x", "fA").status == wb.UNKNOWN
    # A deleted remote ref is pruned from the local view.
    assert wb.delete_backup(clone, "origin", "refs/bh/backup/bh-x/fA", a)
    assert check("fA").refs == ()
    assert remote.exists()


# ---- retention (D7) --------------------------------------------------------------------------


def _bead(status="open", **fields):
    return {"status": status, "assignee": "", **fields}


def test_retention_verdicts():
    days = dt.timedelta(days=1)
    verdict = lambda data, frame="fA", covered=False: wb.retention_verdict(  # noqa: E731
        data, frame, covered=covered, policy=POLICY, now=NOW
    )[0]
    assert verdict(None, covered=True)
    assert not verdict(None)
    assert not verdict(_bead("in_progress", assignee="dev/a"))
    assert not verdict(_bead(labels=["recovery:resumable"]))
    assert not verdict(_bead(updated_at="2020-01-01T00:00:00Z"))  # orphan: never by default
    landed_old = _bead("closed", close_reason="merged", closed_at=(NOW - 15 * days).isoformat())
    assert verdict(landed_old, frame="fOther")  # superseded past 14d
    assert not verdict({**landed_old, "labels": ["claim-frame:fA"]})  # holder awaits coverage
    landed_new = {**landed_old, "closed_at": (NOW - 2 * days).isoformat()}
    assert not verdict(landed_new, frame="fOther")
    unlanded = _bead("closed", close_reason="wontfix", closed_at=(NOW - 31 * days).isoformat())
    assert verdict(unlanded)
    assert not verdict({**unlanded, "closed_at": (NOW - 29 * days).isoformat()})


def test_reap_deletes_covered_and_expired_keeps_the_rest(repo):
    clone, remote = repo
    covered = commit(clone, "covered")
    assert push(clone, "bh-landed", "fA", covered).ok
    git("push", "-q", "origin", f"{covered}:refs/heads/main", cwd=clone)
    live = commit(clone, "live")
    assert push(clone, "bh-live", "fA", live).ok
    dead = commit(clone, "dead")
    assert push(clone, "bh-dead", "fA", dead).ok
    beads = {
        "bh-live": _bead("in_progress", assignee="dev/a"),
        "bh-dead": _bead("closed", close_reason="rejected", closed_at="2026-01-01T00:00:00Z"),
    }
    seen = []

    def show(bead):
        seen.append(bead)
        return beads.get(bead)

    dry = wb.reap(clone, "origin", policy=POLICY, show=show, now=NOW, dry_run=True)
    assert sorted(dry.deleted) == ["refs/bh/backup/bh-dead/fA", "refs/bh/backup/bh-landed/fA"]
    assert len(git("for-each-ref", "refs/bh/backup/", cwd=remote).splitlines()) == 3

    seen.clear()
    scoped = wb.reap(clone, "origin", policy=POLICY, show=show, now=NOW, judge=["bh-live"])
    assert scoped.deleted == ["refs/bh/backup/bh-landed/fA"]  # covered refs always go
    assert "bh-dead" not in seen  # graces judged only for the landed beads
    report = wb.reap(clone, "origin", policy=POLICY, show=show, now=NOW)
    assert report.deleted == ["refs/bh/backup/bh-dead/fA"]
    assert git("for-each-ref", "--format=%(refname)", "refs/bh/backup/", cwd=remote) == (
        "refs/bh/backup/bh-live/fA"
    )
