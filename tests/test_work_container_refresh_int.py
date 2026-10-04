"""Real-bd regression: hash-id children refresh their epic container (bh-bd8hq).

`bh plan file` molecules carry hash ids (`mr-ab12c`) — their only link to the epic is the bd
parent-child field.  The container refresh used to be gated on the dotted-id convention, so these
children forked off a container pinned to a stale base.  The fake-bd tests in `test_work.py` seed
ids by hand; this drives the real `bd` so the parent link is the one bd itself reports.
"""

from __future__ import annotations

import pytest

from beadhive import config, plan, work, work_logic, worktree
from harness.beads import bd, resolve_gates, skip_if_no_bd
from harness.hive import make_hive
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]


def _create(hive, title, *, type_="task", parent="", labels="") -> str:
    """Create a bead; a child gets a HASH id linked to its epic only by the parent-child edge.

    `bd create --parent` mints a dotted `<epic>.<n>` id, which the old dotted-id parse handled —
    so create it standalone and attach the edge afterwards, the shape `bh plan file` leaves."""
    args = ["create", title, "--type", type_, "--silent"]
    if labels:
        args += ["--labels", labels]
    res = bd(*args, cwd=hive.main, capture=True)
    bead = (res.stdout or "").strip().splitlines()[-1]
    if parent:
        bd("update", bead, "--parent", parent, cwd=hive.main, capture=True)
        assert "." not in bead  # still a hash id after the link
    return bead


@pytest.fixture(autouse=True)
def _conventions_out_of_scope(monkeypatch):
    """The molecule-convention gate (complexity labels, swarm, kickoff gates) has its own tests;
    this file exercises dispatch mechanics against real bd parent links only."""
    monkeypatch.setattr(plan, "verify_epic", lambda *_args, **_kwargs: [])


def _kicked_off_epic(hive) -> str:
    epic = _create(hive, "molecule", type_="epic")
    assert "." not in epic  # a hash id, not `<epic>.<n>`
    bd("set-state", epic, "kickoff=approved", "--reason", "test", cwd=hive.main, capture=True)
    work.start(epic=epic, as_="disp/lead", hive=hive.repo)
    _land_first_child(hive, epic)
    return epic


def _land_first_child(hive, epic) -> None:
    """Give the container its own history (refreshing an untouched one only fast-forwards)."""
    child = _create(hive, "first", parent=epic)
    work.claim(bead=child, as_="dev/first", hive=hive.repo)
    target = worktree.locate(config.load(), hive.repo, child)[2]
    (target / "first.txt").write_text("first")
    git("add", "-A", cwd=target)
    git("commit", "-qm", f"feat: {child}", cwd=target)
    work.submit(bead=child, as_="dev/first", hive=hive.repo)
    resolve_gates(hive.main, child)  # the human review approval
    work.merge(bead=child, hive=hive.repo, rm=False, molecule=False)


def _advance_main(hive) -> None:
    (hive.main / "main-advance.txt").write_text("advanced")
    git("add", "-A", cwd=hive.main)
    git("commit", "-qm", "fix: integration advanced", cwd=hive.main)


def _assert_refreshed(hive, epic) -> None:
    branch = f"wt/bead/epic/{epic}"
    subject = git("log", "-1", "--format=%s", branch, cwd=hive.main).stdout.strip()
    assert subject == f"chore(merge): refresh {branch} from main"
    main_tip = git("rev-parse", "main", cwd=hive.main).stdout.strip()
    git("merge-base", "--is-ancestor", main_tip, branch, cwd=hive.main)
    cfg = config.load()
    entry = worktree.locate(cfg, hive.repo, epic, kind="epic")[0]
    # the lifecycle refresh is the shape guard_container_refresh accepts at merge and finish
    work_logic.guard_container_refresh(entry, branch, "main", action="merge")


def test_claim_group_of_hash_id_children_refreshes_container(world):
    hive = make_hive(world)
    epic = _kicked_off_epic(hive)
    first = _create(hive, "a", parent=epic, labels="batch:g")
    second = _create(hive, "b", parent=epic, labels="batch:g")
    _advance_main(hive)

    work.claim(bead="", as_="dev/group", group=f"{first},{second}", hive=hive.repo)

    _assert_refreshed(hive, epic)


def test_assign_hash_id_child_refreshes_container(world):
    hive = make_hive(world)
    epic = _kicked_off_epic(hive)
    child = _create(hive, "a", parent=epic)
    _advance_main(hive)

    work.assign(bead=child, to="child", as_="disp/lead", hive=hive.repo)

    _assert_refreshed(hive, epic)


def test_claim_hash_id_child_refreshes_container(world):
    hive = make_hive(world)
    epic = _kicked_off_epic(hive)
    child = _create(hive, "a", parent=epic)
    _advance_main(hive)

    work.claim(bead=child, as_="dev/child", hive=hive.repo)

    _assert_refreshed(hive, epic)
