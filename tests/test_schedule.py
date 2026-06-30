"""`ws.schedule` self-checks — the coordinator's batch-vs-singleton cost model.

Pure unit tests over in-memory bead dicts shaped like `bd list --parent <epic> --json`
(id + labels + `blocks`/`parent-child` dependencies). Each case isolates one decision:
honor a planner batch, auto-detect a linear chain, refuse a non-chain (fan-in/out), and
trip each guard (size cap / mixed model / mixed gate).
"""

from __future__ import annotations

from ws import schedule


def _bead(bead_id, *, blocks=(), parent=None, batch=None, model=None, gate=None):
    """A molecule child: `blocks` lists the ids that block it; labels carry the batch/model/gate
    grouping signals. `parent` adds a parent-child epic edge (must be ignored by scheduling)."""
    labels = []
    if batch:
        labels.append(f"batch:{batch}")
    if model:
        labels.append(f"model:{model}")
    if gate:
        labels.append(f"gate:{gate}")
    deps = [{"issue_id": bead_id, "depends_on_id": b, "type": "blocks"} for b in blocks]
    if parent:
        deps.append({"issue_id": bead_id, "depends_on_id": parent, "type": "parent-child"})
    return {"id": bead_id, "labels": labels, "dependencies": deps}


def _plan(beads, max_size=5):
    return schedule.plan_schedule(beads, max_size=max_size)


def _group_ids(plan):
    return {g.kind: list(g.ids) for g in plan.groups}


def test_empty_molecule_schedules_nothing():
    plan = _plan([])
    assert plan.groups == []
    assert plan.singletons == []


def test_independent_beads_are_singletons():
    # Two beads with no deps between them benefit from parallel wall-time → never batched.
    plan = _plan([_bead("a"), _bead("b")])
    assert plan.groups == []
    assert plan.singletons == ["a", "b"]


def test_linear_chain_auto_detected_as_one_group():
    # a → b → c: private edges throughout, no fan-in/out → one chain group, no singletons.
    plan = _plan([_bead("a"), _bead("b", blocks=["a"]), _bead("c", blocks=["b"])])
    assert _group_ids(plan) == {"chain": ["a", "b", "c"]}
    assert plan.singletons == []
    assert plan.groups[0].kind == "chain"


def test_parent_child_edges_do_not_form_a_chain():
    # Only `blocks` edges are scheduling edges; sharing an epic parent must not batch beads.
    plan = _plan([_bead("a", parent="epic"), _bead("b", parent="epic")])
    assert plan.groups == []
    assert plan.singletons == ["a", "b"]


def test_fan_out_breaks_the_chain():
    # a blocks both b and c (out-degree 2) → no private edge → all singletons.
    plan = _plan([_bead("a"), _bead("b", blocks=["a"]), _bead("c", blocks=["a"])])
    assert plan.groups == []
    assert sorted(plan.singletons) == ["a", "b", "c"]


def test_fan_in_breaks_the_chain():
    # c depends on both a and b (in-degree 2) → no private edge into c → all singletons.
    plan = _plan([_bead("a"), _bead("b"), _bead("c", blocks=["a", "b"])])
    assert plan.groups == []
    assert sorted(plan.singletons) == ["a", "b", "c"]


def test_planner_batch_honored_even_when_parallel():
    # Two same-file beads the planner grouped (no dep edge between them) → honored as one group.
    plan = _plan([_bead("a", batch="files"), _bead("b", batch="files")])
    assert _group_ids(plan) == {"planner": ["a", "b"]}
    assert plan.singletons == []


def test_single_member_batch_is_a_singleton():
    # A lone batch label is no batch — nothing to run as a unit.
    plan = _plan([_bead("a", batch="files")])
    assert plan.groups == []
    assert plan.singletons == ["a"]


def test_planner_batch_and_chain_and_singleton_together():
    # The acceptance scenario: a planner batch + a linear chain each become one grouped agent;
    # the independent bead stays a singleton.
    beads = [
        _bead("p1", batch="files"),
        _bead("p2", batch="files"),
        _bead("a"),
        _bead("b", blocks=["a"]),
        _bead("c", blocks=["b"]),
        _bead("solo"),
    ]
    plan = _plan(beads)
    assert _group_ids(plan) == {"planner": ["p1", "p2"], "chain": ["a", "b", "c"]}
    assert plan.singletons == ["solo"]


def test_size_cap_refuses_an_overlong_chain():
    # A chain longer than the cap is not one reviewable bubble → fall back to singletons.
    beads = [_bead("a")]
    beads += [_bead(f"n{i}", blocks=[beads[-1]["id"]]) for i in range(3)]  # a→n0→n1→n2 (len 4)
    plan = _plan(beads, max_size=3)
    assert plan.groups == []
    assert len(plan.singletons) == 4


def test_mixed_model_tier_refuses_the_chain():
    # A batch runs as one unit on one model; a chain mixing tiers is not batched.
    plan = _plan([_bead("a", model="opus"), _bead("b", blocks=["a"], model="sonnet")])
    assert plan.groups == []
    assert sorted(plan.singletons) == ["a", "b"]


def test_mixed_review_gate_refuses_the_chain():
    plan = _plan([_bead("a", gate="human"), _bead("b", blocks=["a"], gate="gh:pr")])
    assert plan.groups == []
    assert sorted(plan.singletons) == ["a", "b"]


def test_shared_model_tier_chain_is_batched():
    # Same tier across the chain is fine — the guard only trips on a conflict.
    plan = _plan([_bead("a", model="opus"), _bead("b", blocks=["a"], model="opus")])
    assert _group_ids(plan) == {"chain": ["a", "b"]}
