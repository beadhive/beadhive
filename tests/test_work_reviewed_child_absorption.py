"""Real-Git regression for a reviewed child importing main into an old epic spine."""

from __future__ import annotations

import json

import pytest
import typer

from beadhive import config, registry, work_logic, worktree
from test_work import (
    _commit,
    _git,
    _minted_host_identity,
    _start_and_land_children,
    _wt_of,
    fakebd,
    hive,
    work,
)
from test_work_top_level_composition import _real_ssh_signing_identity

__all__ = ["_minted_host_identity", "_real_ssh_signing_identity", "fakebd", "hive"]


def _absorbed_main(hive, fakebd, *, epic="mr-absorbed", unsigned_child=False):
    seat = _start_and_land_children(hive, fakebd, epic=epic, count=0)
    child = f"{epic}.1"
    fakebd.seed(child, title="reviewed change", parent=epic)
    work.claim(bead=child, as_="dev/child", hive="myrepo")
    child_seat = _wt_of(hive, child)
    old = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
    _commit(hive.main, "feat: independently advance main", fname="advanced-main.txt")
    main = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
    _git("rebase", "main", cwd=child_seat)
    if unsigned_child:
        _git("config", "--worktree", "commit.gpgsign", "false", cwd=child_seat)
    _commit(child_seat, "feat: reviewed child", fname="child-change.txt")
    tip = _git("rev-parse", "HEAD", cwd=child_seat).stdout.strip()
    if unsigned_child:
        assert _git("show", "-s", "--format=%G?", tip, cwd=child_seat).stdout.strip() == "N"
    work.submit(bead=child, as_="dev/child", hive="myrepo")
    fakebd.resolve_review(child)
    work.merge(bead=child, hive="myrepo", rm=False, molecule=False)
    branch = f"wt/bead/epic/{epic}"
    bubble = _git("rev-parse", branch, cwd=hive.main).stdout.strip()
    assert _git("show", "-s", "--format=%P", bubble, cwd=hive.main).stdout.split() == [old, tip]
    return seat, child, branch, old, main, tip, bubble


def _policy(hive, epic, branch):
    entry = registry.resolve_hive(config.load(), "myrepo")
    return work_logic.epic_history_policy(entry, hive.main, epic, branch, "main", 10, "main")


def test_finish_accepts_exact_reviewed_child_absorption_without_rewriting_history(hive, fakebd):
    seat, child, branch, old, main, tip, bubble = _absorbed_main(hive, fakebd)
    entry = registry.resolve_hive(config.load(), "myrepo")
    before = _git("rev-parse", "HEAD", "HEAD^{tree}", cwd=seat).stdout
    worktree.refresh_container(entry, branch, "main")
    assert _git("rev-parse", "HEAD", "HEAD^{tree}", cwd=seat).stdout == before
    assert worktree.base_of(entry, branch, "main") == main
    assert old != main

    policy = _policy(hive, "mr-absorbed", branch)
    assert policy["valid"], policy["errors"]
    assert policy["integrated_children"] == 1
    assert policy["effective_max_commits"] == 2
    assert _git("rev-parse", "HEAD", "HEAD^{tree}", cwd=seat).stdout == before

    work.finish(epic="mr-absorbed", hive="myrepo")
    assert fakebd.beads[child]["status"] == "closed"
    assert fakebd.beads["mr-absorbed"]["status"] == "closed"
    for sha in [old, main, tip, bubble]:
        assert _git("merge-base", "--is-ancestor", sha, "main", cwd=hive.main).returncode == 0
    assert _git("rev-parse", "main^{tree}", cwd=hive.main).stdout.strip() == before.splitlines()[1]


def _replace_bubble(hive, fakebd, seat, child, parents, subject):
    tree = _git("rev-parse", "HEAD^{tree}", cwd=seat).stdout.strip()
    arguments = ["commit-tree", tree, "-S"]
    for parent in parents:
        arguments += ["-p", parent]
    sha = _git(*arguments, "-m", subject, cwd=seat).stdout.strip()
    _git("reset", "--hard", sha, cwd=seat)
    data = fakebd.beads[child]
    links = json.loads(data["metadata"]["git.commits"])
    data["metadata"]["git.commits"] = json.dumps([*links, sha])
    return sha


@pytest.mark.parametrize(
    "malformation",
    [
        "foreign",
        "unreviewed",
        "stale-review",
        "open-review",
        "unlanded",
        "wrong-child-type",
        "missing-link",
        "missing-source-link",
        "reversed",
        "stacked",
        "duplicate",
        "bad-boundary",
        "parent-count",
    ],
)
def test_child_absorption_rejects_unproven_or_malformed_history(hive, fakebd, malformation):
    epic = "mr-absorbed"
    seat, child, branch, old, main, tip, bubble = _absorbed_main(hive, fakebd)
    gate = next(g for g in fakebd.gates if work_logic.is_review_gate_desc(g["description"]))
    if malformation == "unreviewed":
        gate["close_reason"] = "changes-requested"
    elif malformation == "stale-review":
        gate["description"] = gate["description"].replace(
            work_logic.review_gate_sha(gate["description"]), main
        )
    elif malformation == "open-review":
        gate["status"] = "open"
    elif malformation == "unlanded":
        fakebd.beads[child]["status"] = "in_progress"
    elif malformation == "wrong-child-type":
        fakebd.beads[child]["issue_type"] = "epic"
    elif malformation == "missing-link":
        fakebd.beads[child]["metadata"] = {}
    elif malformation == "missing-source-link":
        fakebd.beads[child]["metadata"]["git.commits"] = json.dumps([bubble])
    else:
        subject = f"chore(merge): bead {child}"
        parents = [old, tip]
        if malformation == "foreign":
            subject = "chore(merge): bead mr-foreign"
        elif malformation == "reversed":
            parents.reverse()
        elif malformation == "stacked":
            parents = [bubble, old]
            subject = f"chore(merge): compose {epic} onto main"
        elif malformation == "duplicate":
            parents = [bubble, tip]
        elif malformation == "bad-boundary":
            tree = _git("rev-parse", f"{old}^{{tree}}", cwd=seat).stdout.strip()
            unrelated = _git(
                "commit-tree", tree, "-m", "chore: unrelated root", cwd=seat
            ).stdout.strip()
            parents = [unrelated, tip]
        elif malformation == "parent-count":
            parents.append(main)
        _replace_bubble(hive, fakebd, seat, child, parents, subject)

    before = _git("rev-parse", "main", branch, cwd=hive.main).stdout
    policy = _policy(hive, epic, branch)
    assert not policy["valid"], malformation
    with pytest.raises(typer.Exit):
        work.finish(epic=epic, hive="myrepo")
    assert _git("rev-parse", "main", branch, cwd=hive.main).stdout == before
    assert fakebd.beads[epic]["status"] != "closed"


def test_absorption_does_not_bypass_enforced_signature_gate(hive, fakebd, capsys):
    _seat, _child, branch, _old, _main, _tip, _bubble = _absorbed_main(
        hive, fakebd, unsigned_child=True
    )
    assert _policy(hive, "mr-absorbed", branch)["valid"]
    hive.cfg_path.write_text(
        hive.cfg_path.read_text().replace(
            'review_gate: "human"', 'review_gate: "human"\n  enforce_signing: true'
        )
    )
    before = _git("rev-parse", "main", branch, cwd=hive.main).stdout
    with pytest.raises(typer.Exit):
        work.finish(epic="mr-absorbed", hive="myrepo")
    assert "not verifiably signed" in capsys.readouterr().err
    assert _git("rev-parse", "main", branch, cwd=hive.main).stdout == before
    assert fakebd.beads["mr-absorbed"]["status"] != "closed"
