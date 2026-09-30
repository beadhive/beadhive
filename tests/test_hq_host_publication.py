"""Registration publication against real local Git remotes, including a forced NFF race."""

from pathlib import Path

import pytest

from beadhive import host_provision, hosts, hq
from harness.world import git


def roster(directory: Path, host_id: str):
    return hosts.save(
        directory,
        hosts.HostManifest(
            host_id=host_id,
            label=host_id,
            os="linux",
            arch="x86_64",
            role="executor",
            identity=hosts.IdentityMechanism(kind="none"),
        ),
    )


def setup_remote(tmp_path):
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    local.mkdir()
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("init", "-q", "-b", "main", cwd=local)
    git("config", "user.name", "Fixture", cwd=local)
    git("config", "user.email", "fixture@example.org", cwd=local)
    git("config", "commit.gpgsign", "false", cwd=local)
    (local / "note").write_text("original")
    git("add", "note", cwd=local)
    git("commit", "-qm", "init", cwd=local)
    git("remote", "add", "origin", str(remote), cwd=local)
    git("push", "-qu", "origin", "main", cwd=local)
    return local, remote


def test_only_registration_published_and_rerun_noop(tmp_path):
    local, remote = setup_remote(tmp_path)
    (local / "note").write_text("unrelated local commit")
    git("commit", "-am", "unrelated", cwd=local)
    (local / "note").write_text("staged dirt")
    git("add", "note", cwd=local)
    (local / "note").write_text("unstaged dirt")
    (local / "untracked").write_text("untracked")
    roster(local, "host-a")
    status = git("status", "--porcelain", cwd=local).stdout
    index = git("show", ":note", cwd=local).stdout
    head = git("rev-parse", "HEAD", cwd=local).stdout
    assert hq.publish_host_manifest(local, "host-a")
    tip = git("rev-parse", "main", cwd=remote).stdout
    assert git("show", "main:note", cwd=remote).stdout == "original"
    assert "host-a" in git("show", "main:hosts/host-a.yaml", cwd=remote).stdout
    assert git("status", "--porcelain", cwd=local).stdout == status
    assert git("show", ":note", cwd=local).stdout == index
    assert git("rev-parse", "HEAD", cwd=local).stdout == head
    assert not hq.publish_host_manifest(local, "host-a")
    assert git("rev-parse", "main", cwd=remote).stdout == tip


def test_two_hosts_land_after_non_fast_forward_retry(tmp_path, monkeypatch):
    local, remote = setup_remote(tmp_path)
    other = tmp_path / "other"
    git("clone", "-q", str(remote), str(other), cwd=tmp_path)
    for key, value in (
        ("user.name", "Fixture"),
        ("user.email", "fixture@example.org"),
        ("commit.gpgsign", "false"),
    ):
        git("config", key, value, cwd=other)
    roster(local, "host-a")
    roster(other, "host-b")
    real_git = hq._git
    raced = False

    def race(args, cwd):
        nonlocal raced
        if args[:2] == ["push", "origin"] and not raced:
            raced = True
            assert hq.publish_host_manifest(other, "host-b")
        return real_git(args, cwd)

    monkeypatch.setattr(hq, "_git", race)
    assert hq.publish_host_manifest(local, "host-a")
    assert raced
    for host_id in ("host-a", "host-b"):
        assert host_id in git("show", f"main:hosts/{host_id}.yaml", cwd=remote).stdout


def test_prior_failure_and_dry_run_never_publish(monkeypatch):
    def unexpected(*args):
        pytest.fail("publication must not run")

    monkeypatch.setattr(hq, "publish_host_manifest", unexpected)
    assert host_provision._step_hq_publish(push=False, dry_run=False, prior=[]).status == "skipped"
    assert host_provision._step_hq_publish(push=True, dry_run=True, prior=[]).status == "would"
    prior = [host_provision.StepResult("verify", "failed")]
    assert (
        host_provision._step_hq_publish(push=True, dry_run=False, prior=prior).status == "skipped"
    )
