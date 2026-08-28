"""Display-only workspace and pane metadata contract for the Herdr plugin."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from beadhive import herdr_views
from beadhive.cli import app

HIVE = "github/acme/widgets"
_DEFAULT = object()
SCHEMA = json.loads(
    (
        Path(__file__).parents[1] / "docs" / "schemas" / "herdr-presentation-v1.schema.json"
    ).read_text()
)


def _identity() -> dict:
    return {
        "canonical_id": HIVE,
        "prefix": "wdg",
        "provider": "github",
        "organization": "acme",
        "repository": "widgets",
        "display_name": "acme/widgets",
        "registration_kind": "org-native",
        "affiliation": "maintainer",
    }


def _inventory(*beads: str, state: str = "complete") -> dict:
    items = [
        {
            "hive_id": HIVE,
            "hive_prefix": "wdg",
            "bead_id": bead,
            "worktree_id": f"{HIVE}:{bead}",
            "leaf": bead,
            "branch": f"wt/bead/issue/{bead}",
            "path": f"/managed/{bead}",
            "state": "active",
            "retention": "retained",
            "merged": False,
            "dirty": False,
            "safe": False,
            "underlying_state": None,
            "unknown_reason": None,
        }
        for bead in beads
    ]
    return {
        "source_revision": "inventory-r1",
        "coverage": {"state": state},
        "freshness": {"state": "fresh", "as_of": 1000},
        "worktrees": items,
        "total": len(items) if state == "complete" else None,
        "warnings": [],
    }


def _queue(name: str, beads: list[str], *, state: str = "complete") -> dict:
    readiness = {"ready": "ready", "active": "active", "blocked": "blocked"}[name]
    return {
        "revision": f"{name}-r1",
        "generatedAt": 1000,
        "coverage": {"state": state},
        "returned": len(beads),
        "items": [
            {"id": bead, "hiveId": HIVE, "readiness": {"state": readiness}} for bead in beads
        ],
        "warnings": [],
    }


def _queues(*, partial: bool = False) -> dict[str, dict]:
    state = "partial" if partial else "complete"
    return {
        "ready": _queue("ready", ["task-ready"], state=state),
        "active": _queue("active", ["task-working", "task-blocked", "task-missing"], state=state),
        "blocked": _queue("blocked", ["task-dependency"], state=state),
    }


def _dolt(*, ahead: int | None = 0, behind: int | None = 0, state: str = "complete") -> dict:
    return {
        "hive": HIVE,
        "relativeTo": "origin",
        "ahead": ahead,
        "behind": behind,
        "comparisonState": "equal" if ahead == behind == 0 else "diverged",
        "observedAt": "2026-08-28T06:00:00Z",
        "remoteObservedAt": "2026-08-28T06:00:00Z",
        "sourceRevision": "dolt-r1",
        "coverage": {
            "state": state,
            "counts": "known" if ahead is not None and behind is not None else "unknown",
            "reason": None,
        },
    }


def _agent(
    bead: str,
    pane: str,
    *,
    state: str,
    ownership: str = "owned",
    target: str | None = None,
    harness: str = "codex",
    role: str = "developer",
    direct_children: int | None = 0,
    topology_coverage: str = "complete",
) -> dict:
    target = target or f"bh-{bead}"
    return {
        "revision": f"agent:{bead}:1",
        "target": target,
        "hive": HIVE,
        "bead": bead,
        "facts": {
            "schema_version": 1,
            "source_revision": "facts-r1",
            "target": target,
            "hive": HIVE,
            "bead": bead,
            "harness": harness,
            "role": role,
            "work": {
                "operation": "work.implement",
                "phase": "implement",
                "terminal_phase": False,
            },
            "parent": {"relation": "direct", "target": "dispatcher", "bead": "epic-1"},
            "topology": {
                "coverage": topology_coverage,
                "direct_active_children": direct_children,
                "total_active_descendants": 0,
            },
            "retirement": {
                "availability": "forbidden",
                "reason_code": "live",
                "reason": "live",
                "source_revision": "facts-r1",
                "advisory": True,
            },
        },
        "lifecycle": {"state": state, "launched_at": None, "active_at": None},
        "worktree": {
            "path": f"/managed/{bead}",
            "state": "available" if ownership == "owned" else "missing",
            "branch": f"wt/bead/issue/{bead}",
        },
        "presentation": {
            "session": "bh-supervisor",
            "workspace": "w17",
            "workspace_label": f"bh:{HIVE}",
            "tab": "w17:t4",
            "pane": pane,
        },
        "ownership": {
            "marker": "bh.plugin.herdr/v1",
            "association": "metadata",
            "state": ownership,
            "reason": (
                "explicit bh correlation and live resource identities agree"
                if ownership == "owned"
                else "managed worktree is missing"
            ),
        },
        "warnings": [],
    }


def _snapshot() -> dict:
    return {
        "workspaces": [{"workspace_id": "w17", "label": f"bh:{HIVE}"}],
        "tabs": [{"tab_id": "w17:t4", "workspace_id": "w17"}],
        "panes": [
            {"pane_id": "w17:p2", "workspace_id": "w17", "tab_id": "w17:t4"},
            {"pane_id": "w17:p3", "workspace_id": "w17", "tab_id": "w17:t4"},
        ],
    }


def _payload(
    *agents: dict,
    queues: dict[str, dict] | None = None,
    identity: dict | None = None,
    inventory: dict | None = None,
    dolt: dict | None = None,
    snapshot: dict | None | object = _DEFAULT,
) -> dict:
    return herdr_views.presentation_payload(
        HIVE,
        identity or _identity(),
        inventory or _inventory("task-working", "task-blocked", "task-missing"),
        queues or _queues(),
        {"revision": "roster-r1", "agents": list(agents), "warnings": []},
        _snapshot() if snapshot is _DEFAULT else snapshot,
        dolt=dolt or _dolt(ahead=1, behind=0),
        generated_at=10_000,
        sequence=99,
        ttl_ms=15_000,
    )


def test_exact_workspace_and_panes_are_direct_bounded_metadata_reports() -> None:
    payload = _payload(
        _agent("task-working", "w17:p2", state="working"),
        _agent(
            "task-blocked",
            "w17:p3",
            state="blocked",
            harness="claude",
            role="dispatcher",
        ),
    )

    assert payload["summary"] == {"ready": 1, "running": 2, "needs_you": 2}
    assert payload["freshness"] == {
        "state": "fresh",
        "as_of": 10_000,
        "expires_at": 25_000,
        "ttl_ms": 15_000,
    }
    assert payload["coverage"]["state"] == "complete"
    assert payload["workspace"]["locator"] == {
        "session": "bh-supervisor",
        "workspace_id": "w17",
    }
    workspace_report = payload["workspace"]["report"]
    assert workspace_report == {
        "source": "bh.plugin.herdr.presentation.v1",
        "seq": 99,
        "ttl_ms": 15_000,
        "clearTokens": [
            "bh_hive",
            "bh_hive_id",
            "bh_ready",
            "bh_running",
            "bh_needs_you",
            "bh_coverage",
            "bh_revision",
        ],
        "tokens": {
            "bh_space_title": "[wdg] acme/widgets",
            "bh_affiliation": "maintainer",
            "bh_worktrees": "3",
            "bh_dolt_ahead": "dolt ↑1",
            "bh_dolt_behind": "↓0",
        },
    }

    by_bead = {item["correlation"]["bead_id"]: item for item in payload["panes"]}
    working = by_bead["task-working"]
    assert working["locator"] == {
        "session": "bh-supervisor",
        "workspace_id": "w17",
        "tab_id": "w17:t4",
        "pane_id": "w17:p2",
    }
    assert working["correlation"]["state"] == "exact"
    assert working["report"]["source"] == "bh.plugin.herdr.presentation.v1"
    assert working["report"]["tokens"]["bh_agent_title"] == "[codex] bh-developer"
    assert working["report"]["tokens"]["bh_phase"] == "implement"
    assert working["report"]["tokens"]["bh_operation"] == "work.implement"
    assert set(working["report"]["tokens"]) == {
        "bh_agent_title",
        "bh_bead",
        "bh_phase",
        "bh_operation",
    }
    assert "bh_managed_agents" not in working["report"]["tokens"]
    assert "bh_managed_agents" in working["report"]["clearTokens"]
    assert "state_labels" not in working["report"]
    assert "display_agent" not in working["report"]
    assert "agent" not in working["report"]
    assert "applies_to_source" not in working["report"]
    blocked_tokens = by_bead["task-blocked"]["report"]["tokens"]
    assert blocked_tokens["bh_agent_title"] == "[claude] bh-dispatcher"
    assert blocked_tokens["bh_managed_agents"] == "0"
    assert set(blocked_tokens) == {
        "bh_agent_title",
        "bh_bead",
        "bh_phase",
        "bh_operation",
        "bh_managed_agents",
    }
    assert "bh_managed_agents" not in by_bead["task-blocked"]["report"]["clearTokens"]
    assert "bh_attention" not in blocked_tokens
    assert "bh_state" not in blocked_tokens
    assert by_bead["task-missing"]["correlation"]["state"] == "missing"
    assert by_bead["task-missing"]["report"] is None

    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    jsonschema.Draft202012Validator(SCHEMA).validate(payload)


def test_maintainer_and_contributor_space_rows_include_dolt_and_true_zero() -> None:
    maintainer = _payload(
        _agent("task-working", "w17:p2", state="working"),
        inventory=_inventory(),
        dolt=_dolt(ahead=0, behind=0),
    )
    assert {
        "bh_space_title": "[wdg] acme/widgets",
        "bh_affiliation": "maintainer",
        "bh_worktrees": "0",
        "bh_dolt_ahead": "dolt ↑0",
        "bh_dolt_behind": "↓0",
    }.items() <= maintainer["workspace"]["report"]["tokens"].items()

    contributor_identity = _identity()
    contributor_identity.update(
        {
            "prefix": "fork",
            "display_name": "ignored/display",
            "registration_kind": "external",
            "affiliation": "contributor",
        }
    )
    contributor = _payload(
        _agent("task-working", "w17:p2", state="working"),
        identity=contributor_identity,
        inventory=_inventory("one", "two"),
        dolt=_dolt(ahead=0, behind=3),
    )
    assert {
        "bh_space_title": "[fork] acme/widgets",
        "bh_affiliation": "contributor",
        "bh_worktrees": "2",
        "bh_dolt_ahead": "dolt ↑0",
        "bh_dolt_behind": "↓3",
    }.items() <= contributor["workspace"]["report"]["tokens"].items()


def test_unavailable_counts_are_not_fabricated_as_zero() -> None:
    payload = _payload(
        _agent("task-working", "w17:p2", state="working"),
        inventory=_inventory(state="unavailable"),
        dolt=_dolt(ahead=None, behind=None, state="unavailable"),
    )
    tokens = payload["workspace"]["report"]["tokens"]

    assert tokens["bh_worktrees"] == "worktrees -"
    assert tokens["bh_dolt_ahead"] == "dolt ↑-"
    assert tokens["bh_dolt_behind"] == "↓-"
    assert payload["coverage"]["sources"]["worktrees"]["state"] == "unavailable"
    assert payload["coverage"]["sources"]["dolt"]["state"] == "unavailable"


def test_stale_missing_supervisor_and_locator_mismatch_are_reason_coded() -> None:
    stale = _payload(
        _agent("task-working", "w17:p2", state="working"),
        inventory=_inventory(state="stale"),
        dolt=_dolt(state="stale"),
    )
    assert stale["freshness"]["state"] == "stale"
    assert stale["workspace"]["report"]["tokens"]["bh_dolt_ahead"] == "dolt ↑-"

    unavailable = _payload(_agent("task-working", "w17:p2", state="working"), snapshot=None)
    assert unavailable["workspace"]["correlation"]["reason_code"] == "supervisor_unavailable"
    assert unavailable["workspace"]["report"] is None
    unavailable_pane = next(
        item for item in unavailable["panes"] if item["correlation"]["bead_id"] == "task-working"
    )
    assert unavailable_pane["correlation"]["reason_code"] == "supervisor_unavailable"
    assert unavailable_pane["report"] is None

    mismatch_agent = _agent("task-working", "w17:p2", state="working")
    mismatch_agent["presentation"]["workspace"] = "w99"
    mismatch = _payload(mismatch_agent)
    pane = next(
        item for item in mismatch["panes"] if item["correlation"]["bead_id"] == "task-working"
    )
    assert pane["correlation"]["reason_code"] == "locator_mismatch"
    assert pane["report"] is None


def test_sidebar_tokens_are_bounded_control_free_and_do_not_replace_host_status_rows() -> None:
    identity = _identity()
    identity["prefix"] = "wdg\x1b[31m" + "x" * 100
    identity["organization"] = "acme\ncorp"
    agent = _agent(
        "task-working",
        "w17:p2",
        state="idle",
        harness="codex\x00" + "x" * 100,
    )
    payload = _payload(agent, identity=identity)
    pane = next(item for item in payload["panes"] if item["report"] is not None)

    assert payload["source_revision"] == payload["revision"]
    assert payload["policy"]["preserves_host_rows"] == [
        "status_icon",
        "git_branch",
        "git_ahead_behind",
    ]
    for report in (payload["workspace"]["report"], pane["report"]):
        assert report is not None
        for value in report["tokens"].values():
            assert len(value) <= herdr_views.PRESENTATION_TOKEN_LIMIT
            assert not any(unicodedata.category(char).startswith("C") for char in value)
    forbidden = {"bh_state", "bh_attention", "status_icon", "git_branch", "git_ahead_behind"}
    assert forbidden.isdisjoint(payload["workspace"]["report"]["tokens"])
    assert forbidden.isdisjoint(pane["report"]["tokens"])
    assert "state_labels" not in pane["report"]


def test_incomplete_dispatcher_topology_preserves_unknown_managed_count() -> None:
    payload = _payload(
        _agent(
            "task-working",
            "w17:p2",
            state="working",
            role="dispatcher",
            direct_children=None,
            topology_coverage="partial",
        )
    )

    pane = next(
        item for item in payload["panes"] if item["correlation"]["bead_id"] == "task-working"
    )
    assert pane["report"]["tokens"]["bh_managed_agents"] == "-"
    jsonschema.Draft202012Validator(SCHEMA).validate(payload)


@pytest.mark.parametrize("relation", ["root", "direct"])
def test_root_and_nested_dispatchers_publish_direct_managed_count(relation: str) -> None:
    agent = _agent(
        "task-working",
        "w17:p2",
        state="working",
        role="dispatcher",
        direct_children=3,
    )
    agent["facts"]["parent"] = {
        "relation": relation,
        "target": None if relation == "root" else "dispatcher",
        "bead": None if relation == "root" else "epic-1",
    }
    payload = _payload(agent)

    pane = next(
        item for item in payload["panes"] if item["correlation"]["bead_id"] == "task-working"
    )
    assert pane["report"]["tokens"]["bh_managed_agents"] == "3"


def test_v1_schema_keeps_legacy_report_source_documents_readable() -> None:
    payload = _payload(_agent("task-working", "w17:p2", state="working"))
    payload["workspace"]["report"]["source"] = "bh.plugin.herdr.presentation/v1"
    pane = next(
        item for item in payload["panes"] if item["correlation"]["bead_id"] == "task-working"
    )
    pane["report"]["source"] = "bh.plugin.herdr.presentation/v1"

    jsonschema.Draft202012Validator(SCHEMA).validate(payload)


def test_stale_and_missing_correlations_never_publish_a_report_patch() -> None:
    stale = _agent("task-working", "w17:p2", state="working", ownership="stale")
    missing = _agent(
        "task-blocked",
        "w17:p3\x1b[31m",
        state="blocked",
        target="bh-task-blocked\x00" + "x" * 300,
    )
    payload = _payload(stale, missing, queues=_queues(partial=True))
    by_bead = {item["correlation"]["bead_id"]: item for item in payload["panes"]}

    assert payload["coverage"]["state"] == "partial"
    assert payload["summary"] == {"ready": None, "running": None, "needs_you": None}
    assert by_bead["task-working"]["correlation"]["state"] == "stale"
    assert by_bead["task-working"]["report"] is None
    assert by_bead["task-blocked"]["correlation"]["state"] == "missing"
    assert by_bead["task-blocked"]["locator"]["pane_id"] is None
    assert by_bead["task-blocked"]["correlation"]["target"] is None
    assert by_bead["task-blocked"]["report"] is None

    for patch in [payload["workspace"], *payload["panes"]]:
        report = patch.get("report")
        if report is None:
            continue
        for value in report["tokens"].values():
            assert len(value) <= herdr_views.PRESENTATION_TOKEN_LIMIT
            assert not any(unicodedata.category(char).startswith("C") for char in value)


def test_policy_is_explicitly_display_only_and_cli_registers_the_view() -> None:
    payload = _payload(_agent("task-working", "w17:p2", state="idle"))
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["policy"]["values_only"] is True
    assert payload["policy"]["lifecycle_authority"] is False
    assert payload["policy"]["report_agent_authority"] is False
    assert "\x1b" not in encoded
    assert "foreground" not in encoded
    assert "background" not in encoded
    result = CliRunner().invoke(app, ["plugin", "herdr", "view", "--help"])
    assert result.exit_code == 0
    assert "presentation" in result.stdout
