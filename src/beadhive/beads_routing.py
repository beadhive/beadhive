"""The one composition decision for the beadhive-core command cohorts (bh-sy36q.6).

Every migrated work/planning cohort — lifecycle (``assign`` / ``claim`` / ``resume`` / ``abandon``
and submit's bead-state half, :mod:`beadhive.work_lifecycle`), molecule filing
(:mod:`beadhive.plan_filing`), ready/schedule/claim-next (:mod:`beadhive.work_queue`), and molecule
progress / swarm / dispatch polling / local-loop state (:mod:`beadhive.dispatch_state`) — opens its
Beads session HERE, and nowhere else. Two things are decided in this one place:

* **Default: beadhive-core.** Each seam asks :func:`hive_session` for an unopened session against
  the hive's one supervised Beads service (``bh host beads``) and, when it opens, serves the
  command through the ``beadhive_core`` handler. Nothing here ever starts ``bd serve``.
* **Pre-execution CLI-compatibility selection — only when the route allows it.** When no capable
  session can be opened — no service running, an embedded-Dolt hive Beads 1.3 cannot serve, a
  missing capability, a synthetic ``entry`` that cannot address a hive — :func:`hive_session`
  raises :class:`beadhive_core.SessionUnavailable` (or the client's ``ServiceError`` /
  ``IncompatibleService``, see :func:`unavailable_errors`) BEFORE the first Beads operation. The
  seam then asks :func:`allow_cli_route` whether it may select its named ``bd`` route for the
  whole command. It is never a retry: once the API route is selected, a failing call is reported,
  not replayed.

**The route: ``work.beads.route`` (bh-m36pc).** A per-hive config key (per-hive > global >
default) with three values, resolved by :func:`route`:

* ``api`` (the default): the supervised service only. :func:`allow_cli_route` refuses, so every
  migrated cohort fails closed with one ``✗`` line naming ``bh host beads start --hive <hive>``
  and this key (:class:`BeadsServiceRequired`, exit 1). No ``bd`` route is selected silently.
* ``api+cli-fallback``: the bh-sy36q.6 behavior, kept as a named opt-in — the API when a capable
  session opens, else the seam's ``bd`` route, selected before execution.
* ``cli``: the ``bd`` route always — :func:`hive_session` refuses before resolving anything,
  exactly as the ``BH_BEADS_ROUTE=cli`` rollback does.

An unknown value is refused loudly (:class:`BeadsRouteConfigInvalid`, exit 2), never defaulted.

**Bounded rollback (one release window).** ``BH_BEADS_ROUTE`` still takes precedence over the
config key while it exists, with exactly its bh-sy36q.6 meaning: ``cli`` forces the
CLI-compatibility route for every migrated cohort at once, without touching the service
(:func:`hive_session` refuses before resolving anything); ``api`` selects the default of that
release, i.e. the API with the automatic ``bd`` selection (``api+cli-fallback``). Unset or empty
defers to ``work.beads.route``. Any other value is refused loudly (``BeadsRouteInvalid``) rather
than guessed at, so a typo can never silently select — or silently fail to select — the
rollback. It is removed in a follow-up once the config path is proven.

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

import typer

from . import host_beads, registry
from .config_consumer_ports import work_settings as config

#: The one rollback switch. See the module docstring for its bounded lifetime.
ROUTE_ENV = "BH_BEADS_ROUTE"
ROUTE_API = "api"
ROUTE_CLI = "cli"
_ROUTES = (ROUTE_API, ROUTE_CLI)

#: The per-hive route key (bh-m36pc) and its values; see the module docstring.
CONFIG_KEY = "work.beads.route"
ROUTE_API_CLI_FALLBACK = "api+cli-fallback"
CONFIG_ROUTES = (ROUTE_API, ROUTE_API_CLI_FALLBACK, ROUTE_CLI)


class BeadsRouteInvalid(typer.Exit):
    """``BH_BEADS_ROUTE`` names neither ``api`` nor ``cli``: the command stops with exit 2.

    Raised by :func:`selected_route` after it renders the one ``✗`` diagnostic. Deliberately not
    one of the errors the seams map onto their CLI-compatibility route (``SessionUnavailable``,
    ``ServiceError``, ``IncompatibleService``, ``OSError``, ``ValueError``): a mistyped switch
    must fail the command, never silently reroute it.
    """

    def __init__(self, value: str) -> None:
        super().__init__(code=2)
        self.message = (
            f"{ROUTE_ENV}={value!r} is not a Beads route; use {ROUTE_API!r} (default) or "
            f"{ROUTE_CLI!r} (rollback to the CLI-compatibility route)"
        )

    def __str__(self) -> str:
        return self.message


class BeadsRouteConfigInvalid(typer.Exit):
    """``work.beads.route`` names none of ``api`` / ``api+cli-fallback`` / ``cli``: exit 2.

    Like :class:`BeadsRouteInvalid`, rendered once by :func:`configured_route` and never one of the
    errors a seam maps onto its CLI-compatibility route — a mistyped key fails the command.
    """

    def __init__(self, value: Any) -> None:
        super().__init__(code=2)
        choices = ", ".join(repr(choice) for choice in CONFIG_ROUTES)
        self.message = f"{CONFIG_KEY}={value!r} is not a Beads route; use one of {choices}"

    def __str__(self) -> str:
        return self.message


class BeadsServiceRequired(typer.Exit):
    """No capable Beads session, and the route (``api``) forbids the ``bd`` route: exit 1.

    Raised by :func:`allow_cli_route` after it renders the one ``✗`` diagnostic, which names the
    command that starts the hive's service and the key that opts the hive into the ``bd`` route.
    """

    def __init__(self, hive: str, detail: str) -> None:
        super().__init__(code=1)
        self.hive = hive
        self.message = (
            f"no capable Beads session for {hive}: {detail}. {CONFIG_KEY} is {ROUTE_API!r}, so "
            f"the bd CLI route is not selected — start the service with "
            f"`{host_beads.start_command(hive)}`, or set {CONFIG_KEY} to "
            f"{ROUTE_API_CLI_FALLBACK!r} (or {ROUTE_CLI!r}) for this hive"
        )

    def __str__(self) -> str:
        return self.message


def _core() -> Any:
    return importlib.import_module("beadhive_core")


def selected_route() -> str:
    """``api`` (the default) or ``cli`` (the rollback), read fresh on every call."""
    value = os.environ.get(ROUTE_ENV, "").strip().lower()
    if not value:
        return ROUTE_API
    if value not in _ROUTES:
        refusal = BeadsRouteInvalid(value)
        typer.echo(f"✗ {refusal}", err=True)
        raise refusal
    return value


def configured_route(entry: Any, cfg: dict | None = None) -> str:
    """The hive's ``work.beads.route`` (per-hive > global > ``api``), refused loudly if unknown."""
    hive = entry if isinstance(entry, dict) else None
    value = config.beads_route(cfg, hive)
    if value not in CONFIG_ROUTES:
        refusal = BeadsRouteConfigInvalid(value)
        typer.echo(f"✗ {refusal}", err=True)
        raise refusal
    return str(value)


def route(entry: Any, cfg: dict | None = None) -> str:
    """The effective route for ``entry``'s hive: ``api`` | ``api+cli-fallback`` | ``cli``.

    ``BH_BEADS_ROUTE``, when set, wins with its bh-sy36q.6 meaning (``api`` there is the API with
    the automatic ``bd`` selection); unset or empty defers to :func:`configured_route`.
    """
    if os.environ.get(ROUTE_ENV, "").strip():
        return ROUTE_CLI if selected_route() == ROUTE_CLI else ROUTE_API_CLI_FALLBACK
    return configured_route(entry, cfg)


def _hive_label(entry: Any) -> str:
    try:
        return registry.hive_key(entry)
    except (KeyError, TypeError):
        return "<hive>"


def allow_cli_route(entry: Any, exc: BaseException) -> None:
    """Gate a seam's ``bd`` route on ``exc`` (a no-capable-session error): return to select it.

    Under ``api`` this renders one ``✗`` line and raises :class:`BeadsServiceRequired` from
    ``exc`` instead, so the command fails closed; under ``api+cli-fallback`` / ``cli`` it returns
    and the seam selects its ``bd`` route exactly as before bh-m36pc.
    """
    if route(entry) != ROUTE_API:
        return
    refusal = BeadsServiceRequired(_hive_label(entry), str(exc) or type(exc).__name__)
    typer.echo(f"✗ {refusal}", err=True)
    raise refusal from exc


def hive_session(main: Path, entry: Any, capabilities: frozenset[str]) -> Any:
    """An unopened session against the hive's one supervised Beads service, never started here.

    Raises :class:`beadhive_core.SessionUnavailable` — which every migrated seam turns into its
    CLI-compatibility route when :func:`allow_cli_route` permits — when the ``cli`` route is
    selected (by ``work.beads.route`` or the ``BH_BEADS_ROUTE`` rollback), when the hive cannot
    be served at all, or when ``entry`` cannot address a hive (several read-only surfaces pass a
    synthetic ``{"prefix": ...}`` entry deliberately; see ``tests/test_mcp_strict_bd_reads.py``).
    An absent or stale service surfaces as the client's ``ServiceError`` when the session is
    resolved or opened, which :func:`unavailable_errors` also names.
    """
    core = _core()
    if selected_route() == ROUTE_CLI:
        raise core.SessionUnavailable(
            f"{ROUTE_ENV}={ROUTE_CLI}: rollback selects the CLI-compatibility route"
        )
    if route(entry) == ROUTE_CLI:
        raise core.SessionUnavailable(
            f"{CONFIG_KEY}={ROUTE_CLI}: the hive selects the CLI-compatibility route"
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
