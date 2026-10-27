"""The GitHub ``TrackerAdapter`` (read path) plus the optional ``IssueResolver`` (amendment A1).

Wiring-free (tracker-adapter-port-adr.md section 4): constructed with explicit arguments (the
project, the resolved ``enabled`` set, a ``RetryBudget``, ``sleep``, the token source), it reads
no configuration and imports no plugin kernel. Root's lazy factory row builds it
(:mod:`beadhive_tracker_github.factory`).

**This class is the read path; writers are mixins.** It has no write method itself. Each write
operation is its own module (``comment.create``: :mod:`.comments`; ``labels.change``:
:mod:`.labels`), composed onto this class by :mod:`.compose` only when that operation is enabled,
so the built object structurally lacks a disabled write's method. A present writer is necessary,
never sufficient: it refuses before any request unless its operation is in ``enabled`` and the
call carries a fresh, matching ``WriteGrant``. ``describe().supported`` names what the package
implements; the one grant ledger every writer spends from lives here (one per adapter instance).

Wire facts honoured (port ADR section 1):

* ``state=all`` is load-bearing: GitHub's default hides closed issues. Closed issues are in scope
  unless the query (or ``read.include_closed``) narrows it; the full id listing always uses
  ``state=all`` so a deletion is never inferred from a closed issue.
* Pull requests appear in the issues list and in the repository-wide comment list; both are
  dropped (never work items) but still advance the checkpoint, since they share the ordering.
* ``Link: rel="next"`` is followed verbatim (it may carry an ``after=`` cursor), after
  :meth:`~.urls.GitHubLocation.check_next` confirmed it stays on the API origin.
* ``since`` is inclusive, so a boundary item may repeat and never skips. Issues and repository
  comments are listed ``sort=updated&direction=asc``, so every page's checkpoint (the page's
  highest ``updated_at``, the TRACKER's own clock) is safe to persist. Per-issue comments come in
  creation order, so their checkpoint is withheld until the final page and the running maximum
  travels inside the opaque cursor.
* Deletions are invisible to ``since``: :meth:`GitHubTrackerAdapter.list_ids` gives the full id
  set for a periodic reconcile (``beadhive.modules.tracker.application.snapshot_ids``).

**Disabled reads are refused before any request**, exactly as ``MemoryTracker`` does: the
composition root narrows writers structurally but delegates every read, so the adapter is the
layer that keeps a read the operator did not enable from reaching GitHub (``OperationNotEnabled``,
raised before the credential is even resolved). Identity (``whoami``, ``probe_capabilities``) is
not gated by a read flag: it is the read-only evidence ``doctor --probe`` shows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from beadhive.modules.tracker.contracts import (
    PORT_API_VERSION,
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    Actor,
    AuthenticationFailed,
    CapabilityReport,
    CapabilityState,
    ChangeRequestUrl,
    Checkpoint,
    CommentQuery,
    IdPage,
    IdQuery,
    IdSubject,
    IssueKey,
    IssueQuery,
    NotVisible,
    Operation,
    OperationNotEnabled,
    OtherProject,
    Page,
    PageCursor,
    PermissionDenied,
    ProjectRef,
    RateBudget,
    RetryBudget,
    TrackerComment,
    TrackerDescriptor,
    TrackerError,
    TrackerIssue,
    Tri,
)
from beadhive_plugins.credentials import TokenSource

from . import mapping
from .grants import GrantLedger
from .identity import RepositoryFacts, probe_operation
from .transport import DEFAULT_TIMEOUT_SECONDS, Reply, Session, Transport
from .urls import GitHubLocation

if TYPE_CHECKING:
    import httpx

__all__ = ["CHECKPOINT_PREFIX", "SUPPORTED_OPERATIONS", "GitHubTrackerAdapter"]

#: Every checkpoint token this adapter emits starts with this (and refuses any other).
CHECKPOINT_PREFIX = "github:"

#: What this adapter implements: the read path plus each landed writer (composed when enabled).
SUPPORTED_OPERATIONS = READ_OPERATIONS | {Operation.COMMENT_CREATE, Operation.LABELS_CHANGE}

_NEXT = re.compile(r'<([^<>]+)>\s*;\s*rel="next"')


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _next_link(reply: Reply) -> str | None:
    match = _NEXT.search(reply.headers.get("link", "") or "")
    return match.group(1) if match else None


def _budget(reply: Reply) -> RateBudget | None:
    remaining = reply.headers.get("x-ratelimit-remaining")
    reset = reply.headers.get("x-ratelimit-reset")
    if remaining is None and reset is None:
        return None
    resets_at = None
    if reset is not None and reset.isdigit():
        resets_at = datetime.fromtimestamp(int(reset), tz=UTC)
    return RateBudget(int(remaining) if remaining and remaining.isdigit() else None, resets_at)


def _rows(reply: Reply) -> list[Any]:
    if not isinstance(reply.body, list):
        raise mapping.PayloadUnreadable("GitHub returned a listing that is not a list")
    return reply.body


class GitHubTrackerAdapter:
    """Implements ``TrackerAdapter`` (reader plus identity) and ``IssueResolver`` for GitHub; the
    optional writers are composed onto it by :mod:`.compose`."""

    def __init__(
        self,
        *,
        project: ProjectRef,
        token_source: TokenSource,
        sleep: Callable[[float], None],
        enabled: frozenset[Operation] = frozenset(),
        budget: RetryBudget | None = None,
        page_size: int | None = None,
        include_closed: bool = True,
        base_url: str | None = None,
        http_cache: bool = False,
        label_prefixes: tuple[str, ...] | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._project = project
        self._location = GitHubLocation.from_project(project, base_url)
        self._enabled = frozenset(enabled)
        self._page_size = page_size
        self._label_prefixes = None if label_prefixes is None else tuple(label_prefixes)
        self._include_closed = include_closed
        self._transport = Transport(
            token_source=token_source,
            api_base=self._location.api_base,
            budget=budget or RetryBudget(),
            sleep=sleep,
            clock=clock or _utc_now,
            http_cache=http_cache,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        #: Write capability refuted by a probe: cached for this process only (never YES/UNKNOWN).
        self._refuted: dict[Operation, str] = {}
        #: Single-use write grants already presented to this instance (every writer spends here).
        self._grants = GrantLedger()

    def __repr__(self) -> str:
        return f"GitHubTrackerAdapter({self._location.locator!r})"

    # -- gates and paging plumbing -------------------------------------------------------------

    def _require(self, operation: Operation) -> None:
        if operation not in self._enabled:
            raise OperationNotEnabled(
                f"{operation.value} is not enabled for {self._location.locator}"
            )

    def _per_page(self, size: int | None) -> dict[str, str]:
        size = size if size is not None else self._page_size
        return {} if size is None else {"per_page": str(max(1, min(int(size), 100)))}

    @staticmethod
    def _since(checkpoint: Checkpoint | None) -> dict[str, str]:
        if checkpoint is None:
            return {}
        if not checkpoint.token.startswith(CHECKPOINT_PREFIX):
            raise ValueError("a checkpoint from another adapter")
        moment = mapping.parse_time(checkpoint.token.removeprefix(CHECKPOINT_PREFIX))
        return {"since": mapping.format_time(moment)}

    @staticmethod
    def _stamp(moment: datetime) -> Checkpoint:
        return Checkpoint(CHECKPOINT_PREFIX + mapping.format_time(moment))

    @staticmethod
    def _cursor(next_url: str, high_water: str | None = None) -> PageCursor:
        return PageCursor(json.dumps({"next": next_url, "hw": high_water}, sort_keys=True))

    def _uncursor(self, cursor: PageCursor) -> tuple[str, str | None]:
        try:
            data = json.loads(cursor.token)
            next_url, high_water = data["next"], data["hw"]
        except (ValueError, TypeError, KeyError):
            raise ValueError("a page cursor from another adapter or host") from None
        if not isinstance(next_url, str) or not isinstance(high_water, (str, type(None))):
            raise ValueError("a page cursor from another adapter or host")
        return self._location.check_next(next_url), high_water

    def _fetch(
        self, session: Session, cursor: PageCursor | None, path: str, params: dict[str, str]
    ) -> tuple[Reply, str | None]:
        if cursor is not None:
            next_url, high_water = self._uncursor(cursor)
            return session.get(next_url), high_water
        return session.get(path, params), None

    def _checkpoint(self, rows: list[Any], fallback: Checkpoint | None) -> Checkpoint | None:
        stamps = [mapping.updated_at(row) for row in rows]
        return self._stamp(max(stamps)) if stamps else fallback

    def _issue_number(self, key: IssueKey) -> int:
        """The number of an issue of THIS repository (refusing another project's key)."""
        resolved = self._location.resolve(key.url)
        if resolved.identifier != key.identifier:
            raise NotVisible(f"{key.identifier} is not an issue of {self._location.locator}")
        return int(resolved.identifier.removeprefix("#"))

    # -- TrackerReader -------------------------------------------------------------------------

    def describe(self) -> TrackerDescriptor:
        return TrackerDescriptor(self._project, PORT_API_VERSION, SUPPORTED_OPERATIONS)

    def _issue_page(
        self, query: IssueQuery, *, state: str, sort: str
    ) -> tuple[list[Any], Reply, str | None]:
        params = {
            "state": state,
            "sort": sort,
            "direction": "asc",
            **self._since(query.since),
            **self._per_page(query.page_size),
        }
        with self._transport.operation(Operation.ISSUES_READ) as session:
            reply, _ = self._fetch(
                session, query.cursor, f"{self._location.repo_path}/issues", params
            )
        return _rows(reply), reply, _next_link(reply)

    def list_issues(self, query: IssueQuery) -> Page[TrackerIssue]:
        self._require(Operation.ISSUES_READ)
        include_closed = query.include_closed and self._include_closed
        rows, reply, nxt = self._issue_page(
            query, state="all" if include_closed else "open", sort="updated"
        )
        issues = tuple(mapping.issue(row) for row in rows if not mapping.is_change_request(row))
        return Page(
            issues,
            self._cursor(nxt) if nxt else None,
            self._checkpoint(rows, query.since),  # pull requests share the ordering
            _budget(reply),
        )

    def _comment_page(
        self, cursor: PageCursor | None, path: str, params: dict[str, str]
    ) -> tuple[list[Any], Reply, str | None, str | None]:
        with self._transport.operation(Operation.COMMENTS_READ) as session:
            reply, high_water = self._fetch(session, cursor, path, params)
        return _rows(reply), reply, _next_link(reply), high_water

    def list_comments(self, query: CommentQuery) -> Page[TrackerComment]:
        self._require(Operation.COMMENTS_READ)
        since = self._since(query.since)
        size = self._per_page(query.page_size)
        if query.issue is None:
            path = f"{self._location.repo_path}/issues/comments"
            params = {"sort": "updated", "direction": "asc", **since, **size}
        else:  # creation order; GitHub accepts only `since` here
            number = self._issue_number(query.issue)
            path = f"{self._location.repo_path}/issues/{number}/comments"
            params = {**since, **size}
        rows, reply, nxt, high_water = self._comment_page(query.cursor, path, params)
        comments = tuple(
            mapping.comment(row) for row in rows if not mapping.is_change_request_comment(row)
        )
        if query.issue is None:
            cursor = self._cursor(nxt) if nxt else None
            return Page(comments, cursor, self._checkpoint(rows, query.since), _budget(reply))
        # Unordered: carry the running maximum inside the opaque cursor; emit it at the end.
        stamps = [mapping.updated_at(row) for row in rows]
        if high_water is not None:
            stamps.append(mapping.parse_time(high_water))
        top = mapping.format_time(max(stamps)) if stamps else None
        if nxt:
            return Page(comments, self._cursor(nxt, top), None, _budget(reply))
        final = Checkpoint(CHECKPOINT_PREFIX + top) if top else query.since
        return Page(comments, None, final, _budget(reply))

    def list_ids(self, query: IdQuery) -> IdPage:
        if query.subject is IdSubject.ISSUES:
            self._require(Operation.ISSUES_READ)
            # Always state=all: a closed issue still exists, so it must never look deleted.
            rows, _, nxt = self._issue_page(
                IssueQuery(cursor=query.cursor, page_size=query.page_size),
                state="all",
                sort="created",
            )
            ids = tuple(
                mapping.issue_key(row).url for row in rows if not mapping.is_change_request(row)
            )
            return IdPage(ids, self._cursor(nxt) if nxt else None)
        self._require(Operation.COMMENTS_READ)
        size = self._per_page(query.page_size)
        if query.issue is None:
            path = f"{self._location.repo_path}/issues/comments"
            params = {"sort": "created", "direction": "asc", **size}
        else:
            number = self._issue_number(query.issue)
            path = f"{self._location.repo_path}/issues/{number}/comments"
            params = size
        rows, _, nxt, _ = self._comment_page(query.cursor, path, params)
        ids = tuple(
            mapping.comment(row).key.id
            for row in rows
            if not mapping.is_change_request_comment(row)
        )
        return IdPage(ids, self._cursor(nxt) if nxt else None)

    # -- IssueResolver (amendment A1) ----------------------------------------------------------

    def resolve(self, url: str) -> IssueKey:
        """Pure: the canonical key of an issue URL of this repository (no request)."""
        return self._location.resolve(url)

    def get(self, key: IssueKey) -> TrackerIssue:
        """One read: the issue ``key`` names, open or closed."""
        self._require(Operation.ISSUES_READ)
        number = self._issue_number(key)
        with self._transport.operation(Operation.ISSUES_READ) as session:
            reply = session.get(f"{self._location.repo_path}/issues/{number}")
        row = reply.body
        if mapping.is_change_request(row):
            raise ChangeRequestUrl(f"{key.identifier} is a pull request, not an issue")
        parsed = self._location.parse_html_url(mapping.issue_key(row).url)
        if parsed is None or not self._location.same_repository(parsed[0], parsed[1]):
            raise OtherProject(f"{key.identifier} was transferred to another repository")
        return mapping.issue(row)

    # -- TrackerIdentity -----------------------------------------------------------------------

    @staticmethod
    def _user(session: Session) -> Actor | None:
        if session.anonymous:
            return None
        try:
            reply = session.get("/user")
        except PermissionDenied as exc:
            if "integration" in str(exc).lower():
                return None  # an App installation token: /user is not its to read (deferred)
            raise
        return mapping.actor(reply.body)

    def whoami(self) -> Actor | None:
        """The credential's actor, keyed ``(numeric id, kind)``; ``None`` when anonymous."""
        with self._transport.operation(None) as session:
            return self._user(session)

    def probe_capabilities(self) -> CapabilityReport:
        """GET-only, tri-state evidence per operation; never a proof, never a write."""
        with self._transport.operation(None) as session:
            source = session.source
            actor: Actor | None = None
            facts: RepositoryFacts | None = None
            try:
                actor = self._user(session)
            except AuthenticationFailed as exc:
                facts = RepositoryFacts(readable=False, reason=str(exc))
            except TrackerError:
                actor = None  # the repository read below still decides the evidence
            if facts is None:
                try:
                    reply = session.get(self._location.repo_path)
                    facts = RepositoryFacts.from_reply(reply.body, reply.headers)
                except (AuthenticationFailed, NotVisible, PermissionDenied) as exc:
                    facts = RepositoryFacts(readable=False, reason=str(exc))
                except TrackerError as exc:  # rate limited, transient: could not tell
                    facts = RepositoryFacts(readable=False, reason=str(exc), definitive=False)
        states: dict[Operation, CapabilityState] = {}
        for op in sorted(READ_OPERATIONS | WRITE_OPERATIONS, key=lambda o: o.value):
            tri, reason = probe_operation(op, facts)
            if op in WRITE_OPERATIONS:
                if op in self._refuted:
                    tri, reason = Tri.NO, self._refuted[op]
                elif tri is Tri.NO:
                    self._refuted[op] = reason
            states[op] = CapabilityState(
                op,
                supported=op in SUPPORTED_OPERATIONS,
                enabled=op in self._enabled,
                probe=tri,
                reason=reason,
            )
        return CapabilityReport(actor=actor, credential_source=source, states=states)

    # -- read-only introspection for tests and diagnostics -------------------------------------

    @property
    def enabled(self) -> frozenset[Operation]:
        return self._enabled

    @property
    def location(self) -> GitHubLocation:
        return self._location

    @property
    def settings_summary(self) -> Mapping[str, object]:
        """Non-secret effective settings (no credential, no grant)."""
        transport = self._transport
        return {
            "api_base": transport.api_base,
            "http_cache": transport.http_cache,
            "include_closed": self._include_closed,
            "page_size": self._page_size,
            "retry": transport.budget,
            "timeout_seconds": transport.timeout_seconds,
        }
