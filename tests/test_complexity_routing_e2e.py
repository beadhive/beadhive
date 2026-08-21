"""Release-gate proof for the complete complexity-to-launch contract."""

from __future__ import annotations

from beadhive import localloop, molecule, plan, schedule
from beadhive.config_schema import RoutingTierConfig
from beadhive.model_routing import (
    AvailabilitySnapshot,
    ModelBlockedVerdict,
    ModelSelection,
    translate_for_harness,
)


def _labels(item: dict, *, extra: tuple[str, ...] = ()) -> list[str]:
    labels = [f"complexity:{item['complexity']}", *extra]
    if item.get("model"):
        labels.append(f"model:{item['model']}")
    return labels


def test_spec_to_group_singleton_coordinator_and_concrete_launch_argv():
    spec = {
        "epic": {
            "title": "Route agent work",
            "description": "Choose an available model for every role",
            "design": "Use deterministic capability scoring and gateway model catalogues",
        },
        "issues": [
            {
                "handle": "infer",
                "title": "Implement Python API routing",
                "type": "feature",
                "description": "Build and test the scheduler implementation",
                "acceptance": "The API selects an available model",
                "size": "xs",
                "batch": "routing",
                "component": "runtime",
            },
            {
                "handle": "override",
                "title": "Review the routing proof",
                "type": "task",
                "acceptance": "The proof passes",
                "complexity": "COMPLEX",
                "size": "xs",
                "batch": "routing",
                "component": "runtime",
            },
            {
                "handle": "docs",
                "title": "Polish the routing guide",
                "type": "chore",
                "acceptance": "The guide is readable",
                "complexity": "SIMPLE",
                "size": "l",
            },
        ],
    }

    decisions = plan.compile_complexity_labels(spec)
    assert {decision.provenance for decision in decisions} == {"explicit", "inferred"}
    assert spec["issues"][1]["complexity"] == "COMPLEX", "an explicit override wins"
    assert molecule.validate_spec(spec, {}) == [], "the compiled spec is filing-valid"

    infer, override, docs = spec["issues"]
    beads = [
        {
            "id": "e.1",
            "issue_type": infer["type"],
            "labels": _labels(infer, extra=("batch:routing", "size:xs")),
        },
        {
            "id": "e.2",
            "issue_type": override["type"],
            "labels": _labels(override, extra=("batch:routing", "size:xs")),
        },
        {
            "id": "e.3",
            "issue_type": docs["type"],
            "labels": _labels(docs, extra=("size:l",)),
        },
        {
            "id": "e.nested",
            "issue_type": "epic",
            "labels": ["complexity:REASONING"],
        },
    ]
    dispatch = schedule.plan_schedule(beads, max_size=5)
    assert [group.ids for group in dispatch.groups] == [("e.1", "e.2")]
    assert dispatch.singletons == ["e.3"]
    assert dispatch.coordinators == ("e.nested",)

    routes = [
        RoutingTierConfig(model="openai/gpt-5-mini", ceiling="MEDIUM"),
        RoutingTierConfig(
            model="anthropic/claude-opus-4-1",
            floor="COMPLEX",
            endpoint="https://gateway.example/v1",
        ),
    ]
    availability = [
        AvailabilitySnapshot.live(
            {"openai/gpt-5-mini"},
            source="harness_default",
            role="developer",
            harness="claude",
        ),
        AvailabilitySnapshot.live(
            {"anthropic/claude-opus-4-1"},
            source="gateway_live",
            endpoint="https://gateway.example/v1",
            role="developer",
            harness="claude",
        ),
    ]
    by_id = {bead["id"]: bead for bead in beads}

    grouped = schedule.resolve_launch_decision(
        [by_id[iid] for iid in dispatch.groups[0].ids],
        policy="loose",
        role="developer",
        harness="claude",
        routes=routes,
        availability=availability,
    )
    singleton = schedule.resolve_launch_decision(
        [by_id[dispatch.singletons[0]]],
        policy="loose",
        role="developer",
        harness="claude",
        routes=routes,
        availability=availability,
    )
    coordinator = schedule.resolve_launch_decision(
        [by_id[dispatch.coordinators[0]]],
        policy="loose",
        role="developer",
        harness="claude",
        routes=routes,
        availability=availability,
    )

    assert isinstance(grouped, ModelSelection)
    assert grouped.required_tier.name == "COMPLEX"
    assert grouped.selected_model == "anthropic/claude-opus-4-1"
    assert grouped.availability_source == "gateway_live"
    assert isinstance(singleton, ModelSelection)
    assert singleton.selected_model == "openai/gpt-5-mini"
    assert singleton.availability_source == "harness_default"
    assert isinstance(coordinator, ModelSelection)
    assert coordinator.required_tier.name == "REASONING"

    translated = translate_for_harness(grouped.selected_model, grouped.harness)
    argv = localloop.seat_argv(
        "bh-{role}",
        "developer",
        workspace="/worktree",
        bead="e.1,e.2",
        instructions="/instructions.md",
        session_id="routing-e2e",
        model=translated,
    )
    assert argv[argv.index("--model") + 1] == "claude-opus-4-1"
    assert grouped.as_dict()["selected_model"] == "anthropic/claude-opus-4-1"
    assert "launch_model" not in grouped.as_dict()


def test_loose_fallback_launches_while_strict_preference_blocks():
    bead = {
        "id": "e.1",
        "labels": ["complexity:COMPLEX", "model:openai/unavailable"],
    }
    routes = [RoutingTierConfig(model="anthropic/claude-opus-4-1", floor="COMPLEX")]
    availability = [
        AvailabilitySnapshot.live(
            {"anthropic/claude-opus-4-1"},
            source="harness_default",
            role="developer",
            harness="claude",
        )
    ]

    loose = schedule.resolve_launch_decision(
        [bead],
        policy="loose",
        role="developer",
        harness="claude",
        routes=routes,
        availability=availability,
    )
    strict = schedule.resolve_launch_decision(
        [bead],
        policy="strict",
        role="developer",
        harness="claude",
        routes=routes,
        availability=availability,
    )

    assert isinstance(loose, ModelSelection)
    assert loose.selected_model == "anthropic/claude-opus-4-1"
    assert any("not configured" in warning for warning in loose.warnings)
    assert isinstance(strict, ModelBlockedVerdict)
    assert strict.preferred_model == "openai/unavailable"
    assert "not configured" in strict.reason
    assert strict.remediation
