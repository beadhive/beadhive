"""`config.work_identity` resolution — per-crew agent attribution.

Pure resolution over an in-memory cfg dict (no config file, no git): an agent-mode
`work.identity` with a `crews` mapping must layer each crew's overrides (email / signing key /
optional name) over the base identity, so `crew/alice` and `crew/bob` resolve to distinct
signing keys + emails — real ledger attribution. With no crews or an empty actor, behavior is
unchanged.
"""

from __future__ import annotations

from ws import config

_CREWS_CFG = {
    "work": {
        "identity": {
            "mode": "agent",
            "name": "crew/default",
            "email": "agents@test.dev",
            "signing_key": "/keys/default.pub",
            "sign": True,
            "crews": {
                "crew/alice": {"email": "alice@agents.dev", "signing_key": "/keys/alice.pub"},
                "crew/bob": {
                    "name": "crew/bob-bot",
                    "email": "bob@agents.dev",
                    "signing_key": "/keys/bob.pub",
                    "sign": False,
                },
            },
        }
    }
}


def test_per_crew_overrides_layer_over_base():
    alice = config.work_identity(_CREWS_CFG, None, "crew/alice")
    bob = config.work_identity(_CREWS_CFG, None, "crew/bob")

    # alice overrides email + key, inherits base name + sign
    assert alice["email"] == "alice@agents.dev"
    assert alice["signing_key"] == "/keys/alice.pub"
    assert alice["name"] == "crew/default"
    assert alice["sign"] is True
    assert alice["mode"] == "agent"

    # bob overrides name + email + key + sign — distinct from alice and the base
    assert bob["name"] == "crew/bob-bot"
    assert bob["email"] == "bob@agents.dev"
    assert bob["signing_key"] == "/keys/bob.pub"
    assert bob["sign"] is False

    # sibling crews resolve to different keys/emails (lossless attribution)
    assert alice["signing_key"] != bob["signing_key"]
    assert alice["email"] != bob["email"]


def test_empty_actor_resolves_base_identity():
    base = config.work_identity(_CREWS_CFG, None, "")
    assert base["email"] == "agents@test.dev"
    assert base["signing_key"] == "/keys/default.pub"
    assert base["name"] == "crew/default"
    # the crews mapping never leaks into the normalized profile
    assert "crews" not in base


def test_unknown_actor_falls_back_to_base():
    prof = config.work_identity(_CREWS_CFG, None, "crew/nobody")
    assert prof["email"] == "agents@test.dev"
    assert prof["signing_key"] == "/keys/default.pub"


def test_no_crews_behavior_unchanged():
    cfg = {"work": {"identity": {"mode": "agent", "name": "crew/x", "email": "x@a.dev"}}}
    # actor is supplied but there is no crews mapping — base identity is returned verbatim
    assert config.work_identity(cfg, None, "crew/alice") == config.work_identity(cfg, None, "")


def test_per_rig_crews_override_global():
    entry = {
        "work": {
            "identity": {"crews": {"crew/alice": {"signing_key": "/keys/rig-alice.pub"}}}
        }
    }
    prof = config.work_identity(_CREWS_CFG, entry, "crew/alice")
    # per-rig crew key wins; base email still inherited
    assert prof["signing_key"] == "/keys/rig-alice.pub"
    assert prof["email"] == "agents@test.dev"


def test_supervised_when_nothing_configured():
    assert config.work_identity({}, None, "crew/alice")["mode"] == "supervised"
