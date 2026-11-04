"""Repository label DEFINITIONS: the ``labels.read`` reader and the ``labels.define`` writer.

Both are optional port capabilities (``LabelReader``, ``LabelDefiner``; additive, the port API
version stays 1), each a mixin of :class:`~.tracker.GitHubTrackerAdapter`:

* :class:`LabelDefinitionReading` is part of the READ path (the adapter class inherits it), so,
  like every read, a disabled ``labels.read`` is refused with ``OperationNotEnabled`` before the
  credential is resolved or any request is sent;
* :class:`LabelDefining` is a WRITER, composed in only when ``labels.define`` is enabled
  (:mod:`.compose`), so a disabled define has no method at all.

``define_labels`` copies the shape of the ``labels.change`` writer (:mod:`.labels`). Every refusal
below happens **before any request is sent** (and before the credential is resolved), and the
tests assert it on the transport:

1. a malformed definition (an empty or padded name, a color that is not six hex digits without
   ``#``, a description over :data:`DESCRIPTION_LIMIT` characters, two definitions of one name):
   ``ValueError``;
2. ``labels.define`` is not in the resolved ``enabled`` set: ``OperationNotEnabled``;
3. the grant is not a ``WriteGrant``, names another operation or another target than this
   adapter's ``ProjectRef`` (a define is repository-scoped, never issue-scoped), or was already
   used: ``OperationNotEnabled``;
4. a name to define or prune lies outside the configured ``write.labels.owned_prefixes``:
   ``LabelNotOwned``. The port's ``define_labels`` takes no ``LabelOwnership``, so the operator's
   configuration is the ONLY ownership; an adapter built without one owns nothing;
5. no credential resolved (an anonymous write cannot succeed): ``AuthenticationFailed``.

Then ONE read of the live definitions (paged, below) decides what to send, so a define converges:
a definition the repository already holds identically sends nothing, an owned label whose color
or description differs is updated in place, and a missing one is created. Only then is the grant
**spent** (immediately before the first write; a read that fails never burns the approval, a
batch that changes nothing still uses it) and the writes sent, in sorted name order, creates and
updates first, then deletes, through the one write seam :meth:`~.transport.Session.send`.

Wire (GitHub REST, API version 2022-11-28):

* ``GET /repos/{o}/{r}/labels?per_page=100``, following ``Link: rel="next"`` verbatim after
  :meth:`~.urls.GitHubLocation.check_next`; a missing description is ``""``;
* ``POST /repos/{o}/{r}/labels`` with ``{"name", "color", "description"}`` creates (``201``);
* ``PATCH /repos/{o}/{r}/labels/{name}`` with ``{"new_name", "color", "description"}`` updates
  (``200``). GitHub label names are case-insensitive: an existing label differing only in case is
  updated (renamed to the wanted spelling) rather than duplicated, and it must itself be owned;
* ``DELETE /repos/{o}/{r}/labels/{name}`` removes one ``prune`` names (``204 No Content``, a known
  success with no body). A ``404`` there means "already gone" only if a follow-up ``GET`` of the
  label also answers ``404``; otherwise the refusal stands (GitHub may report "forbidden" as
  "not found").

Every ``{name}`` in a path is percent-encoded with nothing safe (``bh:x/y`` -> ``bh%3Ax%2Fy``).
GitHub's documentation is silent on the permission a define needs and on the duplicate-name
refusal; ``tests/recorded/label_definitions_*.json`` pin both (a duplicate is ``422 Validation
Failed`` with ``code: already_exists``: ``PayloadRejected``).

Outcomes are those of every writer: a rate limit (nothing was done) is retried within the budget
on the injected sleep; a ``5xx``, a timeout, a dropped connection or an unreadable success body
raises ``WriteOutcomeUnknown`` at once, is NEVER retried, and no further request of the call is
sent (a fresh grant re-reads and converges); every other refusal is its scrubbed port error. A
failure part-way leaves the earlier requests applied.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    LabelDefinition,
    LabelNotOwned,
    LabelOwnership,
    NotVisible,
    Operation,
    ProjectRef,
    TrackerError,
    WriteGrant,
    WriteOutcomeUnknown,
    WriteReceipt,
)

from .urls import next_link

if TYPE_CHECKING:
    from .grants import GrantLedger
    from .transport import Session, Transport
    from .urls import GitHubLocation

__all__ = [
    "DESCRIPTION_LIMIT",
    "PAGE_SIZE",
    "LabelDefining",
    "LabelDefinitionReading",
    "definition_payload",
]

_READ = Operation.LABELS_READ
_DEFINE = Operation.LABELS_DEFINE

#: GitHub's documented maximum for a label description.
DESCRIPTION_LIMIT = 100

#: The page size of the label listing (GitHub's maximum).
PAGE_SIZE = 100

_COLOR = re.compile(r"[0-9a-fA-F]{6}")


def _segment(name: str) -> str:
    """``name`` as one URL path segment: every reserved character encoded (``/`` and ``:`` too)."""
    return quote(name, safe="")


def _checked(definition: object) -> LabelDefinition:
    """``definition`` with its color lower-cased, or ``ValueError`` (sends nothing)."""
    if not isinstance(definition, LabelDefinition):
        raise ValueError("labels.define takes LabelDefinition records")
    name, color, description = definition.name, definition.color, definition.description
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError("a label name must be a non-empty string without surrounding spaces")
    if not isinstance(color, str) or not _COLOR.fullmatch(color):
        raise ValueError(f"{name}: a label color is six hex digits without '#'")
    if not isinstance(description, str) or len(description) > DESCRIPTION_LIMIT:
        raise ValueError(f"{name}: a label description is at most {DESCRIPTION_LIMIT} characters")
    return LabelDefinition(name, color.lower(), description)


def definition_payload(definition: LabelDefinition) -> dict[str, str]:
    """The ``POST /labels`` body for ``definition`` (pure)."""
    checked = _checked(definition)
    return {"name": checked.name, "color": checked.color, "description": checked.description}


def _row(body: Any) -> LabelDefinition | None:
    """One GitHub label row as a definition; ``None`` when it is not one."""
    if not isinstance(body, dict):
        return None
    name, color, description = body.get("name"), body.get("color"), body.get("description")
    if not isinstance(name, str) or not isinstance(color, str):
        return None
    if description is not None and not isinstance(description, str):
        return None
    return LabelDefinition(name, color.lower(), description or "")


def _list_definitions(session: Session, location: GitHubLocation) -> list[LabelDefinition]:
    """Every label definition of the repository, paged to the end (one operation's reads)."""
    found: list[LabelDefinition] = []
    reply = session.get(f"{location.repo_path}/labels", {"per_page": str(PAGE_SIZE)})
    while True:
        rows = reply.body
        if not isinstance(rows, list):
            raise TrackerError("GitHub returned a label listing that is not a list")
        for body in rows:
            row = _row(body)
            if row is None:
                raise TrackerError("GitHub returned a label without a name or color")
            found.append(row)
        nxt = next_link(reply.headers.get("link"))
        if nxt is None:
            return found
        reply = session.get(location.check_next(nxt))


class LabelDefinitionReading:
    """Mixin (read path): ``list_label_definitions`` over the host's location and transport."""

    # Provided by the host, ``GitHubTrackerAdapter``.
    _location: GitHubLocation
    _transport: Transport
    _require: Callable[[Operation], None]

    def list_label_definitions(self) -> tuple[LabelDefinition, ...]:
        """Every label definition of the repository, owned or not, ordered by name."""
        self._require(_READ)
        with self._transport.operation(_READ) as session:
            found = _list_definitions(session, self._location)
        return tuple(sorted(found, key=lambda d: d.name))


class LabelDefining:
    """Mixin (writer): ``define_labels`` over the host's location, transport and grant ledger."""

    # Provided by the host, ``GitHubTrackerAdapter``.
    _grants: GrantLedger
    _location: GitHubLocation
    _project: ProjectRef
    _transport: Transport
    #: ``write.labels.owned_prefixes`` when built from configuration; ``None`` owns nothing here.
    _label_prefixes: tuple[str, ...] | None
    _require: Callable[[Operation], None]

    def _authorise_definitions(self, names: Iterable[str], grant: WriteGrant) -> WriteGrant:
        """Every local refusal, in order (sends and spends nothing); returns the checked grant."""
        self._require(_DEFINE)
        checked = self._grants.check(grant, _DEFINE, self._project)
        self._grants.refuse_if_spent(checked)
        self._refuse_foreign(names)
        return checked

    def _refuse_foreign(self, names: Iterable[str]) -> None:
        owned = LabelOwnership(self._label_prefixes or ())
        foreign = sorted({name for name in names if not owned.owns(name)})
        if foreign:
            raise LabelNotOwned(f"labels outside the owned set: {', '.join(foreign)}")

    def define_labels(
        self,
        definitions: tuple[LabelDefinition, ...],
        *,
        grant: WriteGrant,
        prune: frozenset[str] = frozenset(),
    ) -> WriteReceipt:
        """Create or update ``definitions`` (owned names only) under one repository grant, and
        delete each owned label ``prune`` names; converges, so an unchanged batch sends nothing.

        ``prune`` is this adapter's extension of the port (a keyword with an empty default, so
        the ``LabelDefiner`` shape is unchanged): one grant covers the whole convergence."""
        wanted = tuple(_checked(d) for d in definitions)
        folded = [d.name.casefold() for d in wanted]
        if len(set(folded)) != len(folded):
            raise ValueError("a label is defined twice (names are case-insensitive)")
        pruned = frozenset(prune)
        if not all(isinstance(name, str) and name.strip() for name in pruned):
            raise ValueError("prune names must be non-empty strings")
        if {name.casefold() for name in pruned} & set(folded):
            raise ValueError("a label cannot be both defined and pruned")
        checked = self._authorise_definitions([d.name for d in wanted] + sorted(pruned), grant)
        with self._transport.operation(_DEFINE, write=True) as session:
            if session.anonymous:
                raise AuthenticationFailed("labels.define needs a credential and none resolved")
            live = {d.name.casefold(): d for d in _list_definitions(session, self._location)}
            plan = self._plan(wanted, pruned, live)
            self._grants.spend(checked)  # only now: a failed read never burns the approval
            for method, name, payload in plan:
                self._send(session, method, name, payload)
        return WriteReceipt(checked.grant_id, checked.operation, self._project)

    def _plan(
        self,
        wanted: tuple[LabelDefinition, ...],
        pruned: frozenset[str],
        live: dict[str, LabelDefinition],
    ) -> list[tuple[str, str | None, dict[str, str] | None]]:
        """``(method, existing name, payload)`` per write; refuses a foreign case-variant."""
        writes: list[tuple[str, str | None, dict[str, str] | None]] = []
        deletes: list[tuple[str, str | None, dict[str, str] | None]] = []
        for want in sorted(wanted, key=lambda d: d.name):
            have = live.get(want.name.casefold())
            if have is None:
                writes.append(("POST", None, definition_payload(want)))
            elif have != want:
                self._refuse_foreign([have.name])  # a case-variant must be ours to rename
                payload = {"color": want.color, "description": want.description}
                if have.name != want.name:
                    payload["new_name"] = want.name
                writes.append(("PATCH", have.name, payload))
        for name in sorted(pruned):
            have = live.get(name.casefold())
            if have is not None:
                self._refuse_foreign([have.name])
                deletes.append(("DELETE", have.name, None))
        return writes + deletes

    def _send(
        self, session: Session, method: str, name: str | None, payload: dict[str, str] | None
    ) -> None:
        path = f"{self._location.repo_path}/labels"
        if name is None:  # create
            reply = session.send(method, path, payload)
        else:
            target = f"{path}/{_segment(name)}"
            if method == "DELETE":
                try:
                    session.send(method, target, no_content=True)
                except NotVisible:
                    # Gone already, or the write is forbidden and reported as "not found": a read
                    # of the label tells them apart (it answers 404 only when it is gone).
                    try:
                        session.get(target)
                    except NotVisible:
                        return
                    raise
                return
            reply = session.send(method, target, payload)
        if _row(reply.body) is None:
            raise WriteOutcomeUnknown(
                "GitHub answered a label definition write with something other than a label; "
                "it may have applied"
            )
