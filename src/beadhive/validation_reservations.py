"""Default per-key validation reservations and the grant environment (bh-9oc32).

The numbers are the measured profiles of ``bh-5mdar.2`` (``key-profiles.measured.json``, recorded
in the validation broker ADR, "Default reservations"): ``cpu_max`` is the measured speedup knee,
memory is cgroup anon+shmem at ``cpu_max`` with headroom *including* tmpfs scratch, and
``dolt-servers`` is the peak concurrent ``dolt sql-server`` count (``bd-cli`` needs one per
worker). Expected wall is the measured median; no ledger duration reader exists yet, so the table
is the only source.

``work.attest.keys[].resources`` REPLACES a key's default wholesale. An unknown key resolves to
:data:`FALLBACK`, never an error, so a new catalog key admits conservatively instead of failing.

``integration`` reserves up to 16 CPUs (bh-itn9m), the recipe's fixed worker count
(``integration_workers``), so under the local engine it runs the same ``-n`` it runs under slots.
It used to stop at ``cpu_max`` 8 (the bh-5mdar.2 knee, measured on a smaller selection) and ran
~780 s side by side against ~520 s alone at 16 workers: submitted in catalog order, ``stateful``
flexed to 16 first. It is now the gate's laggard (expected 519 s vs stateful's 289 s, both
measured alone at 16 workers on main 96188a28) and ``run_group`` submits it first (laggard-first
submission), so it flexes to its whole 16 before shorter keys claim CPU; ``stateful`` (8..16)
still gets at least its ``cpu_min`` 8 beside it on a 32-CPU host. integration's ``cpu_min`` 8 is
the same watchdog floor as stateful's.

``integration`` also takes its Dolt concurrency from the grant (bh-itn9m, bh-0pjeb D4):
``test-integration-land`` sets ``BH_DOLT_SLOTS`` from ``BH_GRANT_DOLT_SERVERS`` when a grant is
present; without one (the slots path) ``BH_DOLT_SLOTS`` is untouched. Its ``dolt-servers``
reservation is 16, one per worker, past the ~455 s floor that 12 slots set. Memory: a test Dolt
``sql-server`` peaked at ~200 MiB RSS (measured 2026-10-14 on ``test_epoch_fencing_int``), so 16
slots hold ~3.2 GiB (6.4 GiB if every slot ran two servers, as the backup round trip does). The
reservation is 7.5 GiB: 2.5 GiB measured at 8 workers, doubled for 16, plus 12 extra slots x
200 MiB. The budget's ``dolt-servers`` pool default (28) is the sum of the catalog's
reservations, so the pool alone never serialises one gate; memory admission bounds the host.

``stateful`` keeps ``cpu_min`` 8 (the ADR's floor option) rather than scaling
``BH_TEST_TIMEOUT_SECONDS``: at 4 workers it took 1,042 s, past the 900 s watchdog.

The grant reaches a key only through :func:`grant_environ`: a clean, allow-listed environment plus
the grant variables. It is never part of a key's command, so it can never enter ``cmd_hash``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from beadhive_validation_broker import Grant, Reservation

from .config_work_settings import attest_config

__all__ = [
    "DEFAULT_EXPECTED_SECONDS",
    "DEFAULT_RESERVATIONS",
    "ENV_ALLOW_NAMES",
    "ENV_ALLOW_PREFIXES",
    "EXPECTED_FALLBACK_SECONDS",
    "FALLBACK",
    "expected_seconds",
    "grant_environ",
    "resolve_reservation",
]

_MIB = 1024 * 1024

#: The shape for a key with neither a default nor an override: modest, so it neither starves nor
#: monopolises. ``hq-storage`` is deliberately in this class: it has no measured profile yet.
FALLBACK = Reservation(cpu_min=2, cpu_max=8, memory_bytes=1024 * _MIB)
EXPECTED_FALLBACK_SECONDS = 60.0


def _res(cpu_min: int, cpu_max: int, memory_mb: int, **pools: int) -> Reservation:
    named = {name.replace("_", "-"): amount for name, amount in pools.items()}
    return Reservation(cpu_min, cpu_max, memory_mb * _MIB, named)


#: Every key of the live attest catalog (scripts/check_attest_catalog.KEY_RECIPES).
DEFAULT_RESERVATIONS: Mapping[str, Reservation] = {
    "stateful": _res(8, 16, 8704, dolt_servers=2),
    "integration": _res(8, 16, 7680, dolt_servers=16),
    "demos": _res(1, 2, 512),
    "root-composition": _res(4, 8, 1024),
    "bd-cli": _res(8, 16, 2253, dolt_servers=10),
    "packages": _res(2, 8, 512),
    "architecture-contracts": _res(1, 1, 256),
    "docs": _res(1, 2, 256),
    "unit": _res(1, 1, 256),
    "hq-storage": FALLBACK,
}

#: Median wall seconds at ``cpu_max`` (bh-5mdar.2; integration and stateful re-measured alone at
#: 16 workers for bh-itn9m).
DEFAULT_EXPECTED_SECONDS: Mapping[str, float] = {
    "stateful": 289.0,
    "integration": 519.0,
    "demos": 161.0,
    "root-composition": 51.0,
    "bd-cli": 33.0,
    "packages": 23.0,
    "architecture-contracts": 102.0,
    "docs": 8.0,
    "unit": 2.0,
}


def _override(cfg: dict, entry, key: str):
    """The key's configured ``resources``, or None. An unreadable catalog yields no override."""
    for configured in attest_config(cfg, entry).keys:
        if configured.name == key:
            return configured.resources
    return None


def resolve_reservation(key: str, cfg: dict | None = None, entry=None) -> Reservation:
    """The reservation for *key*: its configured override, else its default, else the fallback."""
    override = _override(cfg or {}, entry, key)
    if override is not None:
        return Reservation(
            override.cpu_min,
            override.cpu_max,
            override.memory_mb * _MIB,
            dict(override.pools),
        )
    return DEFAULT_RESERVATIONS.get(key, FALLBACK)


def expected_seconds(key: str, cfg: dict | None = None, entry=None) -> float:
    """Expected wall for *key*: its override, else the measured median, else the fallback."""
    override = _override(cfg or {}, entry, key)
    if override is not None and override.expected_s is not None:
        return override.expected_s
    return DEFAULT_EXPECTED_SECONDS.get(key, EXPECTED_FALLBACK_SECONDS)


#: What a key may inherit. Everything else (notably ``VIRTUAL_ENV``, which broke three keys in
#: the sizing campaign, and credentials) is dropped.
ENV_ALLOW_NAMES = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TZ", "TMPDIR", "CI"}
)
ENV_ALLOW_PREFIXES = ("LC_", "UV_", "XDG_", "BH_TEST_", "BH_HERMETIC")


def grant_environ(grant: Grant, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A key's environment: the allow-listed part of *base* plus the grant variables.

    Stale ``BH_GRANT_*`` / ``BH_VALIDATION_LEASE`` in *base* never survive; only this grant's do.
    """
    source = os.environ if base is None else base
    env = {
        name: value
        for name, value in source.items()
        if name in ENV_ALLOW_NAMES or name.startswith(ENV_ALLOW_PREFIXES)
    }
    env.update(grant.env())
    return env
