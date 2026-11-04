"""The Beads-service seam of the intake report store (bh-9o12j).

Intake used to speak ``bd`` only: every read and write of the report store spawned a process
against the hive's Dolt store. This module composes the store the way every migrated work seam is
composed (:mod:`beadhive.work_lifecycle`, :mod:`beadhive.work_queue`): one decision, made HERE,
about whether the store is served from the hive's supervised Beads service or from the ``bd``
CLI, driven by ``work.beads.route`` (:mod:`beadhive.beads_routing`).

* ``api`` (the default): the service only. With none running the command fails closed with the
  one ``✗`` line the work verbs print, naming ``bh host beads start --hive <hive>`` and the key.
* ``api+cli-fallback``: the service when a capable session opens, else the CLI, selected before
  the first store operation and never changed by a failing call.
* ``cli``: the CLI, as before; no session is resolved.

The decision is made on first use of the store, once per store, and held for the command (a
session opens in about the time of one ``bd`` call; nothing is opened by a command that never
needs the store). A directory that is not a managed hive's own checkout (a clone-on-demand cache,
a directory the registry does not know) has no service to address and stays on the CLI.

:class:`SessionReportApi` is the :class:`~beadhive.adapters.intake.ReportApi` over one open
``BeadsSession``: it turns the generated models into the ``bd show`` / ``bd list`` row shapes the
store already decides on (timestamps spelled as ``bd`` spells them) and every failure into a
:class:`~beadhive.adapters.intake.ReportApiError`. The client and its generated models are
resolved lazily by name; ``src/beadhive`` never imports a workspace package statically
(``scripts/check_package_imports.py``).
"""

from __future__ import annotations

import importlib
import re
import weakref
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import typer

from . import beads_routing, registry
from .adapters.intake import ReportApiError
from .config_consumer_ports import work_settings as config

#: What the store needs of the service. A service lacking any one of them is "no capable session",
#: and the route decides between failing closed and the CLI.
INTAKE_CAPABILITIES = frozenset(
    {
        "project.enforce",
        "issues.get",
        "issues.list",
        "issues.update",
        "issues.close",
        "issues.casMetadata",
        "dependencies.add",
        "dependencies.remove",
    }
)

#: Rows per request. ``list_issues`` accepts up to 1000; one page covers a whole queue.
_PAGE = 500

_UTC = re.compile(r"\+00:00$")


def hive_session(main: Path, entry: Any) -> Any:
    """An unopened session for the report store's ``INTAKE_CAPABILITIES`` — see
    :func:`beadhive.beads_routing.hive_session` (the one composition decision, including the
    ``work.beads.route`` cli route)."""
    return beads_routing.hive_session(main, entry, INTAKE_CAPABILITIES)


#: The session seam: ``(main, entry)`` -> an unopened ``BeadsSession``. Tests substitute a fake
#: session here; production resolves the hive's supervised service.
session_factory: Callable[[Path, Any], Any] = hive_session


def _client() -> Any:
    return importlib.import_module("beadhive_beads_client")


def _models() -> Any:
    return importlib.import_module("beads_v1_3.models")


def _failures() -> tuple[type[BaseException], ...]:
    """Every error a service call can raise that is not a programming error."""
    httpx = importlib.import_module("httpx")
    return (RuntimeError, OSError, ValueError, httpx.HTTPError)


def _spelled_as_bd(value: Any) -> Any:
    """``value`` with its ``..._at`` timestamps spelled as ``bd`` prints them (``Z``, not
    ``+00:00``), so a row from either route renders identically. A ``metadata`` document is
    DATA, never touched: it is stored and compared whole, and a string inside it that looks like
    a timestamp is the document's own."""
    if isinstance(value, dict):
        return {
            key: (
                item
                if key == "metadata"
                else _UTC.sub("Z", item)
                if key.endswith("_at") and isinstance(item, str)
                else _spelled_as_bd(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_spelled_as_bd(item) for item in value]
    return value


class SessionReportApi:
    """:class:`~beadhive.adapters.intake.ReportApi` over one open ``BeadsSession``."""

    def __init__(self, session: Any) -> None:
        self._session = session

    # -- failures --------------------------------------------------------------------------

    def _call(self, name: str, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        client = _client()
        try:
            return method(*args, **kwargs)
        except client.ServiceProblem as exc:
            problem = exc.problem
            detail = getattr(problem, "detail", "") or ""
            text = f"{problem.code}: {detail}" if detail else str(exc)
            if problem.status >= 500:  # the service faulted; it did not say no to THIS request
                raise ReportApiError(f"{name} failed: Beads {text}") from exc
            raise ReportApiError(
                f"{name} refused by Beads ({text})", refused=True, code=problem.code
            ) from exc
        except client.IndeterminateWrite as exc:
            raise ReportApiError(
                f"{name}: outcome unknown — the write may have committed; read the bead before "
                "retrying (never replayed automatically)"
            ) from exc
        except _failures() as exc:
            raise ReportApiError(f"{name} failed: {exc}") from exc

    @staticmethod
    def _missing(exc: ReportApiError) -> bool:
        return exc.refused and exc.code == "not_found"

    # -- reads -----------------------------------------------------------------------------

    def issue(self, bead: str) -> dict | None:
        try:
            details = self._call("read", self._session.get_issue, bead)
        except ReportApiError as exc:
            if self._missing(exc):
                return None
            raise
        return _spelled_as_bd(details.to_dict())

    def dependents(self, bead: str) -> list[dict]:
        try:
            details = self._call("read", self._session.get_issue, bead, include_dependents=True)
        except ReportApiError as exc:
            if self._missing(exc):
                return []
            raise
        rows = details.to_dict().get("dependents") or []
        return [_spelled_as_bd(row) for row in rows]

    def issues(
        self, *, label: str = "", status: str = "", has_metadata_key: str = "", all_: bool = False
    ) -> list[dict]:
        rows: list[dict] = []
        cursor: str | None = None
        while True:
            page = self._call(
                "list",
                self._session.list_issues,
                limit=_PAGE,
                cursor=cursor,
                sort="priority",  # ``bd list``'s own order; the API's default is by creation
                all_=all_,
                label=[label] if label else None,
                status=[status] if status else None,
                has_metadata_key=has_metadata_key or None,
            )
            rows.extend(_spelled_as_bd(item.to_dict()) for item in page.items)
            cursor = _next_cursor(page)
            if cursor is None:
                return rows

    # -- writes ----------------------------------------------------------------------------

    def update(
        self,
        bead: str,
        *,
        actor: str,
        issue_type: str = "",
        priority: int | None = None,
        assignee: str | None = None,
    ) -> None:
        models = _models()
        patch: dict[str, Any] = {}
        if issue_type:
            patch["issue_type"] = issue_type
        if priority is not None:
            patch["priority"] = priority
        if assignee is not None:
            patch["assignee"] = assignee
        request = models.UpdateIssueRequest(actor=actor, patch=models.IssuePatchBody(**patch))
        self._call("update", self._session.update_issue, bead, request)

    def close(self, bead: str, *, reason: str, actor: str, force: bool = False) -> None:
        models = _models()
        fields: dict[str, Any] = {"actor": actor}
        if reason:
            fields["reason"] = reason
        if force:
            fields["force"] = True
        self._call("close", self._session.close_issue, bead, models.CloseIssueRequest(**fields))

    def link(self, source: str, target: str, type_: str, *, actor: str) -> None:
        models = _models()
        edge = models.DependencyEdge(issue_id=source, depends_on_id=target, type_=type_)
        request = models.AddDependenciesRequest(actor=actor, edges=[edge])
        self._call("dependency add", self._session.add_dependencies, request)

    def unlink(self, source: str, target: str, *, actor: str) -> None:
        models = _models()
        request = models.RemoveDependencyRequest(actor=actor, issue_id=source, depends_on_id=target)
        self._call("dependency remove", self._session.remove_dependency, request)

    def set_document(
        self, bead: str, key: str, document: Any, *, current: Any, present: bool, actor: str
    ) -> None:
        """Swap the whole ``key`` document, guarded by the value the caller read: an absent key
        is expected absent, a present one expected equal."""
        models = _models()
        fields: dict[str, Any] = {"actor": actor, "key": key, "value": document}
        if present:
            fields["expected"] = current
        request = models.CompareAndSetMetadataRequest(**fields)
        outcome = self._call(
            "metadata update", self._session.compare_and_set_metadata, bead, request
        )
        if not outcome.swapped:  # a lost race is a 200 answer, not an error: it is still a refusal
            raise ReportApiError(
                f"{key} of {bead} changed since it was read — nothing was written; re-read and "
                "retry",
                refused=True,
            )


def _next_cursor(page: Any) -> str | None:
    if not page.has_more:
        return None
    cursor = page.next_cursor
    return cursor if isinstance(cursor, str) and cursor else None


class ApiSelector:
    """The route decision for ONE store: call it to get the :class:`SessionReportApi`, or ``None``
    when the CLI route is selected. Decided on first call, held after.

    ``entry`` is the hive's registry entry, or a callable returning it (or ``None``) for a caller
    that holds only the hive directory. Raising is the route's own fail-closed
    (:class:`~beadhive.beads_routing.BeadsServiceRequired`): it propagates before the store has
    written anything through the CLI."""

    def __init__(self, directory: Any, entry: Mapping[str, Any] | Callable[[], Any] | None) -> None:
        self._directory = Path(directory)
        self._entry = entry
        self._decided = False
        self._api: SessionReportApi | None = None

    def __call__(self) -> SessionReportApi | None:
        if not self._decided:
            self._api = self._decide()
            self._decided = True
        return self._api

    def _resolve_entry(self) -> Any:
        entry = self._entry() if callable(self._entry) else self._entry
        return entry or None

    def _serves(self, session: Any) -> bool:
        """Whether the OPEN session serves this directory: the service names the checkout it
        serves, and the hive's own checkout is the one a store may use it for. A clone-on-demand
        cache is another checkout of the same data, which the CLI reads and the service does
        not. (The service's own answer, so no second resolution of the hive's directory.)"""
        served = getattr(getattr(session, "context", None), "repo_root", None)
        if not isinstance(served, str) or not served:
            return True  # a service that does not say is trusted: its context already matched
        try:
            return Path(served).resolve() == self._directory.resolve()
        except OSError:
            return False

    def _decide(self) -> SessionReportApi | None:
        entry = self._resolve_entry()
        if entry is None:
            return None
        stack = ExitStack()
        try:
            session = stack.enter_context(session_factory(self._directory, entry))
        except (*beads_routing.unavailable_errors(), OSError, ValueError) as exc:
            stack.close()
            beads_routing.allow_cli_route(entry, exc)
            return None
        if not self._serves(session):
            stack.close()
            return None
        weakref.finalize(self, stack.close)
        return SessionReportApi(session)


def entry_of_directory(directory: Any) -> Any:
    """The registry entry owning ``directory``, or ``None``: the lookup for a caller that holds
    only the hive directory (the queue, the dispositions). Never raises for a directory the
    registry cannot place."""
    try:
        return registry.entry_for_dir(config.load(), Path(directory))
    except typer.Exit:
        raise
    except (OSError, KeyError, TypeError, ValueError):
        return None


__all__ = [
    "INTAKE_CAPABILITIES",
    "ApiSelector",
    "SessionReportApi",
    "entry_of_directory",
    "hive_session",
    "session_factory",
]
