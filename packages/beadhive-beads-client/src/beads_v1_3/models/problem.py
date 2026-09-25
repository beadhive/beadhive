from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="Problem")


@_attrs_define
class Problem:
    """RFC 9457 problem detail. This is the only error shape on this surface. The core declares `type`; this server never
    emits it, so `about:blank` is implied throughout.

        Attributes:
            status (int): The HTTP status code, repeated in the body.
            title (str): The status phrase. Human-facing; never dispatch on it.
            code (str): The stable machine-readable reason, and the ONLY member a client may dispatch on. v0's vocabulary:
                `invalid_argument` (400, also emitted by the Host-header middleware on any route), `invalid_cursor` (400),
                `unauthenticated` (401, only on a server configured with a token file), `not_found` (404), `already_claimed`
                (409), `not_claimable` (409), `not_closable` (409), `not_releasable` (409), `dependency_cycle` (409),
                `dependency_exists` (409), `already_exists` (409), `precondition_failed` (409), `events_journal_disabled` (409),
                `events_journal_truncated` (410), `busy` (503), `db_unavailable` (503), `events_watch_saturated` (503),
                `internal` (500). Renaming or removing a status+code pair is a breaking change; ADDING one is not, so clients
                MUST default-branch on unknown values and fall back to the status class (unknown 4xx → client bug, fail loud;
                unknown 503 → retry per `Retry-After`; other unknown 5xx → server fault).
            request_id (str): Opaque correlation id for this request, echoed in the server's request log line. Never a
                dispatch key and never a retry key. (This server mints per-process ids that do not survive a restart; a
                deployment may substitute any identifier with the same log-correlation property, such as an edge trace id.)
            type_ (str | Unset): RFC 9457 problem type. This server never emits it, so `about:blank` is implied. A
                deployment that hosts problem documentation MAY supply it: one stable URI per status+code pair, dereferencing to
                documentation for that pair. It restates identity that `code` already carries, so a client MUST NOT dispatch on
                it and a server MUST NOT use it to subdivide a code.
            detail (str | Unset): Optional prose, never load-bearing. For 5xx codes it is a FIXED string per code and
                carries nothing about the underlying failure: driver and dial errors routinely embed the DSN, database user and
                host:port, and this API supports binding beyond loopback. It is fixed for `unauthenticated` too, and for the
                mirror-image reason: the caller's own input there is a credential, so echoing it would write the token into
                every client log and proxy trace on the way back. Other 4xx details reflect the caller's own input back and are
                specific.
            param (str | Unset): With `invalid_argument`: the offending query parameter, body member or header name. Present
                on every 400 except a body that fails to parse at all.

                With `precondition_failed`: the body member carrying the guard that missed. It is the same spelling a 400 on the
                same operation would use, so a client reads one member to find the offending input whichever way the request was
                refused.
            reason (str | Unset): With `invalid_argument`: `unknown_parameter` (this server does not know that parameter —
                version skew; degrade or fall back), `invalid_value` (the value is not one this server will act on: malformed,
                out of vocabulary, or — for `limit=0` under `--allow-non-loopback` — legal but refused in this server's
                configuration; `detail` says which), or `project_mismatch` (the `Bd-Project-Id` header named a project this
                server does not serve — a document-level refusal like the Host-header 400, raised on every enforced route, and
                the one that carries `server_project_id`; see the document-level rule). Either way the recovery is to send
                something different, never to retry the same request. The set may grow; default-branch on unknown values.
            assignee (str | Unset): With `already_claimed`: the actor currently holding the issue, read inside the
                transaction that refused.

                IT IS OPTIONAL ON EVERY OPERATION BUT THE CLAIM. `POST /v0/beads/issues/{id}:claim` always carries it, because
                its conflict path reads the row it lost to. `PATCH /v0/beads/issues/{id}` and `POST /v0/beads/issues:batchApply`
                carry it only when the refusing transaction reported a holder, and `POST /v0/beads/issues/{id}:release` never
                does — the ownership fence refuses without naming anyone. An absent member means "this refusal could not name
                the holder", never "nobody holds it"; re-read the row.
            issue_status (str | Unset): With `already_claimed` or `not_claimable`: the issue's status at the moment of
                refusal.
            open_children (int | Unset): With `not_closable`: how many open children the transaction that refused the close
                observed, read inside that transaction rather than parsed out of `detail`.

                PRESENT ONLY for the open-children refusal. The other `not_closable` refusal is a live blocker and carries no
                such member, so member presence — not prose — is how a client tells the two apart. Both are bypassed by `force`.
            existing_type (str | Unset): With `dependency_exists`: the type of the edge the pair already carries, read
                inside the refusing transaction.
            requested_type (str | Unset): With `dependency_exists`: the type the request asked for. Together with
                `existing_type` it is the whole refusal, so a client never parses either out of `detail`.
            issue_id (str | Unset): With `dependency_cycle`, and ONLY on the hierarchy refusal: the issue the requested
                blocking edge would have gated. Its PRESENCE is the discriminator — absent means a plain scheduling cycle,
                present means the edge pointed at the issue's own ancestor or descendant.

                The conflicting hierarchy may exist only inside the rolled-back batch, so no read after the fact can recover it:
                the refusing transaction is the only place this member can come from.
            blocker_id (str | Unset): With `dependency_cycle`, hierarchy refusal only: the ancestor or descendant the edge
                named as blocker. See `issue_id`.
            blocker_is_ancestor (bool | Unset): With `dependency_cycle`, hierarchy refusal only: true when `blocker_id` is
                an ANCESTOR of `issue_id` (which cannot close until its descendants finish, so the gate would never clear),
                false when it is a DESCENDANT (blocked status cascades, so it would inherit the block and never close). Both
                polarities are reported; this member is never omitted to mean false. See `issue_id`.
            expected_version (str | Unset): With `precondition_failed`: the row `revision` the request guarded on, echoed
                from the request itself.

                THE EXPECTED/ACTUAL PAIRS ARE SPLIT BY TYPE rather than carried as one polymorphic `expected`/`actual`, and the
                reason is this document's: a member that is "a version or a status or an assignee" is a schema alternation, and
                no composition keyword is available to spell one here (see `ApplyItem`). Three typed pairs cost three member
                names and are readable by a generated client without a cast.

                IT IS A STRING, and it must be the `revision` string a response carried, verbatim. A JSON number — or any other
                type — is a `400` naming this member. The token spans the FULL 64-bit range, so a number would be rounded past
                2^53 by an IEEE-754-double parser and the guard would miss a row nothing else touched; a string round-trips
                exactly in every consumer.
            actual_version (str | Unset): With `precondition_failed`: the `revision` the row was found holding, read inside
                the transaction that refused the guard.

                PRESENT ONLY WHERE THE REFUSING OPERATION CAN REPORT IT. An all-or-nothing operation rolls its transaction back,
                so a value read after the fact would describe a row the refusal never saw; where the role behind an operation
                does not carry the observed value, this member is omitted rather than reconstructed. Its absence therefore means
                "this server cannot tell you what it found", never "it found zero".

                NO v0 OPERATION EMITS IT TODAY, nor `actual_status` or `actual_assignee`. Every operation that publishes a guard
                refuses all-or-nothing, and none of the roles behind them carries the observed value out of the rolled-back
                transaction. The three members are declared so that an operation whose role CAN report what it found is an
                addition rather than a wire change — a client must not wait for them, and must never read their absence as a
                value.

                IT IS A STRING, and it must be the `revision` string a response carried, verbatim. A JSON number — or any other
                type — is a `400` naming this member. The token spans the FULL 64-bit range, so a number would be rounded past
                2^53 by an IEEE-754-double parser and the guard would miss a row nothing else touched; a string round-trips
                exactly in every consumer.
            expected_status (str | Unset): With `precondition_failed`: the status the request guarded on, echoed from the
                request. See `expected_version`.
            actual_status (str | Unset): With `precondition_failed`: the status the row was found holding. Present under
                `actual_version`'s rule.
            expected_assignee (str | Unset): With `precondition_failed`: the assignee the request guarded on, echoed from
                the request. See `expected_version`.
            actual_assignee (str | Unset): With `precondition_failed`: the assignee the row was found holding. Present under
                `actual_version`'s rule.
            item_index (int | Unset): On a batch operation whose items are heterogeneous: the position in `items` of the
                item that earned the refusal, read from the role's own typed error rather than parsed out of `detail`.

                The request is all or nothing, so there is no per-item result array for a client to find the offender in — these
                four `item_*` members are the only place it exists.
            item_kind (str | Unset): The `kind` of the item at `item_index`, so a client can dispatch on what the item was
                doing without walking its own request back.
            item_key (str | Unset): The refused item's own `key`, or the key its target ref named. ABSENT when the item
                named nothing symbolically, which is a real state rather than a gap: not every item has a key.
            item_issue_id (str | Unset): The id the refused item was acting on, where one had been resolved before the
                refusal. ABSENT when the refusal happened before resolution — a create whose id was never minted, or a ref that
                resolved to nothing.

                IT IS NOT `issue_id`, and the divergence is load-bearing rather than verbose: `issue_id` is a PRESENCE-
                DISCRIMINATING member of the `dependency_cycle` hierarchy refusal, so a batch operation reusing it would make
                that discriminator fire on refusals it says nothing about.
            declared_later (bool | Unset): With `invalid_argument` on a batch operation whose items may name each other:
                whether the unresolvable key IS declared by the request, at a LATER index.

                True is an ORDERING mistake — a key reaches backward only — and false is a key nothing in the request declares,
                which is a typo or a missing item. A client acts differently on each. Both polarities are emitted and the member
                is never omitted to mean false: an absent member says the refusal was not about a key at all.
            since (int | Unset): With `events_journal_truncated`: the checkpoint the reported window begins after.

                It is NOT always the value the request sent. In the ordinary case — the prefix you asked for was pruned — it IS
                your checkpoint. When the prefix is intact but the retained window has an interior hole, it is instead the last
                seq the server could serve contiguously from your checkpoint, and `floor` is where the next intact stretch
                begins. It never reports a value BELOW what you sent, so echoing it back can never re-deliver records you
                already hold.
            floor (int | Unset): With `events_journal_truncated`: the lowest seq still retained, or `head + 1` when the
                journal retains nothing at all. Resuming from `floor - 1` continues with a known, explicit gap.
            head (int | Unset): With `events_journal_truncated`: the highest seq this journal has ever assigned. It never
                decreases under a prune, so `floor > head` means the journal was pruned empty and the caller is at the end of
                its history. Emitted even when zero.
            server_project_id (str | Unset): With `invalid_argument` / `reason: "project_mismatch"` ONLY: the project id
                this server actually serves, so a client that stamped a `Bd-Project-Id` can tell a wrong-server refusal from a
                malformed one without parsing `detail`. It is set on that refusal and on no other, and never on a refusal raised
                before the stamp is checked — the Host gate, or a deployment's authentication layer — so its PRESENCE is the
                signal that this specific check fired.
    """

    status: int
    title: str
    code: str
    request_id: str
    type_: str | Unset = UNSET
    detail: str | Unset = UNSET
    param: str | Unset = UNSET
    reason: str | Unset = UNSET
    assignee: str | Unset = UNSET
    issue_status: str | Unset = UNSET
    open_children: int | Unset = UNSET
    existing_type: str | Unset = UNSET
    requested_type: str | Unset = UNSET
    issue_id: str | Unset = UNSET
    blocker_id: str | Unset = UNSET
    blocker_is_ancestor: bool | Unset = UNSET
    expected_version: str | Unset = UNSET
    actual_version: str | Unset = UNSET
    expected_status: str | Unset = UNSET
    actual_status: str | Unset = UNSET
    expected_assignee: str | Unset = UNSET
    actual_assignee: str | Unset = UNSET
    item_index: int | Unset = UNSET
    item_kind: str | Unset = UNSET
    item_key: str | Unset = UNSET
    item_issue_id: str | Unset = UNSET
    declared_later: bool | Unset = UNSET
    since: int | Unset = UNSET
    floor: int | Unset = UNSET
    head: int | Unset = UNSET
    server_project_id: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status

        title = self.title

        code = self.code

        request_id = self.request_id

        type_ = self.type_

        detail = self.detail

        param = self.param

        reason = self.reason

        assignee = self.assignee

        issue_status = self.issue_status

        open_children = self.open_children

        existing_type = self.existing_type

        requested_type = self.requested_type

        issue_id = self.issue_id

        blocker_id = self.blocker_id

        blocker_is_ancestor = self.blocker_is_ancestor

        expected_version = self.expected_version

        actual_version = self.actual_version

        expected_status = self.expected_status

        actual_status = self.actual_status

        expected_assignee = self.expected_assignee

        actual_assignee = self.actual_assignee

        item_index = self.item_index

        item_kind = self.item_kind

        item_key = self.item_key

        item_issue_id = self.item_issue_id

        declared_later = self.declared_later

        since = self.since

        floor = self.floor

        head = self.head

        server_project_id = self.server_project_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "status": status,
                "title": title,
                "code": code,
                "request_id": request_id,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_
        if detail is not UNSET:
            field_dict["detail"] = detail
        if param is not UNSET:
            field_dict["param"] = param
        if reason is not UNSET:
            field_dict["reason"] = reason
        if assignee is not UNSET:
            field_dict["assignee"] = assignee
        if issue_status is not UNSET:
            field_dict["issue_status"] = issue_status
        if open_children is not UNSET:
            field_dict["open_children"] = open_children
        if existing_type is not UNSET:
            field_dict["existing_type"] = existing_type
        if requested_type is not UNSET:
            field_dict["requested_type"] = requested_type
        if issue_id is not UNSET:
            field_dict["issue_id"] = issue_id
        if blocker_id is not UNSET:
            field_dict["blocker_id"] = blocker_id
        if blocker_is_ancestor is not UNSET:
            field_dict["blocker_is_ancestor"] = blocker_is_ancestor
        if expected_version is not UNSET:
            field_dict["expected_version"] = expected_version
        if actual_version is not UNSET:
            field_dict["actual_version"] = actual_version
        if expected_status is not UNSET:
            field_dict["expected_status"] = expected_status
        if actual_status is not UNSET:
            field_dict["actual_status"] = actual_status
        if expected_assignee is not UNSET:
            field_dict["expected_assignee"] = expected_assignee
        if actual_assignee is not UNSET:
            field_dict["actual_assignee"] = actual_assignee
        if item_index is not UNSET:
            field_dict["item_index"] = item_index
        if item_kind is not UNSET:
            field_dict["item_kind"] = item_kind
        if item_key is not UNSET:
            field_dict["item_key"] = item_key
        if item_issue_id is not UNSET:
            field_dict["item_issue_id"] = item_issue_id
        if declared_later is not UNSET:
            field_dict["declared_later"] = declared_later
        if since is not UNSET:
            field_dict["since"] = since
        if floor is not UNSET:
            field_dict["floor"] = floor
        if head is not UNSET:
            field_dict["head"] = head
        if server_project_id is not UNSET:
            field_dict["server_project_id"] = server_project_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = d.pop("status")

        title = d.pop("title")

        code = d.pop("code")

        request_id = d.pop("request_id")

        type_ = d.pop("type", UNSET)

        detail = d.pop("detail", UNSET)

        param = d.pop("param", UNSET)

        reason = d.pop("reason", UNSET)

        assignee = d.pop("assignee", UNSET)

        issue_status = d.pop("issue_status", UNSET)

        open_children = d.pop("open_children", UNSET)

        existing_type = d.pop("existing_type", UNSET)

        requested_type = d.pop("requested_type", UNSET)

        issue_id = d.pop("issue_id", UNSET)

        blocker_id = d.pop("blocker_id", UNSET)

        blocker_is_ancestor = d.pop("blocker_is_ancestor", UNSET)

        expected_version = d.pop("expected_version", UNSET)

        actual_version = d.pop("actual_version", UNSET)

        expected_status = d.pop("expected_status", UNSET)

        actual_status = d.pop("actual_status", UNSET)

        expected_assignee = d.pop("expected_assignee", UNSET)

        actual_assignee = d.pop("actual_assignee", UNSET)

        item_index = d.pop("item_index", UNSET)

        item_kind = d.pop("item_kind", UNSET)

        item_key = d.pop("item_key", UNSET)

        item_issue_id = d.pop("item_issue_id", UNSET)

        declared_later = d.pop("declared_later", UNSET)

        since = d.pop("since", UNSET)

        floor = d.pop("floor", UNSET)

        head = d.pop("head", UNSET)

        server_project_id = d.pop("server_project_id", UNSET)

        problem = cls(
            status=status,
            title=title,
            code=code,
            request_id=request_id,
            type_=type_,
            detail=detail,
            param=param,
            reason=reason,
            assignee=assignee,
            issue_status=issue_status,
            open_children=open_children,
            existing_type=existing_type,
            requested_type=requested_type,
            issue_id=issue_id,
            blocker_id=blocker_id,
            blocker_is_ancestor=blocker_is_ancestor,
            expected_version=expected_version,
            actual_version=actual_version,
            expected_status=expected_status,
            actual_status=actual_status,
            expected_assignee=expected_assignee,
            actual_assignee=actual_assignee,
            item_index=item_index,
            item_kind=item_kind,
            item_key=item_key,
            item_issue_id=item_issue_id,
            declared_later=declared_later,
            since=since,
            floor=floor,
            head=head,
            server_project_id=server_project_id,
        )

        problem.additional_properties = d
        return problem

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
