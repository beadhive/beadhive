"""Exact Herdr session and ownership identity rules."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .transport_types import FailureCode, HerdrResult

_SESSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CURRENT = frozenset({"current", "active"})


@dataclass(frozen=True, slots=True)
class SessionSelection:
    name: str
    requested: str
    current: bool = False


def resolve_session(
    explicit: str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> SessionSelection:
    env = os.environ if environment is None else environment
    requested = explicit if explicit is not None else env.get("BH_HERDR_SESSION", "default")
    requested = requested.strip()
    if not requested:
        raise ValueError("Herdr session must not be empty")
    if requested.lower() in _CURRENT:
        if env.get("HERDR_ENV") != "1" or not env.get("HERDR_PANE_ID", "").strip():
            raise ValueError(
                "current Herdr session requires a Herdr-managed pane "
                "(HERDR_ENV=1 and HERDR_PANE_ID)"
            )
        name = env.get("HERDR_SESSION", "default").strip() or "default"
        current = True
    else:
        name = requested
        current = False
    if name in {".", ".."} or len(name.encode()) > 128 or _SESSION_RE.fullmatch(name) is None:
        raise ValueError(
            "Herdr session name must use ASCII letters, digits, dot, underscore, or dash"
        )
    return SessionSelection(name, requested, current)


class OwnershipMarker(StrEnum):
    MANAGED = "bh.plugin.herdr/v1"


@dataclass(frozen=True, slots=True)
class IdentityExpectation:
    target: str
    pane_id: str | None = None
    workspace_id: str | None = None
    session: str | None = None
    cwd: str | None = None
    tokens: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IdentityProof:
    target: str
    state: str
    tokens: Mapping[str, str]


def _tokens(record: Mapping[str, object]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    sources: list[object] = []
    for key in ("workspace_record", "pane_record", "pane", "workspace", "agent"):
        nested = record.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    sources.append(record)
    for source in sources:
        raw = source.get("tokens") if isinstance(source, Mapping) else None
        if isinstance(raw, Mapping):
            tokens.update({str(key): str(value) for key, value in raw.items() if value is not None})
    return tokens


collect_tokens = _tokens


def _identity_field(record: Mapping[str, object], field: str) -> object:
    nested_key = {"pane_id": "pane", "workspace_id": "workspace"}.get(field)
    actual = record.get(field)
    nested = record.get(nested_key) if nested_key else None
    if isinstance(nested, str) and field == "pane_id":
        actual = nested
    elif isinstance(nested, Mapping):
        actual = nested.get(field) or nested.get("id") or actual
    return actual


def validate_identity(
    record: Mapping[str, object],
    expected: IdentityExpectation,
) -> HerdrResult[IdentityProof]:
    """Prove one exact live target without accepting lookalike labels."""

    target = record.get("name") or record.get("agent_name") or record.get("target")
    if not isinstance(target, str) or not target:
        return HerdrResult.fail(FailureCode.MALFORMED, "Herdr Agent has no exact target identity")
    if target != expected.target:
        return HerdrResult.fail(FailureCode.CONFLICT, "Herdr Agent target identity conflicts")
    state = record.get("state") or record.get("status") or record.get("agent_status")
    if not isinstance(state, str) or not state:
        return HerdrResult.fail(FailureCode.MALFORMED, "Herdr Agent has no lifecycle state")
    state = state.lower()
    tokens = _tokens(record)
    if tokens.get("bh_owner") != OwnershipMarker.MANAGED:
        return HerdrResult.fail(FailureCode.REFUSED, "Herdr Agent is not Beadhive-owned")
    for key, value in expected.tokens.items():
        if tokens.get(key) != value:
            return HerdrResult.fail(FailureCode.CONFLICT, f"Herdr ownership token {key} conflicts")
    for identity_name, value in (
        ("pane_id", expected.pane_id),
        ("workspace_id", expected.workspace_id),
        ("session", expected.session),
        ("cwd", expected.cwd),
    ):
        if value is None:
            continue
        actual = _identity_field(record, identity_name)
        if identity_name == "session":
            actual = record.get("session", record.get("session_name"))
        if identity_name == "cwd":
            actual = record.get("cwd", record.get("working_directory"))
        if actual != value:
            return HerdrResult.fail(
                FailureCode.CONFLICT,
                f"Herdr {identity_name} identity conflicts",
            )
    if state not in {"idle", "working", "blocked", "done"}:
        return HerdrResult.fail(FailureCode.STALE, "Herdr Agent lifecycle identity is stale")
    return HerdrResult.ok(IdentityProof(target, state, tokens))


def validate_generation(
    tokens: Mapping[str, str], expected: Mapping[str, str]
) -> HerdrResult[IdentityProof]:
    """Check operation/profile/generation tokens without interpreting provider state."""

    conflicts = [key for key, value in expected.items() if tokens.get(key) != value]
    if conflicts:
        return HerdrResult.fail(
            FailureCode.STALE,
            "Herdr generation identity is stale or conflicting",
            detail="generation_conflict",
        )
    return HerdrResult.ok(IdentityProof("", "", dict(tokens)))


def validate_live_identity(
    record: Mapping[str, object],
    *,
    target: str,
    hive: str,
    cwd: str | Path,
    live_states: set[str],
) -> HerdrResult[tuple[str, str]]:
    """Prove target, pane, workspace, lifecycle, and checkout identity together."""

    actual_target = record.get("name") or record.get("agent_name") or record.get("target")
    if actual_target != target:
        return HerdrResult.fail(FailureCode.CONFLICT, "Herdr target identity conflicts")
    state = str(
        record.get("state")
        or record.get("status")
        or record.get("agent_status")
        or record.get("lifecycle")
        or "unknown"
    ).lower()
    if state not in live_states:
        return HerdrResult.fail(FailureCode.STALE, "Herdr lifecycle identity is stale")
    pane = record.get("pane")
    pane_record = record.get("pane_record")
    pane_id = record.get("pane_id")
    if isinstance(pane, str):
        pane_id = pane
    elif isinstance(pane, Mapping):
        pane_id = pane.get("pane_id") or pane.get("id") or pane_id
    pane_sources = [record, pane, pane_record]
    pane_name = None
    for source in pane_sources:
        if isinstance(source, Mapping):
            for key in ("name", "label", "title", "pane_name"):
                if isinstance(source.get(key), str) and source[key].strip():
                    pane_name = source[key]
                    break
        if pane_name:
            break
    workspace = record.get("workspace_record") or record.get("workspace")
    workspace_id = record.get("workspace_id")
    workspace_label = record.get("workspace_label")
    if isinstance(workspace, Mapping):
        workspace_id = workspace.get("workspace_id") or workspace.get("id") or workspace_id
        workspace_label = workspace.get("label") or workspace.get("name") or workspace_label
    actual_cwd = None
    for source in (record, pane, pane_record, workspace, record.get("workspace")):
        if isinstance(source, Mapping):
            actual_cwd = source.get("cwd") or source.get("working_directory")
            if actual_cwd:
                break
    if not isinstance(pane_id, str) or not pane_id or pane_name != target:
        return HerdrResult.fail(FailureCode.CONFLICT, "Herdr pane identity conflicts")
    if not isinstance(workspace_id, str) or workspace_label != f"bh:{hive}":
        return HerdrResult.fail(FailureCode.CONFLICT, "Herdr workspace identity conflicts")
    try:
        same_cwd = Path(str(actual_cwd)).resolve() == Path(cwd).resolve()
    except OSError:
        same_cwd = str(actual_cwd) == str(cwd)
    if not same_cwd:
        return HerdrResult.fail(FailureCode.CONFLICT, "Herdr checkout identity conflicts")
    return HerdrResult.ok((workspace_id, pane_id))
