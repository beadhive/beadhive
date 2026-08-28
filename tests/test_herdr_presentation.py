"""Display-only workspace and pane metadata contract for the Herdr plugin."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import jsonschema
from typer.testing import CliRunner

from beadhive import herdr_views
from beadhive.cli import app

HIVE = "github/acme/widgets"
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


def _agent(
    bead: str,
    pane: str,
    *,
    state: str,
    ownership: str = "owned",
    target: str | None = None,
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
            "harness": "codex",
            "role": "developer",
            "work": {
                "operation": "work.implement",
                "phase": "implement",
                "terminal_phase": False,
            },
            "parent": {"relation": "direct", "target": "dispatcher", "bead": "epic-1"},
            "topology": {
                "coverage": "complete",
                "direct_active_children": 0,
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


def _payload(*agents: dict, queues: dict[str, dict] | None = None) -> dict:
    return herdr_views.presentation_payload(
        HIVE,
        _identity(),
        _inventory("task-working", "task-blocked", "task-missing"),
        queues or _queues(),
        {"revision": "roster-r1", "agents": list(agents), "warnings": []},
        _snapshot(),
        generated_at=10_000,
        sequence=99,
        ttl_ms=15_000,
    )


def test_exact_workspace_and_panes_are_direct_bounded_metadata_reports() -> None:
    payload = _payload(
        _agent("task-working", "w17:p2", state="working"),
        _agent("task-blocked", "w17:p3", state="blocked"),
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
        "source": "bh.plugin.herdr.presentation/v1",
        "seq": 99,
        "ttl_ms": 15_000,
        "tokens": {
            "bh_hive": "acme/widgets",
            "bh_hive_id": HIVE,
            "bh_affiliation": "maintainer",
            "bh_ready": "1",
            "bh_running": "2",
            "bh_needs_you": "2",
            "bh_worktrees": "3",
            "bh_coverage": "complete",
            "bh_revision": payload["revision"],
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
    assert working["report"]["tokens"]["bh_phase"] == "implement"
    assert working["report"]["tokens"]["bh_children"] == "0"
    assert working["report"]["state_labels"]["blocked"] == "NEEDS YOU"
    assert "agent" not in working["report"]
    assert "applies_to_source" not in working["report"]
    assert by_bead["task-blocked"]["report"]["tokens"]["bh_attention"] == "needs_you"
    assert by_bead["task-missing"]["correlation"]["state"] == "missing"
    assert by_bead["task-missing"]["report"] is None

    jsonschema.Draft202012Validator.check_schema(SCHEMA)
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
