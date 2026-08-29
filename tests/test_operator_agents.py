"""Contract fixtures for generic authoritative agent facts."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from beadhive import herdr_plugin, operator_agents


def _agent(
    target: str,
    bead: str,
    *,
    role: str = "developer",
    parent: str | None = None,
    operation: str = "work.implement",
    phase: str = "implement",
    terminal: bool = False,
    presence: str = "live",
) -> dict[str, object]:
    return {
        "target": target,
        "hive": "github/acme/widgets",
        "bead": bead,
        "harness": "codex",
        "role": role,
        "operation": operation,
        "phase": phase,
        "terminal_phase": terminal,
        "parent_bead": parent,
        "presence": presence,
        "source_revision": f"issue:{bead}:1",
    }


def _by_target(payload: dict) -> dict[str, dict]:
    return {item["target"]: item for item in payload["agents"]}


def test_direct_developer_root_and_nested_dispatcher_have_exact_topology() -> None:
    payload = operator_agents.project_agent_facts(
        [
            _agent(
                "root-dispatcher",
                "epic-root",
                role="dispatcher",
                operation="work.dispatch",
                phase="dispatch",
            ),
            _agent(
                "nested-dispatcher",
                "epic-child",
                role="dispatcher",
                parent="epic-root",
                operation="work.dispatch",
                phase="dispatch",
            ),
            _agent("developer", "task-1", parent="epic-child"),
        ],
        source_revision="supervisor:r1",
    )
    agents = _by_target(payload)

    assert agents["root-dispatcher"]["parent"] == {
        "relation": "root",
        "target": None,
        "bead": None,
    }
    assert agents["nested-dispatcher"]["parent"] == {
        "relation": "direct",
        "target": "root-dispatcher",
        "bead": "epic-root",
    }
    assert agents["developer"]["parent"] == {
        "relation": "direct",
        "target": "nested-dispatcher",
        "bead": "epic-child",
    }
    assert agents["root-dispatcher"]["topology"] == {
        "coverage": "complete",
        "direct_active_children": 1,
        "total_active_descendants": 2,
    }
    assert agents["nested-dispatcher"]["topology"]["direct_active_children"] == 1
    assert agents["nested-dispatcher"]["topology"]["total_active_descendants"] == 1
    assert agents["developer"]["topology"]["total_active_descendants"] == 0
    assert agents["developer"]["harness"] == "codex"
    assert agents["developer"]["role"] == "developer"


@pytest.mark.parametrize(
    ("operation", "phase", "terminal"),
    [
        ("work.submit", "submit", False),
        ("work.review", "review", False),
        ("work.merge", "merge", False),
        ("work.dispatch", "dispatch", False),
        ("work.complete", "terminal", True),
    ],
)
def test_work_operations_and_terminal_phase_are_preserved_exactly(
    operation: str, phase: str, terminal: bool
) -> None:
    fact = operator_agents.project_agent_facts(
        [
            _agent(
                "agent-1",
                "task-1",
                operation=operation,
                phase=phase,
                terminal=terminal,
                presence="stopped" if terminal else "live",
            )
        ],
        source_revision="supervisor:r1",
    )["agents"][0]

    assert fact["work"] == {
        "operation": operation,
        "phase": phase,
        "terminal_phase": terminal,
    }
    # The terminal flag comes from Beadhive work facts, not presentation idle.
    assert fact["work"]["terminal_phase"] is terminal


def test_missing_parent_partial_topology_and_cycle_are_explicit() -> None:
    orphan = operator_agents.project_agent_facts(
        [_agent("orphan", "task-1", parent="missing-epic")],
        source_revision="supervisor:r1",
    )
    orphan_fact = orphan["agents"][0]
    assert orphan["coverage"] == {"state": "partial", "reason_code": "topology-partial"}
    assert orphan_fact["parent"] == {
        "relation": "missing",
        "target": None,
        "bead": "missing-epic",
    }
    assert orphan_fact["topology"] == {
        "coverage": "partial",
        "direct_active_children": 0,
        "total_active_descendants": 0,
    }

    cycle = operator_agents.project_agent_facts(
        [
            _agent("agent-a", "task-a", parent="task-b"),
            _agent("agent-b", "task-b", parent="task-a"),
        ],
        source_revision="supervisor:r1",
    )
    for fact in cycle["agents"]:
        assert fact["parent"]["relation"] == "cycle"
        assert fact["topology"]["coverage"] == "partial"
        assert fact["topology"]["total_active_descendants"] is None
        assert fact["retirement"]["reason_code"] == "facts-incomplete"


@pytest.mark.parametrize(
    ("observation", "reason_code"),
    [
        (_agent("a", "a"), "live"),
        (
            _agent(
                "a",
                "a",
                operation="work.complete",
                phase="terminal",
                terminal=True,
                presence="retained",
            ),
            "retained",
        ),
        (
            _agent("a", "a", operation="work.review", phase="review"),
            "pending-review",
        ),
        (
            _agent("a", "a", operation="work.submit", phase="submit"),
            "pending-operation",
        ),
    ],
)
def test_retirement_refusal_classes(observation: dict, reason_code: str) -> None:
    retirement = operator_agents.project_agent_facts(
        [observation], source_revision="supervisor:r1"
    )["agents"][0]["retirement"]
    assert retirement["availability"] == "forbidden"
    assert retirement["reason_code"] == reason_code
    assert retirement["source_revision"] == "supervisor:r1"
    assert retirement["advisory"] is True


def test_child_retained_refusal_precedes_terminal_parent_availability() -> None:
    parent = _agent(
        "parent",
        "epic",
        role="dispatcher",
        operation="work.complete",
        phase="terminal",
        terminal=True,
        presence="stopped",
    )
    child = _agent("child", "task", parent="epic")
    facts = _by_target(
        operator_agents.project_agent_facts([parent, child], source_revision="supervisor:r1")
    )

    assert facts["parent"]["retirement"]["availability"] == "forbidden"
    assert facts["parent"]["retirement"]["reason_code"] == "child-retained"


def test_retirable_stale_revision_and_unavailable_supervisor_are_reason_coded() -> None:
    stopped = _agent(
        "done",
        "task",
        operation="work.complete",
        phase="terminal",
        terminal=True,
        presence="stopped",
    )
    current = operator_agents.project_agent_facts([stopped], source_revision="supervisor:r2")
    assert current["agents"][0]["retirement"]["reason_code"] == "retirable"
    assert current["agents"][0]["retirement"]["availability"] == "allowed"

    stale = operator_agents.project_agent_facts(
        [stopped], source_revision="supervisor:r2", expected_revision="supervisor:r1"
    )
    assert stale["coverage"] == {"state": "stale", "reason_code": "stale-revision"}
    assert stale["agents"][0]["retirement"]["availability"] == "unavailable"
    assert stale["agents"][0]["retirement"]["reason_code"] == "stale-revision"

    unavailable = operator_agents.project_agent_facts(
        [stopped], source_revision=None, supervisor_available=False
    )
    assert unavailable["coverage"] == {
        "state": "unavailable",
        "reason_code": "supervisor-unavailable",
    }
    assert unavailable["agents"][0]["topology"] == {
        "coverage": "unavailable",
        "direct_active_children": None,
        "total_active_descendants": None,
    }
    assert unavailable["agents"][0]["retirement"]["reason_code"] == "supervisor-unavailable"


def test_generic_record_contains_no_herdr_presentation_or_token_vocabulary() -> None:
    payload = operator_agents.project_agent_facts(
        [_agent("agent", "task")], source_revision="supervisor:r1"
    )
    encoded = json.dumps(payload, sort_keys=True)

    for forbidden in ("pane", "workspace", "bh_owner", "bh_target", "state_labels", "tokens"):
        assert forbidden not in encoded

    schema = json.loads(
        (
            Path(__file__).parents[1] / "docs" / "schemas" / "beadhive-agent-facts-v1.schema.json"
        ).read_text()
    )
    jsonschema.validate(payload, schema)


def test_herdr_adapter_joins_exact_work_facts_without_treating_idle_as_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(herdr_plugin.store_locator, "dolt_mode", lambda _main: "server")
    monkeypatch.setattr(
        herdr_plugin.bd,
        "show",
        lambda bead, main, strict: {
            "id": bead,
            "issue_type": "task",
            "status": "in_progress",
            "assignee": "dev/alice",
            "parent": "epic-1",
            "labels": ["harness:codex"],
            "updated_at": "2026-08-28T00:00:00Z",
        },
    )
    observation = herdr_plugin._work_observation(
        {
            "target": "agent-1",
            "hive": "github/acme/widgets",
            "bead": "task-1",
            "revision": "agent:r1",
            "lifecycle": {"state": "idle"},
            "_main": str(tmp_path),
            "_record": {"agent": "codex"},
        }
    )

    assert observation["harness"] == "codex"
    assert observation["role"] == "developer"
    assert observation["operation"] == "work.implement"
    assert observation["phase"] == "implement"
    assert observation["terminal_phase"] is False
    assert observation["parent_bead"] == "epic-1"
    assert observation["presence"] == "live"
