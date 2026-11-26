"""The SQL-plane retirement guard (bh-d9anb, bh 0.40.0 epic bh-6se58).

From 0.40.0 every frame and CLI path reaches HQ only through hq-service (``hq.route: api``).
Opening HQ SQL directly is then a path that belongs on the hq-service host alone. Each HQ SQL
opener (``SqlControlPlane``, ``SqlRuntimeOperator``, the config-store SQL attachment,
``ReleaseCatalogStore``, ``SqlPlacementDirector``) and the HQ-host-local commands
(``hq_migrate_cli``, ``prune-inbox``) call :func:`guard_sql_open` first.

The guard is a diagnostic, never a fallback: it does not select a route, change a result, an
exit code or stdout. ``hq.sql_guard`` says what a *non*-service host gets:

* ``warn``: one stderr line per process per opener and call site;
* ``refuse``: :class:`SqlPlaneRetired`, before connecting;
* ``off``: silent.

The hq-service host is marked explicitly (``hq.service_host: true``, HOST-only, default off);
nothing infers it. In 0.40.0 the guard acts only on ``hq.route: api`` hosts and on hosts that
set ``hq.sql_guard`` explicitly; an ``hq.route: sql`` host that sets nothing is untouched and
silent until it is cut over. A later release will make ``warn`` plus refuse the default for every
unmarked host; nothing in 0.41.0 does. ``BH_HQ_SQL_GUARD`` / ``BH_HQ_SERVICE_HOST`` override the
two keys for one process.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from typing import Any

GUARD_ENV = "BH_HQ_SQL_GUARD"
SERVICE_HOST_ENV = "BH_HQ_SERVICE_HOST"
MODES = ("warn", "refuse", "off")
DEFAULT_MODE = "warn"
_TRUE = ("1", "true", "yes", "on")

_seen: set[tuple[str, str]] = set()
_lock = threading.Lock()


class SqlPlaneRetired(RuntimeError):
    """Opening HQ SQL was refused: this host reaches HQ through hq-service only."""


def reset() -> None:
    """Forget the once-per-process memory (tests)."""
    with _lock:
        _seen.clear()


def _sibling(name: str):
    """A sibling module resolved by name, not imported: every HQ SQL opener imports this guard,
    so a static edge from it back into the config or HQ modules would be an import cycle."""
    return importlib.import_module(f"{__package__}.{name}")


def _host_hq() -> dict[str, Any]:
    try:
        hq = _sibling("config").load_host().get("hq")
    except Exception:  # noqa: BLE001 - an unreadable host config is an unmarked sql host
        return {}
    return dict(hq) if hasattr(hq, "items") else {}


def posture() -> dict[str, Any]:
    """``{route, service_host, mode}`` for this host; never raises.

    ``route`` is ``unknown`` when ``hq.route`` is invalid; an invalid ``hq.sql_guard`` reads as
    the default ``warn`` (the guard must not turn a typo into a refusal)."""
    port = _sibling("hq_port")
    hq = _host_hq()
    try:
        route = port.configured_route({"hq": hq})
    except port.HqRouteError:
        route = "unknown"
    env_host = os.environ.get(SERVICE_HOST_ENV)
    service = env_host.strip().lower() in _TRUE if env_host else hq.get("service_host") is True
    raw = os.environ.get(GUARD_ENV) or hq.get("sql_guard")
    mode = raw if raw in MODES else DEFAULT_MODE
    return {
        "route": route,
        "service_host": service,
        "mode": mode,
        "explicit": raw in MODES,
        "sql_block": bool(hq.get("sql")),
    }


def is_service_host(state: dict[str, Any] | None = None) -> bool:
    """Whether HQ SQL may be opened here: marked as the hq-service host and not ``route: api``."""
    state = state or posture()
    return bool(state["service_host"]) and state["route"] != "api"


def _call_site(opener: str) -> str:
    """The first caller outside the opener's own module and this one."""
    frame = sys._getframe(2)  # noqa: SLF001 - skip guard_sql_open and the opener
    own = {__name__, frame.f_globals.get("__name__")}
    while frame is not None and frame.f_globals.get("__name__") in own:
        frame = frame.f_back
    if frame is None:
        return opener
    code = frame.f_code
    name = getattr(code, "co_qualname", code.co_name)
    return f"{frame.f_globals.get('__name__', '?')}.{name}"


def guard_sql_open(opener: str, *, host_only: bool = False, refuse_only: bool = False) -> None:
    """Warn (or refuse) when *opener* is about to open HQ SQL off the hq-service host.

    *opener* names the opener class or command. *host_only* marks the HQ-host-local operations
    (placement, ``hq_migrate_cli``, ``prune-inbox``) in the message. *refuse_only* is for a command
    whose SQL opener warns by itself: it only refuses (``hq.sql_guard: refuse``), so the command
    adds no second warning. A no-op on the marked hq-service host."""
    state = posture()
    if is_service_host(state) or state["mode"] == "off":
        return
    if state["route"] != "api" and not state["explicit"]:
        return  # a route=sql host that has not opted in is untouched; the default is unchanged
    if refuse_only and state["mode"] != "refuse":
        return
    site = _call_site(opener)
    why = (
        "this host is hq.route: api"
        if state["route"] == "api"
        else "this host is not marked hq.service_host"
    )
    what = "is hq-service-host-only" if host_only else "is retired off the hq-service host"
    message = (
        f"HQ SQL opener {opener} {what} ({why}; called from {site}). "
        "Reach HQ through hq-service (hq.route: api); "
        "a later bh release will refuse this. Silence: hq.sql_guard: off."
    )
    if state["mode"] == "refuse":
        raise SqlPlaneRetired(message)
    with _lock:
        if (opener, site) in _seen:
            return
        _seen.add((opener, site))
    print(f"warning: {message}", file=sys.stderr)


def doctor_data() -> dict[str, Any]:
    """This host's HQ route and ``hq.sql`` status for ``bh doctor``."""
    state = posture()
    lingering = state["sql_block"] and not state["service_host"]
    level = "ok"
    if state["route"] == "api" and state["sql_block"]:
        level = "warn"
        detail = "hq.route is api but a hq.sql block lingers: remove it (hq-service is the plane)"
    elif state["route"] == "sql" and not state["service_host"]:
        level = "warn"
        detail = (
            "hq.route is sql on a host not marked hq.service_host: the HQ SQL warning defaults "
            "on in a later release alongside refuse; cut this host over to hq.route: api"
        )
    elif lingering:
        level = "warn"
        detail = "hq.sql block present on a non-hq-service host"
    else:
        detail = "hq-service host" if state["service_host"] else "no HQ SQL configured"
    return {**state, "level": level, "detail": detail}
