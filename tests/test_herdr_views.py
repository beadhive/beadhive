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
    operator_work_items,
    state_stream,
)
from beadhive.agent_run_summary import Freshness
from beadhive.cli import app
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
    ("width", "variant", "mode", "inspector"),
    [
        (60, "narrow", "single-list", "overlay"),
        (100, "medium", "tabs", "below"),
        (140, "wide", "columns", "below"),
    ],
)
def test_layout_has_deterministic_supervisor_roles_and_popup_split_semantics(
    width: int, variant: str, mode: str, inspector: str
) -> None:
    payload = herdr_views.layout_payload(HIVE, {"width": width, "height": 40})
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
    assert layout["surfaces"]["picker"]["placement"] == "popup"
    assert layout["surfaces"]["agent_actions"]["pane_id"] is None
    tray = layout["surfaces"]["activity_tray"]
    assert (tray["placement"], tray["hide_behavior"], tray["show_behavior"]) == (
        "split",
        "close",
        "reopen-split",
    )
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


def test_six_view_commands_are_registered_and_layout_emits_json() -> None:
    help_result = runner.invoke(app, ["plugin", "herdr", "view", "--help"])
    assert help_result.exit_code == 0, help_result.output
    for command in ("picker", "deck", "bead", "agent", "layout", "stream"):
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
