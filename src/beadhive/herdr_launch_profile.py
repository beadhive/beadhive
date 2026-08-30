"""Typed launch targets owned by the optional Herdr integration.

Core deliberately exposes only :class:`AgentLaunchProfile`.  This module is the
plugin boundary: importing it opts into Herdr's optimistic-concurrency and pane
targeting contract without making either concept part of the core API.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PositiveInt, StringConstraints, model_validator

from .agent_launch_profile import (
    AgentLaunchProfile,
    AgentLaunchReceipt,
    ResolvedAgentLaunchProfile,
    resolve_agent_launch_profile,
)

HerdrProfileVersion = Literal["1"]
ExactHerdrIdentity = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[^\s\x00-\x1f\x7f]+$",
    ),
]


class HerdrPaneCreateTarget(BaseModel):
    """An exact, revision-fenced location at which Herdr may create a pane."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    herdr_session: ExactHerdrIdentity
    space_id: ExactHerdrIdentity
    after_pane_id: ExactHerdrIdentity
    direction: Literal["right", "down"] = "right"
    focus: bool = False


class HerdrAgentLaunchProfile(AgentLaunchProfile):
    """Versioned core launch request bound to one observed Herdr Space.

    ``pane_id`` means reuse exactly that pane.  ``pane_create`` is an explicit
    split request.  Both alternatives carry enough scope to reject a locator
    copied from another session or Space before the server can be mutated.
    """

    version: HerdrProfileVersion = "1"
    herdr_session: ExactHerdrIdentity
    space_id: ExactHerdrIdentity
    space_revision: ExactHerdrIdentity
    pane_id: ExactHerdrIdentity | None = None
    pane_create: HerdrPaneCreateTarget | None = None
    launch_id: ExactHerdrIdentity | None = None
    operation_id: ExactHerdrIdentity | None = None
    generation: PositiveInt = 1
    launch_target: Literal["developer_leaf", "dispatcher_epic", "planner_session"] | None = None
    session_checkout_id: ExactHerdrIdentity | None = None

    @model_validator(mode="after")
    def validate_exact_target(self) -> HerdrAgentLaunchProfile:
        if (self.pane_id is None) == (self.pane_create is None):
            raise ValueError("exactly one of pane_id or pane_create is required")
        create = self.pane_create
        if create is not None:
            if create.herdr_session != self.herdr_session:
                raise ValueError("pane_create belongs to a different Herdr session")
            if create.space_id != self.space_id:
                raise ValueError("pane_create belongs to a different Herdr Space")
        if (self.launch_id is None) != (self.operation_id is None):
            raise ValueError("launch_id and operation_id must be supplied together")
        expected_target = {
            "developer": "developer_leaf",
            "dispatcher": "dispatcher_epic",
            "planner": "planner_session",
        }.get(self.initial_seat)
        if expected_target is None:
            raise ValueError("Herdr managed launch supports developer, dispatcher, or planner")
        if self.launch_target is None:
            object.__setattr__(self, "launch_target", expected_target)
        elif self.launch_target != expected_target:
            raise ValueError("seat conflicts with typed Herdr launch target")
        if self.launch_target == "planner_session":
            if self.managed_bead or not self.session_checkout_id:
                raise ValueError("planner launch requires a beadless explicit session checkout")
        elif self.session_checkout_id is not None:
            raise ValueError("session checkout is reserved for beadless planner launch")
        return self


class HerdrAgentLaunchReceipt(BaseModel):
    """Typed Herdr extension around an independently valid core receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_type: Literal["beadhive.herdr-agent-launch"] = "beadhive.herdr-agent-launch"
    version: HerdrProfileVersion = "1"
    core: AgentLaunchReceipt
    herdr_session: ExactHerdrIdentity
    space_id: ExactHerdrIdentity
    space_revision: ExactHerdrIdentity
    tab_id: ExactHerdrIdentity
    pane_id: ExactHerdrIdentity
    agent_target: ExactHerdrIdentity
    agent_session: ExactHerdrIdentity
    worktree_binding_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    launch_spec_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    seat_contract_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    generation: PositiveInt
    launch_id: ExactHerdrIdentity | None = None
    operation_id: ExactHerdrIdentity | None = None


def launch_spec_digest(resolved: ResolvedAgentLaunchProfile) -> str:
    """Digest the complete local launch specification without exposing it portably."""

    payload = resolved.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"sha256:{digest}"


def worktree_binding_digest(worktree: str | Path) -> str:
    """Return a portable binding for an exact local checkout without exposing its path."""

    canonical = str(Path(worktree).resolve())
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def build_herdr_launch_receipt(
    resolved: ResolvedAgentLaunchProfile,
    profile: HerdrAgentLaunchProfile,
    *,
    pane_id: str,
    agent_target: str,
    worktree: str | Path,
    observation: dict,
) -> HerdrAgentLaunchReceipt:
    """Bind redacted core facts to observed Herdr identity, not requested labels."""

    core = AgentLaunchReceipt.from_resolved(resolved)
    if core.managed_bead != profile.managed_bead or core.bead != profile.bead:
        raise ValueError("resolved launch conflicts with Herdr profile bead identity")
    if core.initial_seat != profile.initial_seat or core.harness != profile.harness:
        raise ValueError("resolved launch conflicts with Herdr profile policy")
    if profile.pane_id is not None and pane_id != profile.pane_id:
        raise ValueError("resolved pane conflicts with exact Herdr reuse target")
    observed = validate_herdr_result_observation(
        profile,
        resolved,
        pane_id=pane_id,
        agent_target=agent_target,
        worktree=worktree,
        snapshot=observation,
    )
    observed_session = observation.get("session", observation.get("session_name"))
    observed_revision = observation.get("revision")
    return HerdrAgentLaunchReceipt(
        core=core,
        herdr_session=observed_session,
        space_id=profile.space_id,
        space_revision=observed_revision,
        tab_id=observed["tab_id"],
        pane_id=pane_id,
        agent_target=agent_target,
        agent_session=observed["agent_session"],
        worktree_binding_digest=worktree_binding_digest(worktree),
        launch_spec_digest=launch_spec_digest(resolved),
        seat_contract_digest=resolved.seat_contract_digest,
        generation=profile.generation,
        launch_id=profile.launch_id,
        operation_id=profile.operation_id,
    )


def validate_herdr_result_observation(
    profile: HerdrAgentLaunchProfile,
    resolved: ResolvedAgentLaunchProfile,
    *,
    pane_id: str,
    agent_target: str,
    worktree: str | Path,
    snapshot: dict,
) -> dict[str, str]:
    """Correlate one actual launch result against a fresh complete observation.

    Unlike the pre-mutation fence, the resulting Space revision may legitimately
    have advanced.  The receipt therefore takes that revision from this snapshot,
    but only after the exact session, Space, and uniquely observed result pane have
    all been proved.
    """

    session = snapshot.get("session", snapshot.get("session_name"))
    if session != profile.herdr_session:
        raise ValueError("result belongs to a different Herdr session")
    revision = snapshot.get("revision")
    if not isinstance(revision, str) or not revision:
        raise ValueError("result observation has no authoritative Herdr Space revision")
    spaces = snapshot.get("spaces", snapshot.get("workspaces"))
    if not isinstance(spaces, list):
        raise ValueError("result observation does not contain a complete Space inventory")
    space_matches = [
        item
        for item in spaces
        if isinstance(item, dict)
        and item.get("space_id", item.get("workspace_id", item.get("id"))) == profile.space_id
    ]
    if len(space_matches) != 1:
        raise ValueError("result Herdr Space correlation is missing or ambiguous")
    if profile.pane_id is not None and pane_id != profile.pane_id:
        raise ValueError("resolved pane conflicts with exact Herdr reuse target")
    panes = snapshot.get("panes")
    if not isinstance(panes, list):
        raise ValueError("result observation does not contain a complete pane inventory")
    pane_matches = [
        item
        for item in panes
        if isinstance(item, dict) and item.get("pane_id", item.get("id")) == pane_id
    ]
    if len(pane_matches) != 1:
        raise ValueError("result Herdr pane correlation is missing or ambiguous")
    pane_space = pane_matches[0].get("space_id", pane_matches[0].get("workspace_id"))
    if pane_space != profile.space_id:
        raise ValueError("result Herdr pane belongs to a different Space")
    tab_id = pane_matches[0].get("tab_id")
    if not isinstance(tab_id, str) or not tab_id:
        raise ValueError("result Herdr pane has no observed tab identity")
    tabs = snapshot.get("tabs")
    if (
        not isinstance(tabs, list)
        or len(
            [
                item
                for item in tabs
                if isinstance(item, dict) and item.get("tab_id", item.get("id")) == tab_id
            ]
        )
        != 1
    ):
        raise ValueError("result Herdr tab correlation is missing or ambiguous")

    agents = snapshot.get("agents")
    if not isinstance(agents, list):
        raise ValueError("result observation does not contain a complete Agent inventory")
    agent_matches = []
    for item in agents:
        if not isinstance(item, dict):
            continue
        record = {**item, **(item.get("agent") if isinstance(item.get("agent"), dict) else {})}
        target = record.get("name", record.get("agent_name", record.get("target")))
        record_pane = record.get("pane_id")
        if record_pane is None and isinstance(record.get("pane"), dict):
            record_pane = record["pane"].get("pane_id", record["pane"].get("id"))
        if target == agent_target and record_pane == pane_id:
            agent_matches.append(record)
    if len(agent_matches) != 1:
        raise ValueError("result Herdr Agent correlation is missing or ambiguous")
    agent = agent_matches[0]
    agent_session = agent.get(
        "agent_session_id", agent.get("agent_session", agent.get("session_id"))
    )
    if not isinstance(agent_session, str) or not agent_session:
        raise ValueError("result Herdr Agent has no observed Agent session identity")
    cwd = agent.get("cwd", agent.get("working_directory", agent.get("current_dir")))
    if not isinstance(cwd, str) or worktree_binding_digest(cwd) != worktree_binding_digest(
        worktree
    ):
        raise ValueError("result Herdr Agent worktree binding conflicts with checkout")
    tokens: dict[str, str] = {}
    for source in (pane_matches[0], agent):
        raw = source.get("tokens")
        if isinstance(raw, dict):
            tokens.update({str(key): str(value) for key, value in raw.items()})
    expected = {
        "bh_generation": str(profile.generation),
        "bh_launch_spec_digest": launch_spec_digest(resolved),
        "bh_seat_contract_digest": resolved.seat_contract_digest,
    }
    if profile.launch_id is not None:
        expected["bh_launch_id"] = profile.launch_id
        expected["bh_operation_id"] = profile.operation_id or ""
    conflicts = [key for key, value in expected.items() if tokens.get(key) != value]
    if conflicts:
        raise ValueError(
            "result Herdr Agent conflicts with requested operation/profile/generation: "
            + ", ".join(conflicts)
        )
    return {"tab_id": tab_id, "agent_session": agent_session}


def parse_herdr_launch_receipt(payload: str | bytes | dict) -> HerdrAgentLaunchReceipt:
    """Strictly parse the Herdr extension without weakening core validation."""

    if isinstance(payload, (str, bytes)):
        return HerdrAgentLaunchReceipt.model_validate_json(payload)
    return HerdrAgentLaunchReceipt.model_validate(payload)


def validate_herdr_receipt_observation(receipt: HerdrAgentLaunchReceipt, snapshot: dict) -> None:
    """Verify a receipt against one authoritative Herdr observation.

    Shape validation alone cannot establish freshness or correlation. Consumers must
    use this fence (or ``consume_herdr_launch_receipt``) before treating a receipt as
    managed presentation evidence.
    """

    session = snapshot.get("session", snapshot.get("session_name"))
    if session != receipt.herdr_session:
        raise ValueError("receipt belongs to a different Herdr session")
    if snapshot.get("revision") != receipt.space_revision:
        raise ValueError("receipt Herdr Space revision is stale")
    spaces = snapshot.get("spaces", snapshot.get("workspaces"))
    if not isinstance(spaces, list):
        raise ValueError("observation does not contain a complete Space inventory")
    space_matches = [
        item
        for item in spaces
        if isinstance(item, dict)
        and item.get("space_id", item.get("workspace_id", item.get("id"))) == receipt.space_id
    ]
    if len(space_matches) != 1:
        raise ValueError("receipt Herdr Space correlation is missing or ambiguous")
    panes = snapshot.get("panes")
    if not isinstance(panes, list):
        raise ValueError("observation does not contain a complete pane inventory")
    pane_matches = [
        item
        for item in panes
        if isinstance(item, dict) and item.get("pane_id", item.get("id")) == receipt.pane_id
    ]
    if len(pane_matches) != 1:
        raise ValueError("receipt Herdr pane correlation is missing or ambiguous")
    pane_space = pane_matches[0].get("space_id", pane_matches[0].get("workspace_id"))
    if pane_space != receipt.space_id:
        raise ValueError("receipt Herdr pane belongs to a different Space")
    if pane_matches[0].get("tab_id") != receipt.tab_id:
        raise ValueError("receipt Herdr tab correlation is stale")
    tabs = snapshot.get("tabs")
    if (
        not isinstance(tabs, list)
        or len(
            [
                item
                for item in tabs
                if isinstance(item, dict) and item.get("tab_id", item.get("id")) == receipt.tab_id
            ]
        )
        != 1
    ):
        raise ValueError("receipt Herdr tab correlation is missing or ambiguous")
    agents = snapshot.get("agents")
    if not isinstance(agents, list):
        raise ValueError("observation does not contain a complete Agent inventory")
    matches = []
    for item in agents:
        if not isinstance(item, dict):
            continue
        record = {**item, **(item.get("agent") if isinstance(item.get("agent"), dict) else {})}
        target = record.get("name", record.get("agent_name", record.get("target")))
        record_pane = record.get("pane_id")
        if record_pane is None and isinstance(record.get("pane"), dict):
            record_pane = record["pane"].get("pane_id", record["pane"].get("id"))
        if target == receipt.agent_target and record_pane == receipt.pane_id:
            matches.append(record)
    if len(matches) != 1:
        raise ValueError("receipt Herdr Agent correlation is missing or ambiguous")
    agent = matches[0]
    observed_agent_session = agent.get(
        "agent_session_id", agent.get("agent_session", agent.get("session_id"))
    )
    if observed_agent_session != receipt.agent_session:
        raise ValueError("receipt Herdr Agent session correlation is stale")
    cwd = agent.get("cwd", agent.get("working_directory", agent.get("current_dir")))
    if not isinstance(cwd, str) or worktree_binding_digest(cwd) != receipt.worktree_binding_digest:
        raise ValueError("receipt worktree binding is stale")
    tokens: dict[str, str] = {}
    for source in (pane_matches[0], agent):
        raw = source.get("tokens")
        if isinstance(raw, dict):
            tokens.update({str(key): str(value) for key, value in raw.items()})
    expected = {
        "bh_generation": str(receipt.generation),
        "bh_launch_spec_digest": receipt.launch_spec_digest,
        "bh_seat_contract_digest": receipt.seat_contract_digest,
    }
    if receipt.launch_id is not None:
        expected["bh_launch_id"] = receipt.launch_id
        expected["bh_operation_id"] = receipt.operation_id or ""
    if any(tokens.get(key) != value for key, value in expected.items()):
        raise ValueError("receipt Herdr Agent launch metadata is stale")


def consume_herdr_launch_receipt(
    payload: str | bytes | dict, snapshot: dict
) -> HerdrAgentLaunchReceipt:
    """Parse and observation-fence an extended receipt in one fail-closed operation."""

    receipt = parse_herdr_launch_receipt(payload)
    validate_herdr_receipt_observation(receipt, snapshot)
    return receipt


def resolve_herdr_launch_profile(profile: HerdrAgentLaunchProfile):
    """Resolve core policy first, returning it alongside the immutable target.

    The explicit type check is intentional: callers must not accidentally pass
    a core-only profile and let an adapter invent a presentation target.
    """

    if type(profile) is not HerdrAgentLaunchProfile:
        raise TypeError("Herdr launch requires HerdrAgentLaunchProfile")
    return resolve_agent_launch_profile(profile), profile


def validate_herdr_observation(profile: HerdrAgentLaunchProfile, snapshot: dict) -> None:
    """Fence a launch against the exact session snapshot supplied by Herdr.

    Herdr snapshots have used both ``spaces`` and the earlier ``workspaces``
    spelling.  IDs are authoritative; labels are deliberately ignored.
    """

    session = snapshot.get("session", snapshot.get("session_name"))
    revision = snapshot.get("revision")
    if session != profile.herdr_session:
        raise ValueError("snapshot belongs to a different Herdr session")
    if revision != profile.space_revision:
        raise ValueError("Herdr Space revision changed")
    spaces = snapshot.get("spaces", snapshot.get("workspaces"))
    if not isinstance(spaces, list):
        raise ValueError("snapshot does not contain a complete Space inventory")
    matches = [
        item
        for item in spaces
        if isinstance(item, dict)
        and item.get("space_id", item.get("workspace_id", item.get("id"))) == profile.space_id
    ]
    if len(matches) != 1:
        raise ValueError("Herdr Space target is missing or ambiguous")

    pane_id = profile.pane_id or profile.pane_create.after_pane_id  # type: ignore[union-attr]
    panes = snapshot.get("panes")
    if not isinstance(panes, list):
        raise ValueError("snapshot does not contain a complete pane inventory")
    pane_matches = [
        item
        for item in panes
        if isinstance(item, dict) and item.get("pane_id", item.get("id")) == pane_id
    ]
    if len(pane_matches) != 1:
        raise ValueError("Herdr pane target is missing or ambiguous")
    pane_space = pane_matches[0].get("space_id", pane_matches[0].get("workspace_id"))
    if pane_space != profile.space_id:
        raise ValueError("Herdr pane belongs to a different Space")
