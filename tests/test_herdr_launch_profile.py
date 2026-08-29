"""Exact targeting contract at the optional Herdr boundary."""

import pytest
from pydantic import ValidationError

from beadhive.agent_launch_profile import AgentLaunchProfile
from beadhive.herdr_launch_profile import (
    HerdrAgentLaunchProfile,
    HerdrPaneCreateTarget,
    resolve_herdr_launch_profile,
    validate_herdr_observation,
)


def _base(**changes):
    values = {
        "managed_bead": True,
        "bead": "bh-123",
        "initial_seat": "developer",
        "harness": "codex",
        "herdr_session": "team-a",
        "space_id": "space-7",
        "space_revision": "rev-4",
        "pane_id": "pane-2",
    }
    values.update(changes)
    return values


def _snapshot(**changes):
    value = {
        "session": "team-a",
        "revision": "rev-4",
        "spaces": [{"space_id": "space-7"}],
        "panes": [{"pane_id": "pane-2", "space_id": "space-7"}],
    }
    value.update(changes)
    return value


def test_extension_inherits_core_validation_and_resolves_core_policy():
    profile = HerdrAgentLaunchProfile(**_base())
    resolved, exact = resolve_herdr_launch_profile(profile)
    assert isinstance(profile, AgentLaunchProfile)
    assert resolved.argv == ("codex",)
    assert exact is profile
    with pytest.raises(ValidationError, match="requires a managed bead"):
        HerdrAgentLaunchProfile(**_base(managed_bead=False, bead=None))


def test_target_requires_exactly_one_complete_alternative():
    with pytest.raises(ValidationError, match="exactly one"):
        HerdrAgentLaunchProfile(**_base(pane_id=None))
    create = HerdrPaneCreateTarget(
        herdr_session="team-a", space_id="space-7", after_pane_id="pane-2"
    )
    with pytest.raises(ValidationError, match="exactly one"):
        HerdrAgentLaunchProfile(**_base(pane_create=create))


@pytest.mark.parametrize(
    "create, message",
    [
        (
            HerdrPaneCreateTarget(
                herdr_session="team-b", space_id="space-7", after_pane_id="pane-2"
            ),
            "different Herdr session",
        ),
        (
            HerdrPaneCreateTarget(
                herdr_session="team-a", space_id="space-8", after_pane_id="pane-2"
            ),
            "different Herdr Space",
        ),
    ],
)
def test_create_target_cannot_cross_scope(create, message):
    with pytest.raises(ValidationError, match=message):
        HerdrAgentLaunchProfile(**_base(pane_id=None, pane_create=create))


def test_observation_accepts_reuse_and_create_targets():
    reuse = HerdrAgentLaunchProfile(**_base())
    validate_herdr_observation(reuse, _snapshot())
    create = HerdrAgentLaunchProfile(
        **_base(
            pane_id=None,
            pane_create={
                "herdr_session": "team-a",
                "space_id": "space-7",
                "after_pane_id": "pane-2",
                "direction": "down",
            },
        )
    )
    validate_herdr_observation(create, _snapshot())


@pytest.mark.parametrize(
    "snapshot, message",
    [
        (_snapshot(session="team-b"), "different Herdr session"),
        (_snapshot(revision="rev-5"), "revision changed"),
        (_snapshot(spaces=[]), "missing or ambiguous"),
        (
            _snapshot(panes=[{"pane_id": "pane-2", "space_id": "space-8"}]),
            "different Space",
        ),
    ],
)
def test_stale_incomplete_and_cross_space_observations_fail_closed(snapshot, message):
    with pytest.raises(ValueError, match=message):
        validate_herdr_observation(HerdrAgentLaunchProfile(**_base()), snapshot)


def test_plain_core_profile_is_not_an_implicit_herdr_request():
    core = AgentLaunchProfile(
        managed_bead=True, bead="bh-123", initial_seat="developer", harness="codex"
    )
    with pytest.raises(TypeError, match="HerdrAgentLaunchProfile"):
        resolve_herdr_launch_profile(core)  # type: ignore[arg-type]
