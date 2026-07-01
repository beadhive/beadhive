"""`ws.schedule` self-checks — the coordinator's batch-vs-singleton cost model.

Pure unit tests over in-memory bead dicts shaped like `bd list --parent <epic> --json`
(id + labels + `blocks`/`parent-child` dependencies). Each case isolates one decision:
honor a planner batch, auto-detect a linear chain, refuse a non-chain (fan-in/out), and
trip each guard (size cap / mixed model / mixed gate).
"""

from __future__ import annotations

from ws import schedule


def _bead(
    bead_id, *, blocks=(), parent=None, batch=None, model=None, gate=None, size=None,
    issue_type=None,
):
    """A molecule child: `blocks` lists the ids that block it; labels carry the batch/model/gate
    grouping signals. `parent` adds a parent-child epic edge (must be ignored by scheduling).
    `issue_type='epic'` marks a child epic (dispatch-by-type → nested coordinator)."""
    labels = []
    if batch:
        labels.append(f"batch:{batch}")
    if model:
        labels.append(f"model:{model}")
    if gate:
        labels.append(f"gate:{gate}")
    if size:
        labels.append(f"size:{size}")
    deps = [{"issue_id": bead_id, "depends_on_id": b, "type": "blocks"} for b in blocks]
    if parent:
        deps.append({"issue_id": bead_id, "depends_on_id": parent, "type": "parent-child"})
    bead = {"id": bead_id, "labels": labels, "dependencies": deps}
    if issue_type:
        bead["issue_type"] = issue_type
    return bead


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


# --- operator override: force_single_group collapses past the guards ------------------------


def test_force_single_group_collapses_beads_that_would_trip_guards():
    # Mixed model + mixed gate + independent (no chain) beads normally fan out or refuse; the
    # operator override collapses them into one `collapsed` group regardless.
    beads = [
        _bead("a", model="opus", gate="human"),
        _bead("b", model="sonnet", gate="gh:pr"),
        _bead("c"),
    ]
    plan = schedule.plan_schedule(beads, max_size=2, force_single_group=True)
    assert _group_ids(plan) == {"collapsed": ["a", "b", "c"]}
    assert plan.singletons == []
    assert plan.groups[0].kind == "collapsed"


def test_force_single_group_ignores_the_size_cap():
    # Four beads with a size cap of 2 would refuse a chain; forced, they stay one collapsed group
    # because chunking is governed only by max_beads_per_session (None ⇒ no split).
    beads = [_bead(f"n{i}") for i in range(4)]
    plan = schedule.plan_schedule(beads, max_size=2, force_single_group=True)
    assert _group_ids(plan) == {"collapsed": ["n0", "n1", "n2", "n3"]}
    assert plan.singletons == []


def test_force_single_group_chunks_by_max_beads_per_session():
    # Exceeding the per-session cap splits into consecutive collapsed chunks in order.
    beads = [_bead(f"n{i}") for i in range(5)]
    plan = schedule.plan_schedule(
        beads, max_size=99, force_single_group=True, max_beads_per_session=2
    )
    assert [g.kind for g in plan.groups] == ["collapsed", "collapsed", "collapsed"]
    assert [list(g.ids) for g in plan.groups] == [["n0", "n1"], ["n2", "n3"], ["n4"]]
    assert plan.singletons == []


def test_force_single_group_no_split_when_within_session_cap():
    # At or under the cap, a single collapsed group — no chunking.
    beads = [_bead("a"), _bead("b")]
    plan = schedule.plan_schedule(
        beads, max_size=99, force_single_group=True, max_beads_per_session=2
    )
    assert _group_ids(plan) == {"collapsed": ["a", "b"]}
    assert plan.singletons == []


def test_force_single_group_empty_molecule_schedules_nothing():
    plan = schedule.plan_schedule([], max_size=5, force_single_group=True)
    assert plan.groups == []
    assert plan.singletons == []


def test_force_single_group_is_read_only_default_path_unchanged():
    # The override is opt-in: the default (unforced) path still fans out mixed beads unchanged.
    beads = [_bead("a", model="opus"), _bead("b", model="sonnet")]
    plan = schedule.plan_schedule(beads, max_size=5)
    assert plan.groups == []
    assert sorted(plan.singletons) == ["a", "b"]


# --- auto mode: size-ordinal budget heuristic -----------------------------------------------


def test_size_weight_maps_each_tier_and_defaults_missing_to_medium():
    # xs<s<m<l<xl ordinal weights; an unlabeled bead is assumed medium (same as an explicit m).
    assert [schedule.size_weight(_bead("x", size=s)) for s in ("xs", "s", "m", "l", "xl")] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert schedule.size_weight(_bead("x")) == schedule.size_weight(_bead("y", size="m"))


def test_auto_collapses_small_epic_of_xs_and_s_beads_under_budget():
    # xs(1) + s(2) + xs(1) = 4 ≤ budget 8 → collapse into one grouped session.
    beads = [_bead("a", size="xs"), _bead("b", size="s"), _bead("c", size="xs")]
    assert schedule.auto_should_collapse(beads, budget=8) is True


def test_auto_fans_out_when_size_sum_exceeds_budget():
    # l(4) + xl(5) = 9 > budget 8 → too costly to collapse, fan out.
    beads = [_bead("a", size="l"), _bead("b", size="xl")]
    assert schedule.auto_should_collapse(beads, budget=8) is False


def test_auto_fans_out_on_mixed_model_tiers_even_under_budget():
    # Cheap by size (xs + xs = 2 ≤ 8) but two model tiers can't share one session → fan out.
    beads = [_bead("a", size="xs", model="opus"), _bead("b", size="xs", model="sonnet")]
    assert schedule.auto_should_collapse(beads, budget=8) is False


def test_auto_fans_out_on_mixed_gate_types_even_under_budget():
    # Cheap by size but conflicting review gates disqualify the collapse.
    beads = [_bead("a", size="xs", gate="human"), _bead("b", size="xs", gate="gh:pr")]
    assert schedule.auto_should_collapse(beads, budget=8) is False


def test_auto_collapses_shared_model_and_gate_under_budget():
    # A uniform tier/gate is not a conflict — only a mix trips the guard.
    beads = [
        _bead("a", size="s", model="opus", gate="human"),
        _bead("b", size="s", model="opus", gate="human"),
    ]
    assert schedule.auto_should_collapse(beads, budget=8) is True


def test_auto_sum_equal_to_budget_collapses():
    # Boundary: sum == budget is within budget (≤, not <).
    beads = [_bead("a", size="l"), _bead("b", size="l")]  # 4 + 4 = 8
    assert schedule.auto_should_collapse(beads, budget=8) is True


def test_auto_empty_epic_does_not_collapse():
    assert schedule.auto_should_collapse([], budget=8) is False


# --- collapsed-seat model selection: max tier across the batch ------------------------------


def test_max_model_tier_picks_opus_for_mixed_sonnet_and_opus():
    # A collapsed session covering a sonnet + an opus bead must run at opus (the harder tier).
    beads = [_bead("a", model="sonnet"), _bead("b", model="opus")]
    assert schedule.max_model_tier(beads) == "opus"


def test_max_model_tier_defaults_to_opus_when_no_child_is_labeled():
    # No bead carries a model: label → no signal to widen from → fall back to the opus default.
    beads = [_bead("a"), _bead("b")]
    assert schedule.max_model_tier(beads) == "opus"


def test_max_model_tier_picks_sonnet_for_haiku_and_sonnet():
    # haiku < sonnet → sonnet is capable enough for the whole batch.
    beads = [_bead("a", model="haiku"), _bead("b", model="sonnet")]
    assert schedule.max_model_tier(beads) == "sonnet"


def test_max_model_tier_returns_the_single_tier_when_uniform():
    # A uniform batch dispatches at exactly that tier — no widening.
    beads = [_bead("a", model="haiku"), _bead("b", model="haiku")]
    assert schedule.max_model_tier(beads) == "haiku"


def test_max_model_tier_ignores_unlabeled_beads_among_labeled_ones():
    # An unlabeled bead carries no signal; the labeled sonnet still sets the dispatch tier.
    beads = [_bead("a"), _bead("b", model="sonnet")]
    assert schedule.max_model_tier(beads) == "sonnet"


def test_max_model_tier_honors_an_explicit_default_override():
    # The fallback tier is configurable; with no labels it returns the caller's default.
    assert schedule.max_model_tier([_bead("a")], default="sonnet") == "sonnet"


def test_max_model_tier_empty_batch_falls_back_to_default():
    assert schedule.max_model_tier([]) == "opus"


# ---- dispatch-by-type: child epics → nested coordinators (xn3o.8) ------------


def test_child_epic_is_a_coordinator_not_a_singleton():
    # A child epic is a molecule → its own nested coordinator seat, never a developer singleton.
    plan = _plan([_bead("e1", issue_type="epic")])
    assert plan.coordinators == ("e1",)
    assert plan.singletons == []
    assert plan.groups == []


def test_mixed_children_split_epics_to_coordinators_leaves_to_plan():
    # Leaves flow through the cost model; epics are partitioned out as coordinators.
    plan = _plan(
        [
            _bead("e1", issue_type="epic"),
            _bead("i1"),
            _bead("i2", batch="b"),
            _bead("i3", batch="b"),
        ]
    )
    assert plan.coordinators == ("e1",)
    assert _group_ids(plan) == {"planner": ["i2", "i3"]}  # leaf batch honored
    assert plan.singletons == ["i1"]  # leaf singleton
    assert "e1" not in plan.singletons


def test_child_epic_never_batched_or_collapsed_with_leaves():
    # Even a batch: label on an epic can't fold it into a leaf group — type wins.
    plan = _plan([_bead("e1", issue_type="epic", batch="b"), _bead("i1", batch="b")])
    assert plan.coordinators == ("e1",)
    # only the leaf remains in the batch group's candidate set (a lone member ⇒ not a group)
    assert all("e1" not in list(g.ids) for g in plan.groups)


def test_force_single_group_excludes_epics_from_the_collapsed_group():
    plan = schedule.plan_schedule(
        [_bead("e1", issue_type="epic"), _bead("i1"), _bead("i2")],
        max_size=5,
        force_single_group=True,
    )
    assert plan.coordinators == ("e1",)
    assert [list(g.ids) for g in plan.groups] == [["i1", "i2"]]  # only the leaves collapse


def test_workstream_all_epic_children_are_all_coordinators():
    # A workstream's children are all epics → all coordinators, nothing to batch/fan out.
    plan = _plan([_bead("e1", issue_type="epic"), _bead("e2", issue_type="epic")])
    assert plan.coordinators == ("e1", "e2")
    assert plan.groups == [] and plan.singletons == []


def test_auto_should_collapse_ignores_child_epics():
    # A workstream (all-epic children) never auto-collapses — epics aren't leaf budget.
    beads = [_bead("e1", issue_type="epic", size="l"), _bead("e2", issue_type="epic", size="l")]
    assert schedule.auto_should_collapse(beads, budget=8) is False
