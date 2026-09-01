"""Typed parsing and joining of Herdr's live topology/roster responses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .transport import decode_protocol
from .transport_types import FailureCode, HerdrResult


class Coverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class AgentRecord:
    target: str
    state: str
    pane_id: str | None = None
    workspace_id: str | None = None
    tab_id: str | None = None
    cwd: str | None = None
    session: str | None = None
    tokens: dict[str, str] | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TopologySnapshot:
    session: str
    revision: str
    spaces: tuple[dict[str, Any], ...]
    tabs: tuple[dict[str, Any], ...]
    panes: tuple[dict[str, Any], ...]
    agents: tuple[AgentRecord, ...]
    coverage: Coverage = Coverage.COMPLETE
    raw: dict[str, Any] | None = None


def _record_pane_id(record: dict[str, Any]) -> str | None:
    pane = record.get("pane")
    pane_id = record.get("pane_id")
    if isinstance(pane, str):
        pane_id = pane
    elif isinstance(pane, dict):
        pane_id = pane.get("id") or pane.get("pane_id") or pane_id
        nested = pane.get("pane")
        if not pane_id and isinstance(nested, dict):
            pane_id = nested.get("id") or nested.get("pane_id")
    return pane_id if isinstance(pane_id, str) and pane_id.strip() else None


def agent_records(
    value: Any, *, unique_by_name: bool = True, include_pane_claims: bool = False
) -> list[dict[str, Any]]:
    """Extract agent-shaped records from provider responses for compatibility callers."""

    records: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            nested = item.get("agent")
            candidate = dict(item)
            if isinstance(nested, dict):
                candidate.update(nested)
            name = candidate.get("name") or candidate.get("agent_name") or candidate.get("target")
            state = (
                candidate.get("state")
                or candidate.get("status")
                or candidate.get("agent_status")
                or candidate.get("lifecycle")
                or candidate.get("lifecycle_state")
            )
            is_agent = isinstance(name, str) and isinstance(state, (str, int, float))
            is_pane_claim = include_pane_claims and _record_pane_id(candidate) is not None
            if is_agent or is_pane_claim:
                records.append(candidate)
            for key, child in item.items():
                if key == "agent" and isinstance(nested, dict):
                    continue
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    if not unique_by_name:
        return records
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        name = str(record.get("name") or record.get("agent_name") or record.get("target"))
        unique.setdefault(name, record)
    return list(unique.values())


def join_snapshot_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Join agent records with their exact pane/workspace facts."""

    panes = {
        str(item.get("pane_id") or item.get("id")): item
        for item in snapshot.get("panes", [])
        if isinstance(item, dict) and (item.get("pane_id") or item.get("id"))
    }
    workspace_rows = snapshot.get("workspaces", snapshot.get("spaces", []))
    workspaces = {
        str(item.get("workspace_id") or item.get("space_id") or item.get("id")): item
        for item in workspace_rows
        if isinstance(item, dict)
        and (item.get("workspace_id") or item.get("space_id") or item.get("id"))
    }
    raw = snapshot.get("agents")
    if not isinstance(raw, list):
        raw = agent_records(snapshot, unique_by_name=False)
    records: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        record = dict(item)
        if isinstance(item.get("agent"), dict):
            record.update(item["agent"])
        pane_id = _record_pane_id(record)
        pane = panes.get(pane_id or "", {})
        workspace_id = record.get("workspace_id") or (
            pane.get("workspace_id") or pane.get("space_id") if isinstance(pane, dict) else None
        )
        workspace = workspaces.get(str(workspace_id or ""), {})
        if pane:
            record["pane_record"] = pane
        if workspace:
            record["workspace_record"] = workspace
        records.append(record)
    return records


def _payload(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("snapshot"), dict):
        return value["snapshot"]
    return value


def _records(value: Any, key: str, alias: str | None = None) -> tuple[dict[str, Any], ...] | None:
    rows = value.get(key)
    if rows is None and alias is not None:
        rows = value.get(alias)
    if rows is None:
        return None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise TypeError(f"{key} must be a list of objects")
    return tuple(rows)


def _agent_records(value: Any) -> tuple[AgentRecord, ...]:
    rows = value.get("agents")
    if rows is None:
        return ()
    if not isinstance(rows, list):
        raise TypeError("agents must be a list")
    parsed: list[AgentRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("agent record must be an object")
        nested = row.get("agent")
        merged = {**row, **nested} if isinstance(nested, dict) else dict(row)
        target = merged.get("name") or merged.get("agent_name") or merged.get("target")
        state = (
            merged.get("state")
            or merged.get("status")
            or merged.get("agent_status")
            or merged.get("lifecycle")
        )
        if not isinstance(target, str) or not target or not isinstance(state, str) or not state:
            raise TypeError("agent record requires target and lifecycle state")
        pane = merged.get("pane")
        pane_id = merged.get("pane_id")
        if isinstance(pane, str):
            pane_id = pane
        elif isinstance(pane, dict):
            pane_id = pane.get("pane_id") or pane.get("id") or pane_id
        tokens = merged.get("tokens")
        if tokens is not None and not isinstance(tokens, dict):
            raise TypeError("agent tokens must be an object")
        parsed.append(
            AgentRecord(
                target=target,
                state=state.lower(),
                pane_id=pane_id if isinstance(pane_id, str) else None,
                workspace_id=(
                    merged.get("workspace_id")
                    if isinstance(merged.get("workspace_id"), str)
                    else None
                ),
                tab_id=merged.get("tab_id") if isinstance(merged.get("tab_id"), str) else None,
                cwd=(
                    merged.get("cwd", merged.get("working_directory"))
                    if isinstance(merged.get("cwd", merged.get("working_directory")), str)
                    else None
                ),
                session=(
                    merged.get("session", merged.get("session_name"))
                    if isinstance(merged.get("session", merged.get("session_name")), str)
                    else None
                ),
                tokens=(
                    {str(k): str(v) for k, v in tokens.items()}
                    if isinstance(tokens, dict)
                    else None
                ),
                raw=merged,
            )
        )
    return tuple(parsed)


def parse_snapshot(payload: str | bytes | dict[str, Any]) -> HerdrResult[TopologySnapshot]:
    """Parse a snapshot while retaining honest ``partial`` collection coverage."""

    decoded = decode_protocol(payload)
    if not decoded.is_ok:
        return decoded
    value = _payload(decoded.value)
    if not isinstance(value, dict):
        return HerdrResult.fail(FailureCode.MALFORMED, "Herdr snapshot must be an object")
    session = value.get("session", value.get("session_name"))
    revision = value.get("revision")
    if not isinstance(session, str) or not session or not isinstance(revision, str) or not revision:
        return HerdrResult.fail(FailureCode.MALFORMED, "Herdr snapshot lacks session or revision")
    if value.get("stale") is True or value.get("fresh") is False or value.get("state") == "stale":
        return HerdrResult.fail(FailureCode.STALE, "Herdr topology snapshot is stale")
    try:
        spaces = _records(value, "spaces", "workspaces")
        tabs = _records(value, "tabs")
        panes = _records(value, "panes")
        agents = _agent_records(value)
    except TypeError as exc:
        return HerdrResult.fail(
            FailureCode.MALFORMED,
            "Herdr snapshot has an invalid collection",
            detail=str(exc),
        )
    validation = _validate_links(value, session, spaces, tabs, panes, agents)
    if validation is not None:
        return HerdrResult.fail(validation[0], validation[1])
    missing = (
        any(collection is None for collection in (spaces, tabs, panes)) or "agents" not in value
    )
    return HerdrResult.ok(
        TopologySnapshot(
            session=session,
            revision=revision,
            spaces=spaces or (),
            tabs=tabs or (),
            panes=panes or (),
            agents=agents,
            coverage=Coverage.PARTIAL if missing else Coverage.COMPLETE,
            raw=value,
        )
    )


def _validate_links(
    value: dict[str, Any],
    session: str,
    spaces: tuple[dict[str, Any], ...] | None,
    tabs: tuple[dict[str, Any], ...] | None,
    panes: tuple[dict[str, Any], ...] | None,
    agents: tuple[AgentRecord, ...],
) -> tuple[FailureCode, str] | None:
    """Reject ambiguous or cross-scope joins before callers can prove identity."""

    def identifier(row: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            candidate = row.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    def unique(rows: tuple[dict[str, Any], ...] | None, *keys: str) -> set[str] | None:
        if rows is None:
            return None
        ids = [identifier(row, *keys) for row in rows]
        if any(item is None for item in ids) or len(set(ids)) != len(ids):
            raise ValueError("topology contains duplicate or missing identity")
        return {item for item in ids if item is not None}

    try:
        space_ids = unique(spaces, "space_id", "workspace_id", "id")
        tab_ids = unique(tabs, "tab_id", "id")
        pane_ids = unique(panes, "pane_id", "id")
        tabs_by_id = (
            {
                identifier(row, "tab_id", "id"): row
                for row in tabs or ()
                if identifier(row, "tab_id", "id") is not None
            }
            if tabs is not None
            else {}
        )
        panes_by_id = (
            {
                identifier(row, "pane_id", "id"): row
                for row in panes or ()
                if identifier(row, "pane_id", "id") is not None
            }
            if panes is not None
            else {}
        )
        identity_bearing = (
            spaces is not None and tabs is not None and panes is not None and "agents" in value
        )
        if identity_bearing:
            for row in tabs or ():
                parent = row.get("space_id", row.get("workspace_id"))
                if not isinstance(parent, str) or not parent:
                    return FailureCode.CONFLICT, "tab is missing its Space ancestry"
            for row in panes or ():
                parent_tab = row.get("tab_id")
                parent_space = row.get("space_id", row.get("workspace_id"))
                if not isinstance(parent_tab, str) or not parent_tab:
                    return FailureCode.CONFLICT, "pane is missing its tab ancestry"
                if not isinstance(parent_space, str) or not parent_space:
                    return FailureCode.CONFLICT, "pane is missing its Space ancestry"
            for agent in agents:
                raw = agent.raw or {}
                if not isinstance(agent.pane_id, str) or not agent.pane_id:
                    return FailureCode.CONFLICT, "Agent is missing its pane ancestry"
                if not isinstance(raw.get("tab_id"), str) or not raw["tab_id"]:
                    return FailureCode.CONFLICT, "Agent is missing its tab ancestry"
                agent_space = raw.get("space_id", raw.get("workspace_id"))
                if not isinstance(agent_space, str) or not agent_space:
                    return FailureCode.CONFLICT, "Agent is missing its Space ancestry"
        target_ids = [agent.target for agent in agents]
        if len(set(target_ids)) != len(target_ids):
            return FailureCode.CONFLICT, "topology contains duplicate Agent targets"
        for rows in (spaces, tabs, panes):
            if rows is not None:
                for row in rows:
                    row_session = row.get("session", row.get("session_name"))
                    if row_session is not None and row_session != session:
                        return FailureCode.CONFLICT, "topology record belongs to another session"
        for agent in agents:
            if agent.session is not None and agent.session != session:
                return FailureCode.CONFLICT, "Agent record belongs to another session"
        if spaces is not None and tabs is not None and space_ids is not None:
            for row in tabs:
                parent = row.get("space_id", row.get("workspace_id"))
                if parent is not None and parent not in space_ids:
                    return FailureCode.CONFLICT, "tab references a dangling Space"
        if panes is not None:
            for row in panes:
                parent = row.get("tab_id")
                if tab_ids is not None and parent is not None and parent not in tab_ids:
                    return FailureCode.CONFLICT, "pane references a dangling tab"
                parent_space = row.get("space_id", row.get("workspace_id"))
                if (
                    space_ids is not None
                    and parent_space is not None
                    and parent_space not in space_ids
                ):
                    return FailureCode.CONFLICT, "pane references a dangling Space"
                if tab_ids is not None and parent in tabs_by_id:
                    tab_space = tabs_by_id[parent].get(
                        "space_id", tabs_by_id[parent].get("workspace_id")
                    )
                    if (
                        parent_space is not None
                        and tab_space is not None
                        and parent_space != tab_space
                    ):
                        return FailureCode.CONFLICT, "pane ancestry conflicts with its tab"
        if pane_ids is not None:
            for agent in agents:
                if agent.pane_id is not None and agent.pane_id not in pane_ids:
                    return FailureCode.CONFLICT, "Agent references a dangling pane"
                raw = agent.raw or {}
                declared_tab = raw.get("tab_id")
                declared_space = raw.get("space_id", raw.get("workspace_id"))
                if declared_tab is not None and tab_ids is not None and declared_tab not in tab_ids:
                    return FailureCode.CONFLICT, "Agent references a dangling tab"
                if (
                    declared_space is not None
                    and space_ids is not None
                    and declared_space not in space_ids
                ):
                    return FailureCode.CONFLICT, "Agent references a dangling Space"
                pane = panes_by_id.get(agent.pane_id or "")
                if pane is not None:
                    pane_tab = pane.get("tab_id")
                    pane_space = pane.get("space_id", pane.get("workspace_id"))
                    if (
                        declared_tab is not None
                        and pane_tab is not None
                        and declared_tab != pane_tab
                    ):
                        return FailureCode.CONFLICT, "Agent ancestry conflicts with its pane"
                    if (
                        declared_space is not None
                        and pane_space is not None
                        and declared_space != pane_space
                    ):
                        return FailureCode.CONFLICT, "Agent ancestry conflicts with its pane"
                    if pane_tab is not None and tab_ids is not None and pane_tab in tabs_by_id:
                        tab_space = tabs_by_id[pane_tab].get(
                            "space_id", tabs_by_id[pane_tab].get("workspace_id")
                        )
                        if (
                            pane_space is not None
                            and tab_space is not None
                            and pane_space != tab_space
                        ):
                            return FailureCode.CONFLICT, "pane ancestry conflicts with its tab"
        expected_generation = value.get("generation")
        generations: set[str] = set()
        if expected_generation is not None:
            generations.add(str(expected_generation))
        for agent in agents:
            if agent.tokens and "bh_generation" in agent.tokens:
                generations.add(agent.tokens["bh_generation"])
        if len(generations) > 1:
            return FailureCode.CONFLICT, "topology contains conflicting generations"
    except ValueError as exc:
        return FailureCode.CONFLICT, str(exc)
    return None


def parse_roster(payload: str | bytes | dict[str, Any]) -> HerdrResult[tuple[AgentRecord, ...]]:
    """Parse an agent roster independently of the larger Space topology."""

    decoded = decode_protocol(payload)
    if not decoded.is_ok:
        return decoded
    value = _payload(decoded.value)
    if isinstance(value, list):
        value = {"agents": value}
    if not isinstance(value, dict):
        return HerdrResult.fail(FailureCode.MALFORMED, "Herdr roster must be an object")
    try:
        return HerdrResult.ok(_agent_records(value))
    except TypeError as exc:
        return HerdrResult.fail(
            FailureCode.MALFORMED,
            "Herdr roster has an invalid record",
            detail=str(exc),
        )
