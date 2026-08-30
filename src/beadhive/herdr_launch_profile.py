"""Typed launch targets owned by the optional Herdr integration.

Core deliberately exposes only :class:`AgentLaunchProfile`.  This module is the
plugin boundary: importing it opts into Herdr's optimistic-concurrency and pane
targeting contract without making either concept part of the core API.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

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
    pane_id: ExactHerdrIdentity
    agent_target: ExactHerdrIdentity
    agent_session: ExactHerdrIdentity
    launch_spec_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


def build_herdr_launch_receipt(
    resolved: ResolvedAgentLaunchProfile,
    profile: HerdrAgentLaunchProfile,
    *,
    pane_id: str,
    agent_target: str,
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
    validate_herdr_result_observation(profile, pane_id=pane_id, snapshot=observation)
    observed_session = observation.get("session", observation.get("session_name"))
    observed_revision = observation.get("revision")
    launch_payload = resolved.model_dump(mode="json")
    launch_digest = hashlib.sha256(
        json.dumps(launch_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return HerdrAgentLaunchReceipt(
        core=core,
        herdr_session=observed_session,
        space_id=profile.space_id,
        space_revision=observed_revision,
        pane_id=pane_id,
        agent_target=agent_target,
        agent_session=observed_session,
        launch_spec_digest=f"sha256:{launch_digest}",
    )


def validate_herdr_result_observation(
    profile: HerdrAgentLaunchProfile, *, pane_id: str, snapshot: dict
) -> None:
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
