"""Repository label definitions (``labels.read`` / ``labels.define``), offline.

Against the ``MockTransport`` fake (``tracker_github_fake.FakeGitHub``): every refusal happens
before any request (and before the credential is resolved), names are percent-encoded on the
wire, a define converges, and the grant is single-use. Against the recorded replays
(``recorded/label_definitions_*.json``): the paged listing, one create / update batch, one
``delete_labels`` batch whose ``204 No Content`` is a known success, and the refusals GitHub's
documentation is silent on (a duplicate name, a missing permission, a delete reported "not
found"). The ``204``-is-success rule of the one write seam is pinned at the transport too.
Nothing here reaches GitHub, reads a clock or sleeps.
"""

from __future__ import annotations

import inspect
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    AuthenticationFailed,
    IssueKey,
    LabelDefiner,
    LabelDefinition,
    LabelNotOwned,
    LabelPruner,
    NotVisible,
    Operation,
    OperationNotEnabled,
    PayloadRejected,
    PermissionDenied,
    ProjectRef,
    RateLimited,
    RetryBudget,
    WriteGrant,
    WriteOutcomeUnknown,
)
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import (
    LabelDefining,
    adapter_class,
    build_adapter,
    parse_settings,
)
from beadhive_tracker_github.adapter.label_definitions import definition_payload
from beadhive_tracker_github.adapter.transport import Transport
from tracker_github_fake import FakeGitHub, adapter_for

DEFINE, READ = Operation.LABELS_DEFINE, Operation.LABELS_READ
PROJECT = ProjectRef("github", "acme/widgets")
RECORDED = Path(__file__).resolve().parent / "recorded"
TOKEN = "ghp_" + "Rec0rdedF1xtureT0ken" * 2

ACCEPTED = LabelDefinition("bh:accepted", "0e8a16", "Accepted for triage")
TRIAGE = LabelDefinition("bh:triage", "fbca04", "Waiting for triage")
HUMAN = LabelDefinition("bug", "d73a4a", "Something is not working")


def grant(grant_id: str = "g-1", *, target: object = PROJECT, op: Operation = DEFINE):
    return WriteGrant(grant_id, op, target)  # type: ignore[arg-type]


class Tokens:
    """A token source that counts how often it was asked (a refusal must not ask)."""

    def __init__(self, world: FakeGitHub | None = None, *, anonymous: bool = False) -> None:
        self.calls = 0
        self._token = None if anonymous else (world.token if world else TOKEN)

    def __call__(self) -> ResolvedToken | None:
        self.calls += 1
        return None if self._token is None else ResolvedToken(self._token, "env:GITHUB_TOKEN")


def definer(world: FakeGitHub, *, enabled=None, prefixes=("bh:",), tokens=None, budget=None):
    return adapter_for(
        world,
        enabled=READ_OPERATIONS | {DEFINE} if enabled is None else enabled,
        label_prefixes=prefixes,
        tokens=tokens,
        budget=budget,
    )


def bodies(world: FakeGitHub) -> list[tuple[str, str, Any]]:
    """Every write sent: ``(method, raw path, decoded JSON body or None)``."""
    return [
        (r.method, r.url.raw_path.decode(), json.loads(r.content) if r.content else None)
        for r in world.requests
        if r.method != "GET"
    ]


# ---- refusals: before any request, before the credential ------------------------------------


def test_with_labels_read_disabled_the_listing_refuses_before_any_request():
    world = FakeGitHub()
    world.seed_label_definition(HUMAN)
    tokens = Tokens(world)
    adapter = adapter_for(world, enabled=READ_OPERATIONS - {READ}, tokens=tokens)

    with pytest.raises(OperationNotEnabled, match="labels.read is not enabled"):
        adapter.list_label_definitions()
    assert world.requests == [] and tokens.calls == 0


def test_with_labels_define_disabled_the_built_adapter_has_no_definer():
    world = FakeGitHub()
    adapter = adapter_for(world, label_prefixes=("bh:",))
    assert not isinstance(adapter, LabelDefiner) and not hasattr(adapter, "define_labels")
    assert world.requests == []


def test_a_composed_definer_whose_flag_is_off_refuses_before_any_request_or_credential():
    """Defence in depth: even a composed definer re-checks its own enabled set."""
    world = FakeGitHub()
    tokens = Tokens(world)
    adapter = adapter_class({DEFINE})(
        project=PROJECT,
        token_source=tokens,
        sleep=world.sleep,
        clock=world.clock.now,
        enabled=READ_OPERATIONS,  # labels.define is NOT enabled
        label_prefixes=("bh:",),
        transport=world.transport(),
    )
    assert isinstance(adapter, LabelDefining)

    with pytest.raises(OperationNotEnabled, match="labels.define is not enabled"):
        adapter.define_labels((ACCEPTED,), grant=grant())
    assert world.requests == [] and tokens.calls == 0
    assert world.label_definitions() == ()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "g-1",
        grant(target=IssueKey("#1", "https://github.com/acme/widgets/issues/1")),
        grant(target=ProjectRef("github", "someone-else/elsewhere")),
        grant(op=Operation.LABELS_CHANGE),
    ],
    ids=["missing", "string", "issue-scoped", "another-repository", "another-operation"],
)
def test_a_missing_or_mismatched_grant_refuses_before_any_request(bad):
    world = FakeGitHub()
    tokens = Tokens(world)
    adapter = definer(world, tokens=tokens)
    with pytest.raises(OperationNotEnabled):
        adapter.define_labels((ACCEPTED,), grant=bad)
    assert world.requests == [] and tokens.calls == 0


@pytest.mark.parametrize(
    ("definitions", "prefixes"),
    [
        ((HUMAN,), ("bh:",)),
        ((LabelDefinition("x-bh:accepted", "ffffff", ""),), ("bh:",)),
        ((ACCEPTED, HUMAN), ("bh:",)),  # one foreign name refuses the batch
        ((LabelDefinition("BH:upper", "ffffff", ""),), ("bh:",)),
        ((ACCEPTED,), None),  # no configured ownership owns nothing
        ((ACCEPTED,), ()),
    ],
    ids=["human", "lookalike", "mixed", "case", "unconfigured", "empty-prefixes"],
)
def test_a_name_outside_the_configured_prefixes_refuses_before_any_request(definitions, prefixes):
    world = FakeGitHub()
    world.seed_label_definition(HUMAN)
    tokens = Tokens(world)
    adapter = definer(world, prefixes=prefixes, tokens=tokens)
    with pytest.raises(LabelNotOwned):
        adapter.define_labels(definitions, grant=grant())
    assert world.requests == [] and tokens.calls == 0
    assert world.label_definitions() == (HUMAN,)


@pytest.mark.parametrize(
    "definitions",
    [
        (LabelDefinition("bh:a", "#0e8a16", ""),),
        (LabelDefinition("bh:a", "0e8a1", ""),),
        (LabelDefinition("bh:a", "zzzzzz", ""),),
        (LabelDefinition("bh:a", "0e8a16", "x" * 101),),
        (LabelDefinition(" bh:a", "0e8a16", ""),),
        (LabelDefinition("", "0e8a16", ""),),
        (ACCEPTED, LabelDefinition("bh:Accepted", "000000", "")),
        ("bh:a",),
    ],
    ids=[
        "hash-color",
        "short-color",
        "not-hex",
        "long-description",
        "padded-name",
        "empty-name",
        "duplicate-by-case",
        "not-a-definition",
    ],
)
def test_a_malformed_batch_is_refused_before_any_request_or_grant(definitions):
    world = FakeGitHub()
    adapter = definer(world)
    with pytest.raises(ValueError):
        adapter.define_labels(definitions, grant=grant())
    assert world.requests == []
    adapter.define_labels((ACCEPTED,), grant=grant())  # the refusal spent nothing
    assert world.label_definitions() == (ACCEPTED,)


def test_a_description_of_exactly_one_hundred_characters_is_accepted():
    world = FakeGitHub()
    longest = LabelDefinition("bh:long", "ABCDEF", "d" * 100)
    definer(world).define_labels((longest,), grant=grant())
    assert world.label_definitions() == (LabelDefinition("bh:long", "abcdef", "d" * 100),)
    assert definition_payload(longest) == {
        "name": "bh:long",
        "color": "abcdef",
        "description": "d" * 100,
    }


def test_an_anonymous_define_is_refused_locally_and_keeps_the_grant():
    world = FakeGitHub()
    adapter = definer(world, tokens=Tokens(anonymous=True))
    with pytest.raises(AuthenticationFailed, match="needs a credential"):
        adapter.define_labels((ACCEPTED,), grant=grant("g-kept"))
    assert world.requests == []
    # The same grant still works once a credential resolves (it was never spent).
    definer(world).define_labels((ACCEPTED,), grant=grant("g-kept"))
    assert world.label_definitions() == (ACCEPTED,)


def test_a_failed_read_of_the_live_definitions_does_not_burn_the_grant():
    world = FakeGitHub()
    budget = RetryBudget(max_retries=0)
    adapter = definer(world, budget=budget)
    world.throttle(requests=1, retry_after_seconds=5)
    with pytest.raises(RateLimited):
        adapter.define_labels((ACCEPTED,), grant=grant("g-1"))
    assert world.write_requests() == 0
    adapter.define_labels((ACCEPTED,), grant=grant("g-1"))  # not spent: nothing was written
    assert world.write_requests() == 1 and world.label_definitions() == (ACCEPTED,)


# ---- the wire: list, create, update, delete ---------------------------------------------------


def test_the_listing_reads_every_page_of_at_most_one_hundred():
    world = FakeGitHub()
    names = [f"bh:n{n:03d}" for n in range(150)]
    for name in names:
        world.seed_label_definition(LabelDefinition(name, "ededed", ""))
    world.seed_label_definition(HUMAN)

    listed = adapter_for(world).list_label_definitions()

    assert [d.name for d in listed] == sorted([*names, "bug"])
    gets = [r for r in world.requests if r.method == "GET"]
    assert len(gets) == 2 and world.write_requests() == 0
    assert dict(parse_qsl(gets[0].url.query.decode())) == {"per_page": "100"}
    assert gets[1].url.path == "/repositories/9001/labels"  # Link: rel="next", verbatim


def test_a_define_creates_and_updates_owned_labels_and_spares_the_rest():
    world = FakeGitHub()
    stale = LabelDefinition("bh:stale", "cccccc", "")
    for definition in (HUMAN, TRIAGE, stale):
        world.seed_label_definition(definition)
    changed = LabelDefinition("bh:triage", "000000", "Re-described")

    receipt = definer(world).define_labels((ACCEPTED, changed), grant=grant("g-ok"))

    assert receipt.grant_id == "g-ok" and receipt.operation is DEFINE
    assert receipt.target == PROJECT
    assert set(world.label_definitions()) == {HUMAN, ACCEPTED, changed, stale}  # no prune here
    assert bodies(world) == [
        (
            "POST",
            "/repos/acme/widgets/labels",
            {"name": "bh:accepted", "color": "0e8a16", "description": "Accepted for triage"},
        ),
        (
            "PATCH",
            "/repos/acme/widgets/labels/bh%3Atriage",
            {"color": "000000", "description": "Re-described"},
        ),
    ]


def test_delete_labels_deletes_owned_labels_everywhere_and_spares_human_ones():
    world = FakeGitHub()
    for definition in (HUMAN, TRIAGE, LabelDefinition("bh:stale", "cccccc", "")):
        world.seed_label_definition(definition)
    issue = world.add_issue("thread", at=datetime(2026, 1, 2, tzinfo=UTC), labels=("bh:stale",))

    receipt = definer(world).delete_labels(("bh:stale", "bh:absent"), grant=grant("g-ok"))

    assert receipt.grant_id == "g-ok" and receipt.operation is DEFINE
    assert receipt.target == PROJECT
    assert set(world.label_definitions()) == {HUMAN, TRIAGE}
    assert world.server_labels(issue) == frozenset()  # GitHub removes a deleted label too
    assert bodies(world) == [("DELETE", "/repos/acme/widgets/labels/bh%3Astale", None)]


def test_the_adapter_is_a_label_pruner_only_with_labels_define_and_define_has_no_prune():
    world = FakeGitHub()
    assert isinstance(definer(world), LabelPruner)
    assert not isinstance(adapter_for(world, label_prefixes=("bh:",)), LabelPruner)
    assert list(inspect.signature(LabelDefining.define_labels).parameters) == [
        "self",
        "definitions",
        "grant",
    ]
    assert list(inspect.signature(LabelDefining.delete_labels).parameters) == [
        "self",
        "names",
        "grant",
    ]


@pytest.mark.parametrize(
    ("names", "prefixes"),
    [
        (("bug",), ("bh:",)),  # never prune a human label
        (("bh:stale", "bug"), ("bh:",)),  # one foreign name refuses the batch
        (("BH:stale",), ("bh:",)),
        (("bh:stale",), None),  # no configured ownership owns nothing
        (("bh:stale",), ()),
    ],
    ids=["human", "mixed", "case", "unconfigured", "empty-prefixes"],
)
def test_a_deleted_name_outside_the_configured_prefixes_refuses_before_any_request(names, prefixes):
    world = FakeGitHub()
    world.seed_label_definition(HUMAN)
    tokens = Tokens(world)
    adapter = definer(world, prefixes=prefixes, tokens=tokens)
    with pytest.raises(LabelNotOwned):
        adapter.delete_labels(names, grant=grant())
    assert world.requests == [] and tokens.calls == 0
    assert world.label_definitions() == (HUMAN,)


@pytest.mark.parametrize("names", [("",), (" bh:a",), ("bh:a", ""), ("bh:a", 3)])
def test_a_malformed_deletion_is_refused_before_any_request_or_grant(names):
    world = FakeGitHub()
    world.seed_label_definition(LabelDefinition("bh:a", "cccccc", ""))
    adapter = definer(world)
    with pytest.raises(ValueError):
        adapter.delete_labels(names, grant=grant())
    assert world.requests == []
    adapter.delete_labels(("bh:a",), grant=grant())  # the refusal spent nothing
    assert world.label_definitions() == ()


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(grant(target=IssueKey("github", "acme/widgets", "#1")), id="issue-scope"),
        pytest.param(grant(target=ProjectRef("github", "other/repo")), id="other-project"),
        pytest.param(grant(op=Operation.LABELS_CHANGE), id="other-operation"),
        pytest.param("g-1", id="not-a-grant"),
    ],
)
def test_a_mismatched_grant_refuses_a_deletion_before_any_request(bad):
    world = FakeGitHub()
    tokens = Tokens(world)
    with pytest.raises(OperationNotEnabled):
        definer(world, tokens=tokens).delete_labels(("bh:stale",), grant=bad)
    assert world.requests == [] and tokens.calls == 0


def test_a_deletion_uses_its_grant_once_even_when_the_label_is_already_gone():
    world = FakeGitHub()
    adapter = definer(world)
    adapter.delete_labels(("bh:absent",), grant=grant("g-1"))
    assert world.write_requests() == 0  # converged: nothing to delete, nothing sent
    with pytest.raises(OperationNotEnabled, match="already used"):
        adapter.delete_labels(("bh:absent",), grant=grant("g-1"))


def test_an_anonymous_deletion_is_refused_locally_and_keeps_the_grant():
    world = FakeGitHub()
    world.seed_label_definition(LabelDefinition("bh:stale", "cccccc", ""))
    with pytest.raises(AuthenticationFailed, match="needs a credential"):
        definer(world, tokens=Tokens(anonymous=True)).delete_labels(
            ("bh:stale",), grant=grant("g-kept")
        )
    assert world.requests == []
    definer(world).delete_labels(("bh:stale",), grant=grant("g-kept"))
    assert world.label_definitions() == ()


def test_a_foreign_case_variant_is_never_deleted_and_keeps_the_grant():
    world = FakeGitHub()
    world.seed_label_definition(LabelDefinition("BH:stale", "cccccc", "a human's label"))
    adapter = definer(world)
    with pytest.raises(LabelNotOwned, match="BH:stale"):
        adapter.delete_labels(("bh:stale",), grant=grant("g-1"))
    assert world.write_requests() == 0
    with pytest.raises(LabelNotOwned):  # refused before the spend: not "already used"
        adapter.delete_labels(("bh:stale",), grant=grant("g-1"))


def test_an_ambiguous_deletion_stops_the_batch_and_is_never_retried():
    world = FakeGitHub()
    for name in ("bh:a", "bh:b"):
        world.seed_label_definition(LabelDefinition(name, "cccccc", ""))
    adapter = definer(world)
    world.fail_next_write_ambiguously(applied=True)
    with pytest.raises(WriteOutcomeUnknown):
        adapter.delete_labels(("bh:a", "bh:b"), grant=grant("g-1"))
    assert world.write_requests() == 1  # the second delete was never sent
    adapter.delete_labels(("bh:a", "bh:b"), grant=grant("g-2"))  # a fresh grant converges
    assert world.label_definitions() == ()


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("bh:area/ci", "bh%3Aarea%2Fci"),
        ("bh:needs review", "bh%3Aneeds%20review"),
        ("bh:50%", "bh%3A50%25"),
        ("bh:a?b#c", "bh%3Aa%3Fb%23c"),
        ("bh:café", "bh%3Acaf%C3%A9"),
    ],
)
def test_label_names_are_percent_encoded_in_every_path(name, encoded):
    world = FakeGitHub()
    world.seed_label_definition(LabelDefinition(name, "cccccc", "old"))
    world.seed_label_definition(LabelDefinition(name + "-gone", "cccccc", ""))
    adapter = definer(world)

    adapter.define_labels((LabelDefinition(name, "dddddd", "new"),), grant=grant("g-1"))
    adapter.delete_labels((name + "-gone",), grant=grant("g-2"))

    assert [(m, p) for m, p, _ in bodies(world)] == [
        ("PATCH", f"/repos/acme/widgets/labels/{encoded}"),
        ("DELETE", f"/repos/acme/widgets/labels/{encoded}-gone"),
    ]
    assert world.label_definitions() == (LabelDefinition(name, "dddddd", "new"),)


def test_an_owned_label_differing_only_in_case_is_renamed_not_duplicated():
    world = FakeGitHub()
    world.seed_label_definition(LabelDefinition("bh:Accepted", "0e8a16", "Accepted for triage"))
    definer(world).define_labels((ACCEPTED,), grant=grant())
    assert world.label_definitions() == (ACCEPTED,)
    assert bodies(world) == [
        (
            "PATCH",
            "/repos/acme/widgets/labels/bh%3AAccepted",
            {"new_name": "bh:accepted", "color": "0e8a16", "description": "Accepted for triage"},
        )
    ]


def test_a_foreign_case_variant_is_never_renamed_and_keeps_the_grant():
    world = FakeGitHub()
    world.seed_label_definition(LabelDefinition("BH:accepted", "0e8a16", "a human's label"))
    adapter = definer(world)
    with pytest.raises(LabelNotOwned, match="BH:accepted"):
        adapter.define_labels((ACCEPTED,), grant=grant("g-1"))
    assert world.write_requests() == 0
    # Refused before the spend: a second call is refused for ownership again, not for reuse.
    with pytest.raises(LabelNotOwned):
        adapter.define_labels((ACCEPTED,), grant=grant("g-1"))


def test_a_converged_batch_sends_no_write_but_uses_the_grant():
    world = FakeGitHub()
    world.seed_label_definition(ACCEPTED)
    adapter = definer(world)
    adapter.define_labels((ACCEPTED,), grant=grant("g-1"))
    assert world.write_requests() == 0
    with pytest.raises(OperationNotEnabled, match="already used"):
        adapter.define_labels((ACCEPTED,), grant=grant("g-1"))


def test_an_ambiguous_write_stops_the_batch_and_is_never_retried():
    world = FakeGitHub()
    adapter = definer(world)
    world.fail_next_write_ambiguously(applied=True)
    with pytest.raises(WriteOutcomeUnknown):
        adapter.define_labels((ACCEPTED, TRIAGE), grant=grant("g-1"))
    assert world.write_requests() == 1  # the second create was never sent
    assert world.label_definitions() == (ACCEPTED,)
    adapter.define_labels((ACCEPTED, TRIAGE), grant=grant("g-2"))  # a fresh grant converges
    assert world.write_requests() == 2
    assert set(world.label_definitions()) == {ACCEPTED, TRIAGE}


# ---- configuration -----------------------------------------------------------------------------


def _settings(read: bool, define: bool, prefixes: list[str] | None) -> dict[str, Any]:
    labels: dict[str, Any] = {"enabled": False}
    if prefixes is not None:
        labels["owned_prefixes"] = prefixes
    return {
        "enabled": True,
        "read": {"labels": {"enabled": read}},
        "write": {"labels": labels, "label_definitions": {"enabled": define}},
    }


def test_the_flag_keys_enable_labels_read_and_labels_define():
    parsed = parse_settings(_settings(True, True, ["bh:"]))
    assert {READ, DEFINE} <= parsed.enabled
    assert Operation.LABELS_CHANGE not in parsed.enabled
    off = parse_settings(_settings(False, False, ["bh:"]))
    assert not {READ, DEFINE} & off.enabled


@pytest.mark.parametrize("prefixes", [None, []], ids=["unset", "empty"])
def test_labels_define_needs_an_owned_prefix(prefixes):
    assert DEFINE not in parse_settings(_settings(True, True, prefixes)).enabled


def test_a_settings_built_definer_is_bounded_by_the_configured_prefixes():
    world = FakeGitHub()
    adapter = build_adapter(
        _settings(True, True, ["bh:intake/"]),
        lambda: ResolvedToken(world.token, "env:GH_TOKEN"),
        project=PROJECT,
        sleep=world.sleep,
        clock=world.clock.now,
        transport=world.transport(),
    )
    assert isinstance(adapter, LabelDefiner)
    with pytest.raises(LabelNotOwned):
        adapter.define_labels((ACCEPTED,), grant=grant())
    owned = LabelDefinition("bh:intake/accepted", "0e8a16", "Accepted")
    adapter.define_labels((owned,), grant=grant("g-2"))
    assert adapter.list_label_definitions() == (owned,)


# ---- the one write seam: 204 No Content is a known success ------------------------------------


def _session_reply(response: httpx.Response, *, no_content: bool = False):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return response

    transport = Transport(
        token_source=lambda: ResolvedToken(TOKEN, "env:GITHUB_TOKEN"),
        api_base="https://api.github.com",
        budget=RetryBudget(max_retries=0),
        sleep=lambda _s: pytest.fail("no retry"),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )
    with transport.operation(DEFINE, write=True) as session:
        reply = session.send("DELETE", "/repos/acme/widgets/labels/x", no_content=no_content)
    assert len(sent) == 1
    return reply


def test_a_204_with_no_body_is_a_known_success_not_an_unknown_outcome():
    reply = _session_reply(httpx.Response(204))
    assert reply.status == 204 and reply.body is None


def test_an_empty_2xx_is_a_success_only_where_the_endpoint_documents_204():
    assert _session_reply(httpx.Response(200), no_content=True).body is None
    with pytest.raises(WriteOutcomeUnknown, match="not JSON"):
        _session_reply(httpx.Response(200))
    with pytest.raises(WriteOutcomeUnknown, match="not JSON"):  # a body that is not JSON
        _session_reply(httpx.Response(200, content=b"<html>"), no_content=True)


# ---- recorded exchanges --------------------------------------------------------------------------


def _shape(url: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc, parts.path, tuple(sorted(parse_qsl(parts.query)))


class Replay:
    """Serve recorded exchanges in order; a request that is not the recorded one (method, raw
    URL with its percent-encoding, query order-insensitive, and JSON body) is a mismatch."""

    def __init__(self, interactions: list[dict[str, Any]]) -> None:
        self.interactions = deque(interactions)
        self.mismatches: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.interactions:
            self.mismatches.append(f"unexpected extra request {request.method} {request.url}")
            return httpx.Response(418, json={"message": "replay exhausted"})
        expected = self.interactions.popleft()
        want = expected["request"]
        body = json.loads(request.content) if request.content else None
        if (
            request.method != want["method"]
            or _shape(str(request.url)) != _shape(want["url"])
            or body != want.get("json")
        ):
            self.mismatches.append(f"sent {request.method} {request.url} {body}, recorded {want}")
            return httpx.Response(418, json={"message": "replay mismatch"})
        response = expected["response"]
        return httpx.Response(
            response["status"], headers=response["headers"], json=response["json"]
        )

    def assert_done(self) -> None:
        assert self.mismatches == []
        assert not self.interactions, f"{len(self.interactions)} recorded exchanges unused"


def _load(name: str) -> dict[str, Any]:
    document = json.loads((RECORDED / name).read_text(encoding="utf-8"))
    assert document["provenance"].startswith("Synthetic"), name
    return document


def _recorded_adapter(replay: Replay, *, enabled=READ_OPERATIONS | {DEFINE}):
    return adapter_class(enabled)(
        project=PROJECT,
        token_source=lambda: ResolvedToken(TOKEN, "env:GITHUB_TOKEN"),
        sleep=lambda _s: pytest.fail("a recorded exchange never needs a retry"),
        enabled=enabled,
        label_prefixes=("bh:",),
        transport=httpx.MockTransport(replay.handler),
    )


def test_the_recorded_listing_is_paged_and_follows_the_link_verbatim():
    replay = Replay(_load("label_definitions_list_paged.json")["interactions"])
    listed = _recorded_adapter(replay, enabled=READ_OPERATIONS).list_label_definitions()
    replay.assert_done()
    assert listed == (
        LabelDefinition("bh:intake/accepted", "0e8a16", "Accepted for triage"),
        LabelDefinition("bh:type/feature", "a2eeef", ""),  # a null description is ""
        LabelDefinition("bug", "d73a4a", "Something isn't working"),
    )


def test_the_recorded_define_creates_and_updates():
    replay = Replay(_load("label_definitions_define.json")["interactions"])
    receipt = _recorded_adapter(replay).define_labels(
        (
            LabelDefinition("bh:intake/accepted", "0e8a16", "Accepted for triage"),  # unchanged
            LabelDefinition("bh:progress/started", "fbca04", "Work has started"),  # update
            LabelDefinition("bh:type/feature", "a2eeef", "New feature or request"),  # create
        ),
        grant=grant("g-rec"),
    )
    replay.assert_done()  # the bodies and the percent-encoded paths are the recorded ones
    assert receipt.grant_id == "g-rec" and receipt.target == PROJECT


def test_the_recorded_delete_of_an_owned_label_is_a_204_success_and_sends_nothing_for_absent():
    replay = Replay(_load("label_definitions_delete.json")["interactions"])
    receipt = _recorded_adapter(replay).delete_labels(
        ("bh:absent", "bh:stale value"), grant=grant("g-rec")
    )
    replay.assert_done()  # one read, then the one percent-encoded DELETE, whose 204 is a success
    assert receipt.grant_id == "g-rec" and receipt.target == PROJECT


REFUSALS = _load("label_definitions_refusals.json")["cases"]


def test_every_recorded_refusal_names_its_evidence_honestly():
    for exchanges in REFUSALS.values():
        for exchange in exchanges:
            assert exchange["provenance"].startswith(("Documented form only", "Undocumented"))
            assert "re-verify on the first live labels.define" in exchange["provenance"].lower()


@pytest.mark.parametrize(
    ("case", "method", "argument", "error", "text"),
    [
        (
            "duplicate-name",
            "define_labels",
            (LabelDefinition("bh:type/feature", "a2eeef", "New feature or request"),),
            PayloadRejected,
            "422 payload rejected after authorization (Validation Failed)",
        ),
        (
            "no-write-permission",
            "define_labels",
            (LabelDefinition("bh:type/feature", "a2eeef", "New feature or request"),),
            PermissionDenied,
            "403 token lacks the permission (endpoint needs issues=write; pull_requests=write)",
        ),
        ("delete-forbidden-as-not-found", "delete_labels", ("bh:stale",), NotVisible, "404"),
    ],
)
def test_each_recorded_refusal_raises_its_port_error(case, method, argument, error, text):
    replay = Replay(REFUSALS[case])
    with pytest.raises(error) as raised:
        getattr(_recorded_adapter(replay), method)(argument, grant=grant())
    replay.assert_done()
    assert str(raised.value).startswith(text)
    if error is PermissionDenied:
        assert raised.value.needed == "issues=write; pull_requests=write"


def test_a_recorded_delete_of_a_label_already_gone_converges():
    replay = Replay(REFUSALS["delete-already-gone"])
    receipt = _recorded_adapter(replay).delete_labels(("bh:stale",), grant=grant())
    replay.assert_done()
    assert receipt.operation is DEFINE
