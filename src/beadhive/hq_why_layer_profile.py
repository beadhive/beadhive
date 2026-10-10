"""``bh hq why`` provider: refused trust-profile overrides (bh-bc2rj).

A host-local override (``hq.placement_mode``, ``hive.write_policy``, ``hq.tenure``) that would
loosen the fleet trust profile is refused: the knob keeps the profile's value
(:mod:`beadhive.trust_profiles`). That refusal never blocks a write — the write runs under the
profile — so this layer reports ``ok`` (or ``skipped`` with no override refused) and never
stops the walk. It names the knob, the profile and the one command that applies the looser
value with a signature: publishing the strictest profile that admits it (``bh hq authority
profile-<name> --operator-key <key> --confirm``). It runs last, after the fence.
"""

from __future__ import annotations

from .hq_why import OK, SKIPPED, Layer, Verdict, WhyContext


def _bh() -> str:
    from . import config

    return config.BINARY_ALIAS


def admitting_profile(override, profiles=None) -> str | None:
    """The strictest profile under which `override` is not refused, or ``None``."""
    from . import trust_profiles

    for name in reversed(profiles or trust_profiles.PROFILES):
        if not trust_profiles.resolve(name, [override]).refusals:
            return name
    return None


def check_profile(ctx: WhyContext) -> Verdict:
    from . import hq_authority_enforce

    profile = hq_authority_enforce.trust_profile(ctx.cfg if isinstance(ctx.cfg, dict) else None)
    refusals = profile.refusals
    if not refusals:
        return Verdict("profile", SKIPPED, "ok", f"{profile.doctor_line()}; no override refused")
    first = refusals[0]
    override = first.override
    target = admitting_profile(override)
    command = (
        f"{_bh()} hq authority profile-{target} --operator-key <key> --confirm"
        if target
        else f"{_bh()} config unset {override.key}"
    )
    return Verdict(
        "profile",
        OK,
        "profile_override_refused",
        f"{override.key}={override.value} ({override.source}) is refused by the {profile.name} "
        f"trust profile; {override.knob} stays at the profile's value",
        command,
        {
            "profile": profile.name,
            "knob": override.knob,
            "key": override.key,
            "value": override.value,
            "admitting_profile": target,
            "refused": [outcome.message for outcome in refusals],
        },
    )


LAYERS: tuple[Layer, ...] = (
    Layer("profile", 50, check_profile, "host-local trust-profile overrides the profile refuses"),
)
