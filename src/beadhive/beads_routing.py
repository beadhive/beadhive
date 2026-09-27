"""The one composition decision for the beadhive-core command cohorts (bh-sy36q.6).

Every migrated work/planning cohort — lifecycle (``assign`` / ``claim`` / ``resume`` / ``abandon``
and submit's bead-state half, :mod:`beadhive.work_lifecycle`), molecule filing
(:mod:`beadhive.plan_filing`), ready/schedule/claim-next (:mod:`beadhive.work_queue`), and molecule
progress / swarm / dispatch polling / local-loop state (:mod:`beadhive.dispatch_state`) — opens its
Beads session HERE, and nowhere else. Two things are decided in this one place:

* **Default: beadhive-core.** Each seam asks :func:`hive_session` for an unopened session against
  the hive's one supervised Beads service (``bh host beads``) and, when it opens, serves the
  command through the ``beadhive_core`` handler. Nothing here ever starts ``bd serve``.
* **Pre-execution CLI-compatibility selection.** When no capable session can be opened — no
  service running, an embedded-Dolt hive Beads 1.3 cannot serve, a missing capability, a synthetic
  ``entry`` that cannot address a hive — :func:`hive_session` raises
  :class:`beadhive_core.SessionUnavailable` (or the client's ``ServiceError`` /
  ``IncompatibleService``, see :func:`unavailable_errors`) BEFORE the first Beads operation, and the
  seam selects its named ``bd`` route for the whole command. This is retained deliberately (the
  bh-sy36q.6 decision, see ``packages/beadhive-core/README.md``): embedded-Dolt hives cannot be
  served by Beads 1.3 at all, ``claim`` starts every developer loop, and ``abandon`` is the
  stall-recovery path, so these commands must not fail closed on a hive the API cannot serve.
  It is never a retry: once the API route is selected, a failing call is reported, not replayed.

**Bounded rollback (one release window).** ``BH_BEADS_ROUTE=cli`` forces the CLI-compatibility
route for every migrated cohort at once, without touching the service: :func:`hive_session`
refuses before resolving anything, exactly as if no service were running. It exists so an
operator can back out of the core route in the first release that ships this cutover without a
code change; it is removed in the release after that (the automatic unavailability selection
above stays — it is a compatibility route, not a rollback). Unset, empty, or ``api`` selects the
default. Any other value is refused loudly (``BeadsRouteInvalid``) rather than guessed at, so a
typo can never silently select — or silently fail to select — the rollback.

``approve`` / ``bounce`` (:mod:`beadhive.work_review`, bh-bwnys.1) are deliberately NOT routed
through here: they have no CLI-compatibility implementation to fall back to and fail closed when no
service is running, rollback or not.

``beadhive_core`` is resolved lazily by name — ``src/beadhive`` never imports a workspace package
statically (``scripts/check_package_imports.py``).
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

from . import host_beads

#: The one rollback switch. See the module docstring for its bounded lifetime.
ROUTE_ENV = "BH_BEADS_ROUTE"
ROUTE_API = "api"
ROUTE_CLI = "cli"
_ROUTES = (ROUTE_API, ROUTE_CLI)


class BeadsRouteInvalid(RuntimeError):
    """``BH_BEADS_ROUTE`` names neither ``api`` nor ``cli``.

    Deliberately not a ``ValueError`` / ``OSError``: the seams map those onto their
    CLI-compatibility route, and a mistyped switch must fail the command, not reroute it.
    """

    def __init__(self, value: str) -> None:
        super().__init__(
            f"{ROUTE_ENV}={value!r} is not a Beads route; use {ROUTE_API!r} (default) or "
            f"{ROUTE_CLI!r} (rollback to the CLI-compatibility route)"
        )


def _core() -> Any:
    return importlib.import_module("beadhive_core")


def selected_route() -> str:
    """``api`` (the default) or ``cli`` (the rollback), read fresh on every call."""
    value = os.environ.get(ROUTE_ENV, "").strip().lower()
    if not value:
        return ROUTE_API
    if value not in _ROUTES:
        raise BeadsRouteInvalid(value)
    return value


def hive_session(main: Path, entry: Any, capabilities: frozenset[str]) -> Any:
    """An unopened session against the hive's one supervised Beads service, never started here.

    Raises :class:`beadhive_core.SessionUnavailable` — which every migrated seam turns into its
    CLI-compatibility route — when the rollback is selected, when the hive cannot be served at
    all, or when ``entry`` cannot address a hive (several read-only surfaces pass a synthetic
    ``{"prefix": ...}`` entry deliberately; see ``tests/test_mcp_strict_bd_reads.py``). An absent
    or stale service surfaces as the client's ``ServiceError`` when the session is resolved or
    opened, which :func:`unavailable_errors` also names.
    """
    core = _core()
    if selected_route() == ROUTE_CLI:
        raise core.SessionUnavailable(
            f"{ROUTE_ENV}={ROUTE_CLI}: rollback selects the CLI-compatibility route"
        )
    try:
        return host_beads.resolve_session(main, capabilities, entry=entry)
    except host_beads.HiveNotServable as exc:
        raise core.SessionUnavailable(str(exc)) from exc
    except (KeyError, TypeError) as exc:
        raise core.SessionUnavailable(f"cannot address hive from entry {entry!r}: {exc}") from exc


def unavailable_errors() -> tuple[type[BaseException], ...]:
    """Every error meaning "no capable session — select the CLI-compatibility route".

    ``IncompatibleService`` (whose subclass ``CapabilityMissing`` is the missing-capability case),
    the supervised-service ``ServiceError`` family, and ``SessionUnavailable`` — imported lazily
    so this module never statically imports a workspace package.
    """
    client = importlib.import_module("beadhive_beads_client")
    service = importlib.import_module("beadhive_beads_client.service")
    return (client.IncompatibleService, service.ServiceError, _core().SessionUnavailable)
