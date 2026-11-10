"""`review_gate: gh:pr` submit owns its PR (bh-srb1r; GitHub #35, #50).

Real git in a tmp hive plus the faked ``bd`` and ``gh`` of ``test_work``. What is pinned here:

* submit opens the PR for the pushed bead branch — or adopts the one already open — exactly once
  across resubmits, and the review gate carries its number (``--await-id``), so bd's own gh:pr
  checker can resolve it;
* submit records ``landing=pr-pending``, so ``work land`` completes the bead once the PR merges —
  whether or not bd's checker resolved the review gate first — with no bd passthrough;
* a submit that cannot open a PR (gh missing, ``gh pr create`` failing) refuses before any gate,
  and a PR-less gate left by an older submit is superseded on resubmit.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import config_store, ghpr, work, work_logic
from test_work import (
    CONFIG_YAML,
    _commit,
    _git,
    _minted_host_identity,
    _remote_has,
    _wt,
    fakebd,
    fakegh,
    hive,
)

# the shared fixtures (and the autouse host identity), re-exported for pytest
__all__ = ["_minted_host_identity", "fakebd", "fakegh", "hive"]

URL = "https://github.com/myorg/myrepo/pull/7"


def _ghpr_hive(hive) -> None:
    hive.cfg_path.write_text(CONFIG_YAML.replace('review_gate: "human"', 'review_gate: "gh:pr"'))
    config_store.clear_load_cache()


def _claim_and_commit(hive, fakebd, bead, msg="fix: the change") -> None:
    fakebd.seed(bead, title="fix the widget", acceptance_criteria="widget fixed")
    work.claim(bead=bead, as_="", hive="myrepo")
    _commit(_wt(hive, bead), msg)


def _open_review(fakebd, bead) -> list[dict]:
    return [
        g
        for g in fakebd.gates
        if g["status"] == "open"
        and bead in g["description"]
        and work_logic.is_review_gate_desc(g["description"])
    ]


def _sha(hive, bead) -> str:
    return _git("rev-parse", "--short", "HEAD", cwd=_wt(hive, bead)).stdout.strip()


def test_submit_opens_the_pr_and_the_gate_waits_on_it(hive, fakebd, fakegh):
    _ghpr_hive(hive)
    _claim_and_commit(hive, fakebd, "mr-1")

    work.submit(bead="mr-1", hive="myrepo")

    assert _remote_has(hive, "wt/bead/issue/mr-1")
    (create,) = fakegh.created()
    assert create[create.index("--head") + 1] == "wt/bead/issue/mr-1"
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--title") + 1] == "fix the widget"
    assert "widget fixed" in create[create.index("--body") + 1]
    (gate,) = _open_review(fakebd, "mr-1")
    assert gate["await_type"] == "gh:pr"
    assert gate["await_id"] == "7"  # bd gate check now has a PR number to watch
    assert f"bh:review {_sha(hive, 'mr-1')} PR #7 {URL}" in gate["description"]
    assert fakebd.states["mr-1"]["review"] == "pending"
    assert fakebd.states["mr-1"]["landing"] == "pr-pending"  # what `land` checks


def test_submit_adopts_an_already_open_pr(hive, fakebd, fakegh):
    _ghpr_hive(hive)
    _claim_and_commit(hive, fakebd, "mr-2")
    fakegh.prs = [{"number": 12, "url": URL[:-1] + "12", "state": "OPEN", "mergedAt": None}]

    work.submit(bead="mr-2", hive="myrepo")

    assert fakegh.created() == []
    (gate,) = _open_review(fakebd, "mr-2")
    assert gate["await_id"] == "12"


def test_resubmit_reuses_the_pr_and_keeps_one_gate_on_it(hive, fakebd, fakegh):
    _ghpr_hive(hive)
    _claim_and_commit(hive, fakebd, "mr-3")
    work.submit(bead="mr-3", hive="myrepo")
    fakegh.prs = [{"number": 7, "url": URL, "state": "OPEN", "mergedAt": None}]  # GitHub sees it

    work.submit(bead="mr-3", hive="myrepo")  # same sha: the gate is reused
    _commit(_wt(hive, "mr-3"), "fix: review feedback", fname="more.txt")
    work.submit(bead="mr-3", hive="myrepo")  # new sha: gate superseded, PR adopted

    assert len(fakegh.created()) == 1
    (gate,) = _open_review(fakebd, "mr-3")
    assert gate["await_id"] == "7"
    assert _sha(hive, "mr-3") in gate["description"]


def test_resubmit_supersedes_a_pr_less_gate_left_by_an_older_submit(hive, fakebd, fakegh):
    """The #35 dead end unsticks with a plain resubmit: a same-sha gate with no PR number is
    superseded by one that carries it, not reused."""
    _ghpr_hive(hive)
    _claim_and_commit(hive, fakebd, "mr-4")
    sha = _sha(hive, "mr-4")
    fakebd.gates.append(
        {
            "id": "old",
            "status": "open",
            "description": f"blocks mr-4\n\nReason: bh:review {sha}",
            "await_type": "gh:pr",
            "await_id": "",
        }
    )

    work.submit(bead="mr-4", hive="myrepo")

    assert fakebd.did("gate", "resolve", "old", f"superseded by resubmit {sha}")
    (gate,) = _open_review(fakebd, "mr-4")
    assert gate["await_id"] == "7"


def test_failed_pr_create_refuses_before_any_gate(hive, fakebd, fakegh):
    _ghpr_hive(hive)
    _claim_and_commit(hive, fakebd, "mr-5")
    fakegh.create_rc = 1

    with pytest.raises(typer.Exit):
        work.submit(bead="mr-5", hive="myrepo")

    assert not fakebd.did("gate", "create", "--blocks", "mr-5")
    assert fakebd.states.get("mr-5", {}).get("review") != "pending"


def test_submit_without_gh_refuses_before_pushing(hive, fakebd, fakegh, monkeypatch):
    _ghpr_hive(hive)
    _claim_and_commit(hive, fakebd, "mr-6")
    monkeypatch.setattr(ghpr, "available", lambda: False)

    with pytest.raises(typer.Exit):
        work.submit(bead="mr-6", hive="myrepo")

    assert not _remote_has(hive, "wt/bead/issue/mr-6")
    assert not fakebd.did("gate", "create", "--blocks", "mr-6")
    assert fakegh.calls == []


def test_human_gate_submit_never_touches_gh(hive, fakebd, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("gh invoked on a human-gate submit")

    monkeypatch.setattr(ghpr, "run", _boom)
    _claim_and_commit(hive, fakebd, "mr-7")

    work.submit(bead="mr-7", hive="myrepo")

    assert "landing" not in fakebd.states.get("mr-7", {})


@pytest.mark.parametrize("bd_checker_resolved_first", [False, True])
def test_merged_pr_lands_the_bead_without_the_bd_passthrough(
    hive, fakebd, fakegh, bd_checker_resolved_first
):
    """The documented unattended path: submit → PR merges on GitHub → `work land` closes it.
    bd's gh:pr checker may resolve the review gate first (it now has the PR number) or not —
    land completes the bead either way, and leaves no open gate behind."""
    _ghpr_hive(hive)
    _claim_and_commit(hive, fakebd, "mr-8")
    work.submit(bead="mr-8", hive="myrepo")
    fakegh.prs = [{"number": 7, "url": URL, "state": "MERGED", "mergedAt": "2026-11-10T00:00Z"}]
    if bd_checker_resolved_first:
        for g in _open_review(fakebd, "mr-8"):
            g["status"] = "closed"

    work.land(bead="mr-8", hive="myrepo")

    assert fakebd.beads["mr-8"]["status"] == "closed"
    assert fakebd.beads["mr-8"]["close_reason"] == "merged"
    assert _open_review(fakebd, "mr-8") == []
    assert not any(c[1][:2] == ["gate", "check"] for c in fakebd.calls)


def test_land_refuses_while_the_review_pr_is_open(hive, fakebd, fakegh):
    _ghpr_hive(hive)
    _claim_and_commit(hive, fakebd, "mr-9")
    work.submit(bead="mr-9", hive="myrepo")
    fakegh.prs = [{"number": 7, "url": URL, "state": "OPEN", "mergedAt": None}]

    with pytest.raises(typer.Exit):
        work.land(bead="mr-9", hive="myrepo")

    assert fakebd.beads["mr-9"]["status"] != "closed"
    assert len(_open_review(fakebd, "mr-9")) == 1
