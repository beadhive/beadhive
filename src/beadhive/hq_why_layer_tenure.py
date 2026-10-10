"""``bh hq why`` provider: the elected-tenure cache (trust-profiles ADR section 3, bh-ofszz).

On a SQL-HQ host with the cache on, a command reads the configuration snapshot and its
``author`` / ``claim`` decision from the record the host daemon refreshes
(:mod:`beadhive.hq_tenure_cache`). This layer reports that record: which revision it holds, when
it was refreshed and when the tenure ends. It refuses only when the daemon reports HQ
unreachable, the tenure has lapsed and the trust profile does not let the asked capability
continue after the lapse (``publish`` and ``place`` never do; ``author`` and ``claim`` do under
``open`` and ``trusted``) — the same rule the eligibility boundary applies. It runs after the
fence and before the profile layer.
"""

from __future__ import annotations

from .hq_why import OK, REFUSED, SKIPPED, Layer, Verdict, WhyContext


def _bh() -> str:
    from .config_consumer_ports import daemon_settings as config

    return config.BINARY_ALIAS


def check_tenure(ctx: WhyContext) -> Verdict:
    from . import hq_tenure_cache
    from .config_consumer_ports import daemon_settings as config

    host_cfg = config.load_host()
    sql = ((host_cfg.get("hq") or {}).get("sql") or {}) if isinstance(host_cfg, dict) else {}
    if sql.get("enabled") is not True or not hq_tenure_cache.enabled(host_cfg):
        return Verdict("tenure", SKIPPED, "ok", "no elected-tenure cache on this host")
    status = hq_tenure_cache.status(host_cfg)
    if not status["cached"]:
        return Verdict(
            "tenure",
            SKIPPED,
            "ok",
            "no elected-tenure cache record yet; the next command reads HQ once and seeds it",
            detail=status,
        )
    policy = hq_tenure_cache.current_policy(host_cfg)
    lapsed = status["lapsed"] or not policy.offline_grace
    if status["unreachable_since"] and not policy.allows_offline(ctx.capability, lapsed=lapsed):
        return Verdict(
            "tenure",
            REFUSED,
            "tenure_lapsed",
            f"HQ unreachable since {status['unreachable_since']} and the tenure ended at "
            f"{status['expires_at']}: the {policy.profile} trust profile does not let "
            f"{ctx.capability} continue after the lapse",
            f"{_bh()} doctor",
            status,
        )
    return Verdict(
        "tenure",
        OK,
        "ok",
        f"elected-tenure cache holds revision {status['revision']} (refreshed "
        f"{status['refreshed_at']}, tenure ends {status['expires_at']}, profile {policy.profile})",
        detail=status,
    )


LAYERS: tuple[Layer, ...] = (
    Layer("tenure", 45, check_tenure, "the elected-tenure cache's record and lapse rules"),
)
