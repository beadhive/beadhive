"""Contract tests for the Herdr-specific, nearly-rendered projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
from typer.testing import CliRunner

from beadhive import (
    herdr_plugin,
    herdr_views,
    operator_actions,
    operator_sources,
    operator_work_items,
    state_stream,
)
from beadhive.agent_run_summary import Freshness
from beadhive.cli import app
from beadhive.engine import FederationPeer, FederationStatus
from beadhive.operator_sources import OperatorSourceError
from beadhive.public_readers import AgentRunSnapshot, Coverage

runner = CliRunner()
SCHEMA = json.loads(
    (Path(__file__).parents[1] / "docs" / "schemas" / "herdr-view-v1.schema.json").read_text()
)
HIVE = "acme/widgets"


def _summary(hive: str = HIVE, *, label: str = "Widgets", ready: int = 1) -> dict:
    return {
        "id": hive,
        "displayLabel": label,
        "prefix": "wdg",
        "provider": "github",
        "org": hive.split("/")[0],
        "repo": hive.split("/")[-1],
        "availability": {"state": "available", "reason": None},
        "counts": {"open": ready, "ready": ready, "active": 0, "blocked": 0},
        "revision": "factory-r1",
        "asOf": 1000,
        "coverage": {"state": "complete"},
        "advertisedActions": operator_actions.hive_actions(
            hive_id=hive, revision="factory-r1", advertised_at=1000
        ),
    }


def _work(bead: str, readiness: str, *, title: str = "Ship widget") -> dict:
    target = {"hiveId": HIVE, "kind": "work-item", "id": bead}
    reason = {
        "ready": "all prerequisites satisfied",
        "blocked": "waiting on dependency",
        "active": "claimed by an agent",
    }[readiness]
    return {
        "ref": target,
        "revision": "work-r1",
        "hiveId": HIVE,
        "id": bead,
        "title": title,
        "description": "Description\nwith an ESC \x1b[31m token",
        "design": "design",
        "acceptanceCriteria": "acceptance",
        "notes": "notes",
        "labels": ["ui"],
        "issueType": "task",
        "priority": 1,
        "status": "open",
        "readiness": {"state": readiness, "reason": reason},
        "blockerCount": 1 if readiness == "blocked" else 0,
        "dependencies": [],
        "dependents": [],
        "gates": [],
        "claim": None,
        "advertisedActions": operator_actions.work_item_actions(
            target=target,
            readiness=readiness,
            readiness_reason=reason,
            partial=False,
            revision="work-r1",
            advertised_at=1000,
        ),
    }


def _queue(name: str, items: list[dict]) -> dict:
    return {
        "schemaVersion": 1,
        "hiveId": HIVE,
        "queue": name,
        "revision": f"{name}-r1",
        "generatedAt": 1000,
        "freshness": {"state": "fresh", "asOf": 1000},
        "coverage": {"state": "complete"},
        "limit": 200,
        "returned": len(items),
        "truncated": False,
        "nextCursor": None,
        "items": items,
        "warnings": [],
    }


def _agent(
    *, target: str = "bh-widget-1", state: str = "working", ownership: str = "owned"
) -> dict:
    reason = "current bh-owned live pane is proven"
    action_target = {"hiveId": HIVE, "kind": "agent", "id": target}
    return {
        "target": target,
        "hive": HIVE,
        "bead": "widget-1",
        "lifecycle": {"state": state},
        "worktree": {"path": "/tmp/widgets", "state": "present"},
        "presentation": {"tab": "Agents", "pane": "pane-1"},
        "ownership": {"state": ownership, "reason": reason},
        "capabilities": ["attach", "dispatch", "watch", "reap"],
        "revision": "agent-r1",
        "advertised_actions": operator_actions.agent_actions(
            target=action_target,
            ownership_state=ownership,
            lifecycle_state=state,
            reason=reason,
            revision="agent-r1",
            advertised_at=1000,
            max_prompt_bytes=8192,
        ),
    }


def _roster(*agents: dict) -> dict:
    return {"revision": "roster-r1", "observed_at": 1000, "agents": list(agents), "warnings": []}


def _crew_agent(
    target: str,
    bead: str,
    *,
    role: str = "developer",
    parent: str | None = None,
    relation: str = "root",
    direct: int = 0,
    total: int = 0,
    terminal: bool = False,
    session: str = "bh-supervisor",
) -> dict:
    action_target = {"hiveId": HIVE, "kind": "agent", "id": target}
    reason = "current bh-owned live pane is proven"
    actions = operator_actions.agent_actions(
        target=action_target,
        ownership_state="owned",
        lifecycle_state="idle",
        reason=reason,
        revision="roster-r1",
        advertised_at=1000,
        max_prompt_bytes=8192,
    )
    return {
        "revision": f"agent:{target}",
        "target": target,
        "hive": HIVE,
        "bead": bead,
        "facts": {
            "harness": "codex",
            "role": role,
            "work": {
                "operation": "work.complete"
                if terminal
                else "work.dispatch"
                if role == "dispatcher"
                else "work.implement",
                "phase": "terminal"
                if terminal
                else "dispatch"
                if role == "dispatcher"
                else "implement",
                "terminal_phase": terminal,
            },
            "parent": {"relation": relation, "target": parent, "bead": None},
            "topology": {
                "coverage": "complete",
                "direct_active_children": direct,
                "total_active_descendants": total,
            },
            "retirement": {
                "availability": "forbidden",
                "reason_code": "live",
                "reason": "the agent remains live",
                "source_revision": "roster-r1",
                "advisory": True,
            },
        },
        "lifecycle": {"state": "idle"},
        "presentation": {
            "session": session,
            "workspace": "ws-1",
            "tab": f"tab-{target}",
            "pane": f"pane-{target}",
        },
        "ownership": {"state": "owned", "reason": reason},
        "advertised_actions": actions,
    }


def _crew_roster(*agents: dict) -> dict:
    return {
        "revision": "roster-r1",
        "session": "bh-supervisor",
        "authoritative_session": True,
        "observed_at": 1000,
        "agents": list(agents),
        "warnings": [],
    }


def _crew_snapshot() -> dict:
    return {"workspaces": [{"workspace_id": "ws-1", "label": f"bh:{HIVE}"}]}


def _crew_fixture() -> tuple[dict, ...]:
    return (
        _crew_agent("dispatcher-1", "epic-root", role="dispatcher", direct=2, total=3),
        _crew_agent(
            "developer-b",
            "task-b",
            parent="dispatcher-1",
            relation="direct",
            terminal=True,
        ),
        _crew_agent(
            "dispatcher-2",
            "epic-child",
            role="dispatcher",
            parent="dispatcher-1",
            relation="direct",
            direct=1,
            total=1,
        ),
        _crew_agent("developer-c", "task-c", parent="dispatcher-2", relation="direct"),
        _crew_agent("developer-direct", "task-direct"),
    )


def _queues() -> dict[str, dict]:
    return {
        "ready": _queue("ready", [_work("widget-2", "ready", title="Line 1\n\x1b[31mLine 2")]),
        "active": _queue("active", []),
        "blocked": _queue("blocked", [_work("widget-3", "blocked")]),
    }


def test_picker_is_attention_ordered_bounded_and_cursor_scoped() -> None:
    roster = _roster(_agent(state="blocked"))
    summaries = [_summary("acme/quiet", label="Quiet"), _summary()]

    first = herdr_views.picker_payload(summaries, roster, limit=1, cursor=None)

    assert first["rows"][0]["entity"]["id"] == HIVE
    assert first["returned"] == 1
    assert first["truncated"] is True
    assert first["next_cursor"] and "/" not in first["next_cursor"]
    assert first["rows"][0]["key"] == f"hive:{HIVE}"
    assert first["rows"][0]["tokens"]["provider"] == "github"
    assert first["actions"][0]["invoke"]["argv"] == [
        "bh",
        "plugin",
        "herdr",
        "view",
        "deck",
        "--hive",
        HIVE,
        "--json",
    ]
    jsonschema.validate(first, SCHEMA)

    second = herdr_views.picker_payload(summaries, roster, limit=1, cursor=first["next_cursor"])
    assert second["rows"][0]["entity"]["id"] == "acme/quiet"

    with pytest.raises(OperatorSourceError, match="projection changed") as exc:
        herdr_views.picker_payload(
            [*summaries, _summary("acme/new")],
            roster,
            limit=1,
            cursor=first["next_cursor"],
        )
    assert exc.value.code == "view_cursor_revision_mismatch"


def test_real_roster_revision_invalidates_deck_cursor_on_agent_change(
    tmp_path, monkeypatch
) -> None:
    cwd = tmp_path / "widget-1"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    target = "bh-widget-1"
    pane = {
        "pane_id": "w1:p2",
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "label": target,
        "cwd": str(cwd),
        "tokens": {
            "bh_owner": "bh.plugin.herdr/v1",
            "bh_hive_id": HIVE,
            "bh_bead_id": "widget-1",
            "bh_target": target,
            "bh_schema": "1",
        },
    }
    snapshot = {
        "agents": [{"name": target, "state": "working", "pane_id": "w1:p2"}],
        "panes": [pane],
        "workspaces": [{"workspace_id": "w1", "label": f"bh:{HIVE}"}],
    }
    monkeypatch.setattr(
        herdr_plugin.worktree,
        "locate",
        lambda *_args: ({}, tmp_path, cwd, "wt/bead/issue/widget-1"),
    )
    monkeypatch.setattr(
        herdr_plugin.worktree,
        "managed",
        lambda _cfg: [("widget", str(cwd), "wt/bead/issue/widget-1")],
    )

    first_roster = herdr_plugin._roster_payload(snapshot, {"managed_repos": []})
    first = herdr_views.deck_payload(HIVE, _queues(), first_roster, limit=1, cursor=None)
    assert (
        herdr_plugin._roster_payload(snapshot, {"managed_repos": []})["revision"]
        == first_roster["revision"]
    )
    snapshot["agents"][0]["state"] = "blocked"
    second_roster = herdr_plugin._roster_payload(snapshot, {"managed_repos": []})

    assert first_roster["revision"] != second_roster["revision"]
    assert first_roster["agents"][0]["revision"] != second_roster["agents"][0]["revision"]
    with pytest.raises(OperatorSourceError) as exc:
        herdr_views.deck_payload(
            HIVE,
            _queues(),
            second_roster,
            limit=1,
            cursor=first["next_cursor"],
        )
    assert exc.value.code == "view_cursor_revision_mismatch"

    monkeypatch.setattr(herdr_plugin.worktree, "managed", lambda _cfg: [])
    stale_roster = herdr_plugin._roster_payload(snapshot, {"managed_repos": []})
    assert stale_roster["agents"][0]["ownership"]["state"] == "stale"
    assert stale_roster["revision"] != second_roster["revision"]


def test_real_roster_revision_tracks_observed_and_expected_worktree_paths(
    tmp_path, monkeypatch
) -> None:
    expected = tmp_path / "expected-widget-1"
    observed_a = tmp_path / "observed-a"
    observed_b = tmp_path / "observed-b"
    expected.mkdir()
    observed_a.mkdir()
    observed_b.mkdir()
    (expected / ".git").write_text("gitdir: elsewhere\n")
    target = "bh-widget-1"
    pane = {
        "pane_id": "w1:p2",
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "label": target,
        "cwd": str(observed_a),
        "tokens": {
            "bh_owner": "bh.plugin.herdr/v1",
            "bh_hive_id": HIVE,
            "bh_bead_id": "widget-1",
            "bh_target": target,
            "bh_schema": "1",
        },
    }
    snapshot = {
        "agents": [{"name": target, "state": "working", "pane_id": "w1:p2"}],
        "panes": [pane],
        "workspaces": [{"workspace_id": "w1", "label": f"bh:{HIVE}"}],
    }
    monkeypatch.setattr(
        herdr_plugin.worktree,
        "locate",
        lambda *_args: ({}, tmp_path, expected, "wt/bead/issue/widget-1"),
    )
    monkeypatch.setattr(
        herdr_plugin.worktree,
        "managed",
        lambda _cfg: [("widget", str(expected), "wt/bead/issue/widget-1")],
    )

    first_roster = herdr_plugin._roster_payload(snapshot, {"managed_repos": []})
    first = herdr_views.deck_payload(HIVE, _queues(), first_roster, limit=1, cursor=None)
    pane["cwd"] = str(observed_b)
    second_roster = herdr_plugin._roster_payload(snapshot, {"managed_repos": []})

    first_agent = first_roster["agents"][0]
    second_agent = second_roster["agents"][0]
    assert first_agent["ownership"] == second_agent["ownership"]
    assert first_agent["revision"] != second_agent["revision"]
    assert first_roster["revision"] != second_roster["revision"]
    with pytest.raises(OperatorSourceError) as exc:
        herdr_views.deck_payload(
            HIVE,
            _queues(),
            second_roster,
            limit=1,
            cursor=first["next_cursor"],
        )
    assert exc.value.code == "view_cursor_revision_mismatch"


def test_deck_sections_layout_tokens_and_action_invocations_are_safe() -> None:
    roster = _roster(_agent(state="working"), _agent(target="bh-widget-2", state="blocked"))

    wide = herdr_views.deck_payload(HIVE, _queues(), roster, limit=20, cursor=None, width=140)
    narrow = herdr_views.deck_payload(HIVE, _queues(), roster, limit=20, cursor=None, width=60)

    assert [section["id"] for section in wide["sections"]] == ["ready", "running", "needs-you"]
    assert [section["id"] for section in narrow["sections"]] == ["needs-you", "running", "ready"]
    assert wide["layout"]["surfaces"]["deck"]["section_mode"] == "columns"
    assert narrow["layout"]["surfaces"]["deck"]["section_mode"] == "single-list"

    rendered = json.dumps(wide)
    assert "\x1b" not in rendered
    for section in wide["sections"]:
        for row in section["rows"]:
            assert "\n" not in row["primary"]
            assert len(row["primary"]) <= 160
            assert row["key"].startswith(("work-item:", "agent:"))

    launch = next(
        action
        for action in wide["actions"]
        if action["source_action"] == "work-item.launch" and action["availability"] == "allowed"
    )
    actions = {action["source_action"]: action for action in wide["actions"] if action["invoke"]}
    assert launch["invoke"]["argv"][:3] == ["bh", "plugin", "herdr"]
    assert launch["invoke"]["shell"] is False
    dispatch = actions["agent.dispatch"]
    assert dispatch["invoke"]["input"] == "stdin"
    assert "--stdin" in dispatch["invoke"]["argv"]
    assert dispatch["input"]["schema"]["sensitive"] is True
    assert "prompt" not in rendered.casefold()
    assert actions["agent.reap"]["availability"] == "confirmation-required"
    jsonschema.validate(wide, SCHEMA)
    jsonschema.validate(narrow, SCHEMA)


def test_deck_keeps_active_work_visible_without_a_correlated_agent() -> None:
    queues = {
        "ready": _queue("ready", []),
        "active": _queue("active", [_work("widget-active", "active")]),
        "blocked": _queue("blocked", []),
    }

    deck = herdr_views.deck_payload(HIVE, queues, _roster(), limit=20, cursor=None)
    running = next(section for section in deck["sections"] if section["id"] == "running")

    assert [row["entity"]["id"] for row in running["rows"]] == ["widget-active"]


def test_picker_counts_unproven_running_agent_as_needing_attention() -> None:
    stale = _agent(state="working", ownership="stale")

    picker = herdr_views.picker_payload([_summary()], _roster(stale), limit=10, cursor=None)

    assert picker["rows"][0]["counts"]["running"] == 0
    assert picker["rows"][0]["counts"]["needs_attention"] == 1


def test_unavailable_and_unsafe_actions_never_publish_an_invocation() -> None:
    foreign = _agent(target="bh-foreign", ownership="foreign")
    actions = {
        item["source_action"]: item
        for item in herdr_views.agent_payload(foreign, _roster(foreign))["actions"]
    }
    assert actions["agent.dispatch"]["availability"] == "forbidden"
    assert actions["agent.dispatch"]["invoke"] is None

    unsafe = operator_actions.hive_actions(
        hive_id="acme/widgets;touch-pwned", revision="r1", advertised_at=1000
    )[0]
    rendered = herdr_views.render_action(unsafe)
    assert rendered["availability"] == "unavailable"
    assert rendered["reason_code"] == "unsafe_entity_identity"
    assert rendered["invoke"] is None


def test_bead_and_agent_inspectors_preserve_exact_generic_and_roster_facts() -> None:
    item = _work("widget-2", "ready")
    detail = {
        "hiveId": HIVE,
        "revision": "detail-r1",
        "freshness": {"state": "fresh", "asOf": 1000},
        "coverage": {"state": "complete"},
        "warnings": [],
        "item": item,
    }
    agent = {**_agent(), "bead": "widget-2"}

    bead = herdr_views.bead_payload(detail, _roster(agent))
    inspector = herdr_views.agent_payload(agent, _roster(agent))

    assert bead["scope"] == {"hive": HIVE, "bead": "widget-2"}
    assert bead["freshness"]["as_of"] == 1000
    assert "asOf" not in bead["freshness"]
    assert bead["detail"]["agents"][0]["entity"]["id"] == "bh-widget-1"
    assert inspector["scope"]["target"] == "bh-widget-1"
    assert inspector["detail"]["ownership"] == agent["ownership"]
    jsonschema.validate(bead, SCHEMA)
    jsonschema.validate(inspector, SCHEMA)


@pytest.mark.parametrize(
    ("width", "variant", "mode", "inspector", "direction"),
    [
        (60, "narrow", "single-list", "overlay", "down"),
        (100, "medium", "tabs", "below", "right"),
        (140, "wide", "columns", "below", "right"),
    ],
)
def test_workspace_layout_has_deterministic_popup_and_companion_split_semantics(
    width: int, variant: str, mode: str, inspector: str, direction: str
) -> None:
    payload = herdr_views.layout_payload(
        HIVE,
        {
            "width": width,
            "height": 40,
            "workspace_id": "workspace-1",
            "pane_id": "pane-1",
        },
    )
    layout = payload["layout"]
    deck = layout["surfaces"]["deck"]

    assert layout["session"] == "bh-supervisor"
    assert layout["cross_session_focus"] is False
    assert [(tab["role"], tab["owns_agents"]) for tab in layout["tabs"]] == [
        ("board", False),
        ("agents", True),
    ]
    assert (deck["variant"], deck["section_mode"], deck["inspector"]) == (
        variant,
        mode,
        inspector,
    )
    assert (
        deck["placement"],
        deck["direction"],
        deck["target_role"],
        deck["lifecycle"],
        deck["close_behavior"],
        deck["reopen_behavior"],
    ) == (
        "split",
        direction,
        "agents",
        "ordinary-pane",
        "close",
        "reopen-split",
    )
    assert layout["surfaces"]["picker"]["placement"] == "popup"
    assert layout["surfaces"]["agent_actions"]["pane_id"] is None
    tray = layout["surfaces"]["activity_tray"]
    assert (tray["placement"], tray["hide_behavior"], tray["show_behavior"]) == (
        "split",
        "close",
        "reopen-split",
    )
    jsonschema.validate(payload, SCHEMA)


def test_layout_without_workspace_context_preserves_explicit_dedicated_tab_contract() -> None:
    payload = herdr_views.layout_payload(HIVE, {"width": 100, "height": 40})
    deck = payload["layout"]["surfaces"]["deck"]

    assert (
        deck["placement"],
        deck["direction"],
        deck["target_role"],
        deck["lifecycle"],
        deck["close_behavior"],
        deck["reopen_behavior"],
    ) == (
        "tab",
        None,
        "board",
        "ordinary-tab",
        "close",
        "reopen-tab",
    )
    assert payload["schema_version"] == 1
    jsonschema.validate(payload, SCHEMA)


def test_unresolved_hive_keeps_picker_popup_even_with_workspace_context() -> None:
    payload = herdr_views.layout_payload(
        None,
        {"width": 100, "height": 40, "workspace_id": "workspace-1", "pane_id": "pane-1"},
    )

    assert payload["layout"]["surfaces"]["picker"]["placement"] == "popup"
    assert payload["layout"]["surfaces"]["deck"]["placement"] == "tab"
    jsonschema.validate(payload, SCHEMA)


def test_stream_is_snapshot_first_bounded_and_resyncs_an_opaque_stale_cursor() -> None:
    deck = herdr_views.deck_payload(HIVE, _queues(), _roster(_agent()), limit=20, cursor=None)

    frames = herdr_views.stream_frames(deck, hive=HIVE, since=None, limit=2)
    assert [frame["type"] for frame in frames] == ["snapshot", "agent-observed"]
    assert len(frames) == 2
    assert frames[0]["snapshot"] == deck
    assert frames[0]["cursor"] and HIVE not in frames[0]["cursor"]
    for frame in frames:
        jsonschema.validate(frame, SCHEMA)

    changed = {**deck, "revision": "new-revision"}
    stale = herdr_views.stream_frames(changed, hive=HIVE, since=frames[0]["cursor"], limit=1)
    assert stale[0]["type"] == "snapshot"
    assert stale[0]["resync_required"] is True
    assert stale[0]["resync_reason"] == "view_cursor_revision_mismatch"


def test_deck_cursor_reaches_ready_items_beyond_the_generic_queue_page_limit(
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    beads = state_stream.ProviderSnapshot(
        scope="hive",
        revision="beads-many",
        as_of=now,
        issues=tuple(
            state_stream.StreamIssue(
                id=f"widget-{index:03d}",
                hive=HIVE,
                issue_type="task",
                status="open",
                priority="P1",
                title=f"Widget {index:03d}",
                updated_at=now,
            )
            for index in range(401)
        ),
    )
    runtime = AgentRunSnapshot(
        host_id="host-1",
        source_id="runtime-1",
        revision="runtime-many",
        summaries=(),
        coverage=Coverage.COMPLETE,
        coverage_reason=None,
        freshness=Freshness(state="fresh", as_of=now),
    )

    class Sources:
        cfg = {}

        def resolve_hive(self, hive_id):
            assert hive_id == HIVE
            return SimpleNamespace(entry={})

        def refresh_hive(self, _hive):
            return beads, runtime

        def close(self):
            pass

    queue_calls = []
    queue_payload = operator_work_items.queue_payload

    def counted_queue_payload(**kwargs):
        queue_calls.append(kwargs["query"].queue)
        return queue_payload(**kwargs)

    monkeypatch.setattr(operator_work_items, "queue_payload", counted_queue_payload)
    backend = herdr_views.ViewBackend(cfg={}, sources=Sources(), _roster=_roster())
    first = backend.deck(HIVE, limit=200, cursor=None)
    second = backend.deck(HIVE, limit=200, cursor=first["next_cursor"])
    third = backend.deck(HIVE, limit=200, cursor=second["next_cursor"])

    assert first["returned"] == 200
    assert first["truncated"] is True
    assert second["returned"] == 200
    assert second["truncated"] is True
    assert third["returned"] == 1
    assert third["sections"][0]["rows"][0]["entity"]["id"] == "widget-400"
    assert third["truncated"] is False
    assert queue_calls == ["ready", "active", "blocked"] * 3


def test_live_operator_sources_deck_cursor_survives_a_new_polling_instance(
    tmp_path, monkeypatch
) -> None:
    hive_id = "github/acme/widgets"
    cfg = {
        "managed_repos": [
            {
                "provider": "github",
                "org": "acme",
                "repo": "widgets",
                "prefix": "widget",
                "kind": "org-native",
            }
        ]
    }
    runtime = AgentRunSnapshot(
        host_id="host-1",
        source_id="runtime-1",
        revision="runtime-content-r1",
        summaries=(),
        coverage=Coverage.COMPLETE,
        coverage_reason=None,
        freshness=Freshness(state="fresh", as_of="2026-08-27T12:00:00Z"),
    )
    issues = tuple(
        state_stream.StreamIssue(
            id=f"widget-{index}",
            hive=hive_id,
            issue_type="task",
            status="open",
            priority="P1",
            title=f"Widget {index}",
            updated_at="2026-08-27T12:00:00Z",
            labels=(("release:feature",) if index == 0 else ("release:fix",) if index == 1 else ()),
        )
        for index in range(3)
    )

    class Provider:
        def __init__(self, *, instance: str, content: str, source_issues=issues):
            self.instance = instance
            self.content = content
            self.source_issues = source_issues

        def refresh(self, _request):
            return state_stream.ProviderSnapshot(
                scope="hive",
                revision=f"{self.instance}:opaque-source-revision",
                as_of="2026-08-27T12:00:01Z",
                issues=self.source_issues,
                content_revision=self.content,
            )

    def backend(provider, backend_cfg=cfg):
        sources = operator_sources.OperatorSources(
            cfg=backend_cfg,
            host_id="host-1",
            provider=provider,
            summary_reader=lambda *_args: runtime,
            dispatch_sink_for_entry=lambda *_args: tmp_path / "dispatch.jsonl",
        )
        return herdr_views.ViewBackend(cfg=backend_cfg, sources=sources, _roster=_roster())

    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: False)
    first = backend(Provider(instance="poll-a", content="content-r1")).deck(
        hive_id, limit=1, cursor=None, width=100
    )
    second = backend(Provider(instance="poll-b", content="content-r1")).deck(
        hive_id, limit=1, cursor=first["next_cursor"], width=100
    )

    first_ids = {row["entity"]["id"] for section in first["sections"] for row in section["rows"]}
    second_ids = {row["entity"]["id"] for section in second["sections"] for row in section["rows"]}
    assert first_ids == {"widget-0"}
    assert second_ids == {"widget-1"}

    reordered_cfg = {**cfg, "release": {"strategy": "stable-versioning"}}
    reordered_first = backend(
        Provider(instance="poll-c", content="content-r1"), reordered_cfg
    ).deck(hive_id, limit=1, cursor=None, width=100)
    assert {
        row["entity"]["id"] for section in reordered_first["sections"] for row in section["rows"]
    } == {"widget-1"}
    with pytest.raises(OperatorSourceError) as reordered:
        backend(Provider(instance="poll-d", content="content-r1"), reordered_cfg).deck(
            hive_id, limit=1, cursor=first["next_cursor"], width=100
        )
    assert (reordered.value.code, reordered.value.status_code) == (
        "view_cursor_revision_mismatch",
        409,
    )

    changed_issues = (
        *issues[:-1],
        state_stream.StreamIssue(**{**issues[-1].__dict__, "title": "Changed"}),
    )
    with pytest.raises(OperatorSourceError) as exc:
        backend(
            Provider(instance="poll-e", content="content-r2", source_issues=changed_issues)
        ).deck(hive_id, limit=1, cursor=first["next_cursor"], width=100)
    assert (exc.value.code, exc.value.status_code) == ("view_cursor_revision_mismatch", 409)


def test_presentation_composes_extended_hive_facts_without_losing_queues(
    monkeypatch,
) -> None:
    queues = _queues()
    roster = _roster()
    sources = SimpleNamespace(
        resolve_hive=lambda _hive: SimpleNamespace(
            entry={
                "provider": "github",
                "org": "acme",
                "repo": "widgets",
                "prefix": "widget",
            }
        )
    )
    backend = herdr_views.ViewBackend(cfg={}, sources=sources, _roster=roster)
    monkeypatch.setattr(
        backend,
        "hive_facts",
        lambda _hive: (queues, roster, {"ready": {"queue": "ready"}}),
    )
    monkeypatch.setattr(backend, "session_snapshot", lambda: None)
    monkeypatch.setattr(
        backend,
        "dolt_comparison",
        lambda _hive, _entry: {
            "hive": HIVE,
            "ahead": 0,
            "behind": 0,
            "sourceRevision": "dolt-r1",
            "coverage": {"state": "complete", "counts": "known"},
        },
    )
    monkeypatch.setattr(
        herdr_views.worktree,
        "inventory_snapshot_payload",
        lambda **_kwargs: {"worktrees": [], "total": 0, "warnings": []},
    )

    payload = backend.presentation(HIVE)

    assert payload["view"] == "presentation"
    assert payload["scope"] == {"hive": HIVE}


def test_presentation_dolt_comparison_is_one_bounded_read_only_observation(
    monkeypatch,
) -> None:
    calls = []

    class Engine:
        def federation_status(self, path, *, timeout):
            calls.append((Path(path), timeout))
            return FederationStatus(
                ok=True,
                peers=(FederationPeer(peer="origin", reachable=True, ahead=2, behind=0),),
            )

    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "widgets",
        "prefix": "wdg",
    }
    monkeypatch.setattr(herdr_views.engine, "get_engine", lambda _cfg: Engine())
    monkeypatch.setattr(
        herdr_views.registry, "hive_dir", lambda _entry: Path("/managed/acme/widgets")
    )
    backend = herdr_views.ViewBackend(cfg={}, sources=SimpleNamespace())

    comparison = backend.dolt_comparison("github/acme/widgets", entry)

    assert comparison["ahead"] == 2
    assert comparison["behind"] == 0
    assert comparison["coverage"]["state"] == "complete"
    assert calls == [(Path("/managed/acme/widgets"), herdr_views.engine.FEDERATION_TIMEOUT)]


def test_backend_composes_sidebar_metadata_and_exact_crew_topology(monkeypatch) -> None:
    hive = "github/acme/widgets"
    agents = list(json.loads(json.dumps(_crew_fixture())))
    for agent in agents:
        agent["hive"] = hive
        for action in agent["advertised_actions"]:
            action["target"]["hiveId"] = hive
    roster = _crew_roster(*agents)
    queues = _queues()
    snapshot = {"workspaces": [{"workspace_id": "ws-1", "label": f"bh:{hive}"}]}
    sources = SimpleNamespace(
        resolve_hive=lambda requested: SimpleNamespace(
            entry={
                "provider": "github",
                "org": "acme",
                "repo": "widgets",
                "prefix": "wdg",
            }
        )
    )
    backend = herdr_views.ViewBackend(cfg={}, sources=sources, _roster=roster)
    monkeypatch.setattr(
        herdr_views.hive_identity,
        "identity_record",
        lambda _entry: {
            "canonical_id": hive,
            "provider": "github",
            "organization": "acme",
            "repository": "widgets",
            "prefix": "wdg",
            "affiliation": "maintainer",
        },
    )
    monkeypatch.setattr(
        herdr_views.worktree,
        "inventory_snapshot_payload",
        lambda **_kwargs: {
            "source_revision": "inventory-r1",
            "coverage": {"state": "complete"},
            "worktrees": [],
            "total": 0,
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        backend,
        "hive_facts",
        lambda _hive: (queues, roster, {"ready": {"queue": "ready"}}),
    )
    monkeypatch.setattr(backend, "session_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        backend,
        "dolt_comparison",
        lambda _hive, _entry: {
            "hive": hive,
            "ahead": 0,
            "behind": 0,
            "sourceRevision": "dolt-r1",
            "coverage": {"state": "complete", "counts": "known"},
        },
    )

    presentation = backend.presentation(hive)
    crew = backend.crew(hive, limit=200, cursor=None)

    assert presentation["coverage"]["state"] == "complete"
    assert presentation["workspace"]["report"]["tokens"] == {
        "bh_space_title": "[wdg] acme/widgets",
        "bh_affiliation": "maintainer",
        "bh_worktrees": "0",
        "bh_dolt_ahead": "dolt ↑0",
        "bh_dolt_behind": "↓0",
    }
    dispatcher_report = next(
        pane["report"]
        for pane in presentation["panes"]
        if pane["correlation"]["target"] == "dispatcher-1"
    )
    assert dispatcher_report["tokens"]["bh_managed_agents"] == "2"
    assert dispatcher_report["tokens"]["bh_operation"] == "work.dispatch"
    assert crew["coverage"]["state"] == "complete"
    assert [node["target"] for node in crew["roots"]] == [
        "dispatcher-1",
        "developer-direct",
    ]
    assert crew["roots"][0]["children"][0]["safe_removal"]["source_revision"] == "roster-r1"
    assert crew["workspace"]["desired_tabs"][-1] == {
        "role": "direct-agent",
        "target": "developer-direct",
        "label": "Direct Agent",
    }


def test_crew_projects_exact_mixed_forest_layout_and_retirement_offer() -> None:
    payload = herdr_views.crew_payload(
        HIVE,
        _crew_roster(*reversed(_crew_fixture())),
        _crew_snapshot(),
        limit=200,
        generated_at=1000,
    )

    assert payload["source_revision"] == "roster-r1"
    assert payload["coverage"]["state"] == "complete"
    assert payload["freshness"] == {"state": "fresh", "as_of": 1000, "expires_at": 16000}
    assert [node["target"] for node in payload["roots"]] == [
        "dispatcher-1",
        "developer-direct",
    ]
    parent = payload["roots"][0]
    assert parent["relation"] == "root-dispatcher"
    assert [child["target"] for child in parent["children"]] == [
        "developer-b",
        "dispatcher-2",
    ]
    assert parent["stage"] == {
        "desired": True,
        "placement": "right",
        "role": "child-stage",
    }
    assert parent["children"][1]["relation"] == "direct-child-dispatcher"
    assert parent["children"][1]["children"][0]["target"] == "developer-c"
    assert parent["children"][0]["safe_removal"] == {
        "availability": "confirmation-required",
        "reason_code": "operator-confirmation-required",
        "source_revision": "roster-r1",
    }
    assert payload["roots"][1]["relation"] == "direct-agent"
    assert [tab["role"] for tab in payload["workspace"]["desired_tabs"]] == [
        "crew",
        "dispatcher",
        "dispatcher",
        "direct-agent",
    ]
    assert payload["returned"] == 5
    assert payload["truncated"] is False
    jsonschema.validate(payload, SCHEMA)


def test_crew_direct_only_and_reordered_inputs_are_deterministic() -> None:
    direct = _crew_agent("developer-direct", "task-direct")
    direct_only = herdr_views.crew_payload(
        HIVE, _crew_roster(direct), _crew_snapshot(), limit=200, generated_at=1000
    )
    first = herdr_views.crew_payload(
        HIVE, _crew_roster(*_crew_fixture()), _crew_snapshot(), limit=200, generated_at=1000
    )
    second = herdr_views.crew_payload(
        HIVE,
        _crew_roster(*reversed(_crew_fixture())),
        _crew_snapshot(),
        limit=200,
        generated_at=1000,
    )

    assert direct_only["roots"][0]["relation"] == "direct-agent"
    assert direct_only["workspace"]["desired_tabs"] == [
        {"role": "crew", "label": "Crew"},
        {"role": "direct-agent", "target": "developer-direct", "label": "Direct Agent"},
    ]
    assert first == second


@pytest.mark.parametrize("case", ["missing-parent", "cycle", "cross-session", "duplicate"])
def test_crew_structural_and_locator_failures_are_partial_without_navigation_or_removal(
    case: str,
) -> None:
    agents = list(_crew_fixture())
    if case == "missing-parent":
        agents[1]["facts"]["parent"].update({"relation": "direct", "target": "missing-dispatcher"})
    elif case == "cycle":
        agents[0]["facts"]["parent"].update({"relation": "direct", "target": "dispatcher-2"})
    elif case == "cross-session":
        agents[1]["presentation"]["session"] = "default"
    else:
        agents.append(json.loads(json.dumps(agents[1])))

    payload = herdr_views.crew_payload(
        HIVE, _crew_roster(*agents), _crew_snapshot(), limit=200, generated_at=1000
    )
    pending = list(payload["roots"])
    nodes = []
    while pending:
        current = pending.pop()
        nodes.append(current)
        pending.extend(current["children"])

    assert payload["coverage"]["state"] == "partial"
    assert all("safe_removal" not in node for node in nodes)
    assert all(node["safe_actions"] == [] for node in nodes)
    assert all("stage" not in node for node in nodes)
    assert len({node["target"] for node in nodes}) == len(nodes)
    if case == "cross-session":
        child = next(node for node in nodes if node["target"] == "developer-b")
        assert "locator" not in child
        assert child["safe_actions"] == []
    assert payload["diagnostics"]
    jsonschema.validate(payload, SCHEMA)


def test_crew_over_limit_is_partial_atomic_and_has_no_continuation_or_removal() -> None:
    payload = herdr_views.crew_payload(
        HIVE, _crew_roster(*_crew_fixture()), _crew_snapshot(), limit=2, generated_at=1000
    )

    assert payload["coverage"]["state"] == "partial"
    assert payload["returned"] == 2
    assert payload["truncated"] is True
    assert payload["next_cursor"] is None
    assert "safe_removal" not in payload["roots"][0]["children"][0]
    with pytest.raises(OperatorSourceError) as error:
        herdr_views.crew_payload(
            HIVE,
            _crew_roster(*_crew_fixture()),
            _crew_snapshot(),
            limit=200,
            cursor="page-2",
        )
    assert (error.value.code, error.value.status_code) == ("crew_cursor_unsupported", 409)


def test_crew_last_child_absence_only_withdraws_stage_intent() -> None:
    before_agents = [
        _crew_agent("dispatcher-1", "epic-root", role="dispatcher", direct=1, total=1),
        _crew_agent(
            "developer-b",
            "task-b",
            parent="dispatcher-1",
            relation="direct",
            terminal=True,
        ),
    ]
    before = herdr_views.crew_payload(
        HIVE, _crew_roster(*before_agents), _crew_snapshot(), limit=200, generated_at=1000
    )
    after_parent = _crew_agent("dispatcher-1", "epic-root", role="dispatcher", direct=0, total=0)
    after_roster = _crew_roster(after_parent)
    after_roster["revision"] = "roster-r2"
    for action in after_parent["advertised_actions"]:
        action["sourceRevision"] = "roster-r2"
        action["preconditions"]["sourceRevision"] = "roster-r2"
    after = herdr_views.crew_payload(
        HIVE, after_roster, _crew_snapshot(), limit=200, generated_at=2000
    )

    assert before["roots"][0]["stage"]["role"] == "child-stage"
    assert before["roots"][0]["children"][0]["safe_removal"]["source_revision"] == "roster-r1"
    assert after["source_revision"] == "roster-r2"
    assert after["roots"][0]["children"] == []
    assert "stage" not in after["roots"][0]
    assert "close" not in json.dumps(after, sort_keys=True)
    assert after["workspace"]["desired_tabs"][1]["target"] == "dispatcher-1"


def test_deck_disables_launch_when_herdr_cli_preflight_is_unavailable(monkeypatch) -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    beads = state_stream.ProviderSnapshot(
        scope="hive",
        revision="beads-ready",
        as_of=now,
        issues=(
            state_stream.StreamIssue(
                id="widget-ready",
                hive=HIVE,
                issue_type="task",
                status="open",
                priority="P1",
                title="Ready widget",
                updated_at=now,
            ),
        ),
    )
    runtime = AgentRunSnapshot(
        host_id="host-1",
        source_id="runtime-1",
        revision="runtime-ready",
        summaries=(),
        coverage=Coverage.COMPLETE,
        coverage_reason=None,
        freshness=Freshness(state="fresh", as_of=now),
    )

    class Sources:
        cfg = {}

        def resolve_hive(self, hive_id):
            assert hive_id == HIVE
            return SimpleNamespace(entry={})

        def refresh_hive(self, _hive):
            return beads, runtime

        def close(self):
            pass

    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: False)
    backend = herdr_views.ViewBackend(cfg={}, sources=Sources(), _roster=_roster())

    payload = backend.deck(HIVE, limit=20, cursor=None)
    launch = next(
        action for action in payload["actions"] if action["source_action"] == "work-item.launch"
    )

    assert launch["availability"] == "unavailable"
    assert launch["reason_code"] == "herdr_cli_unavailable"
    assert launch["invoke"] is None


def test_bead_disables_launch_when_herdr_cli_preflight_is_unavailable(monkeypatch) -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    beads = state_stream.ProviderSnapshot(
        scope="hive",
        revision="beads-ready",
        as_of=now,
        issues=(
            state_stream.StreamIssue(
                id="widget-ready",
                hive=HIVE,
                issue_type="task",
                status="open",
                priority="P1",
                title="Ready widget",
                updated_at=now,
            ),
        ),
    )
    runtime = AgentRunSnapshot(
        host_id="host-1",
        source_id="runtime-1",
        revision="runtime-ready",
        summaries=(),
        coverage=Coverage.COMPLETE,
        coverage_reason=None,
        freshness=Freshness(state="fresh", as_of=now),
    )

    class Sources:
        def resolve_hive(self, hive_id):
            assert hive_id == HIVE
            return SimpleNamespace(entry={})

        def refresh_hive(self, _hive):
            return beads, runtime

        def close(self):
            pass

    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: False)
    backend = herdr_views.ViewBackend(cfg={}, sources=Sources(), _roster=_roster())

    payload = backend.bead(HIVE, "widget-ready")
    launch = next(
        action for action in payload["actions"] if action["source_action"] == "work-item.launch"
    )

    assert launch["availability"] == "unavailable"
    assert launch["reason_code"] == "herdr_cli_unavailable"
    assert launch["invoke"] is None


def test_launch_preflight_requires_configured_kind_integration(monkeypatch) -> None:
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["codex"])
    monkeypatch.setattr(herdr_views.config, "herdr_kind", lambda _cfg, _entry: "codex")
    monkeypatch.setattr(herdr_plugin, "_integration_ready", lambda _kind: (False, "not installed"))
    backend = herdr_views.ViewBackend(cfg={}, sources=SimpleNamespace(), _roster=_roster())

    preflight = backend.launch_preflight(HIVE, {})

    assert preflight["availability"] == "unavailable"
    assert preflight["reasonCode"] == "herdr_integration_unavailable"


def test_launch_preflight_requires_authoritative_supervisor_session(monkeypatch) -> None:
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["codex"])
    monkeypatch.setattr(herdr_views.config, "herdr_kind", lambda _cfg, _entry: "codex")
    monkeypatch.setattr(herdr_plugin, "_integration_ready", lambda _kind: (True, "installed"))
    backend = herdr_views.ViewBackend(
        cfg={},
        sources=SimpleNamespace(),
        _roster={"revision": "unavailable", "agents": [], "warnings": []},
    )

    preflight = backend.launch_preflight(HIVE, {})

    assert preflight["availability"] == "unavailable"
    assert preflight["reasonCode"] == "herdr_session_unavailable"


def test_launch_preflight_forbids_an_active_foreign_host_lease(monkeypatch) -> None:
    from beadhive import guard

    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["codex"])
    monkeypatch.setattr(herdr_views.config, "herdr_kind", lambda _cfg, _entry: "codex")
    monkeypatch.setattr(herdr_plugin, "_integration_ready", lambda _kind: (True, "installed"))
    lease = SimpleNamespace(held_by=lambda _host: False, is_expired=lambda: False)
    monkeypatch.setattr(guard, "primary_state", lambda *_args, **_kwargs: ("wdg", "host-2", lease))
    monkeypatch.setattr(herdr_views.host, "host_id", lambda: "host-1")
    backend = herdr_views.ViewBackend(cfg={}, sources=SimpleNamespace(), _roster=_roster())

    preflight = backend.launch_preflight(HIVE, {})

    assert preflight["availability"] == "forbidden"
    assert preflight["reasonCode"] == "active_foreign_host_lease"


def test_degraded_sources_are_explicit_and_do_not_fabricate_agent_counts() -> None:
    unavailable = {
        "revision": "unavailable",
        "observed_at": None,
        "agents": [],
        "warnings": ["Herdr unavailable\ntry later"],
    }
    picker = herdr_views.picker_payload([_summary()], unavailable, limit=10, cursor=None)
    deck = herdr_views.deck_payload(HIVE, _queues(), unavailable, limit=10, cursor=None)

    assert picker["coverage"]["state"] == "partial"
    assert picker["coverage"]["sources"]["agents"]["state"] == "unavailable"
    assert picker["rows"][0]["counts"]["running"] is None
    assert "running ?" in picker["rows"][0]["secondary"]
    assert deck["coverage"]["state"] == "partial"
    assert deck["coverage"]["sources"]["agents"]["state"] == "unavailable"
    assert picker["warnings"] == ["Herdr unavailable try later"]
    launch = next(
        action for action in deck["actions"] if action["source_action"] == "work-item.launch"
    )
    assert launch["availability"] == "unavailable"
    assert launch["reason_code"] == "herdr_session_unavailable"
    assert launch["invoke"] is None


def test_eight_view_commands_are_registered_and_layout_emits_json() -> None:
    help_result = runner.invoke(app, ["plugin", "herdr", "view", "--help"])
    assert help_result.exit_code == 0, help_result.output
    for command in (
        "picker",
        "deck",
        "bead",
        "agent",
        "crew",
        "layout",
        "presentation",
        "stream",
    ):
        assert command in help_result.output

    layout = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "view",
            "layout",
            "--hive",
            HIVE,
            "--context-json",
            '{"width":60,"height":24}',
            "--json",
        ],
    )
    assert layout.exit_code == 0, layout.output
    assert json.loads(layout.output)["layout"]["surfaces"]["deck"]["variant"] == "narrow"
