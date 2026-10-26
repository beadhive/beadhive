"""Identity and the read-only capability probe (tracker-adapter-port-adr.md section 2).

* The actor is keyed ``(numeric id, kind)``; ``login`` is display only and is never compared.
* The write probe is GET-only and tri-state: ``NO`` definitive, ``YES`` only "not refuted",
  ``UNKNOWN`` for unscoped tokens; it never writes, and a ``NO`` for a write is cached for the
  process lifetime while ``YES`` and ``UNKNOWN`` are never cached as fact.
"""

from __future__ import annotations

import httpx
import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    Actor,
    ActorKey,
    ActorKind,
    AuthenticationFailed,
    Operation,
    Tri,
)
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import mapping
from beadhive_tracker_github.adapter.identity import RepositoryFacts, probe_operation
from tracker_github_fake import FakeGitHub, adapter_for

COMMENT, LABELS = Operation.COMMENT_CREATE, Operation.LABELS_CHANGE


def _probe(world: FakeGitHub, **kwargs):
    report = adapter_for(world, **kwargs).probe_capabilities()
    return report, {op: (state.probe, state.reason) for op, state in report.states.items()}


# ---- the actor: numeric id and type, never the login ------------------------------------------


def test_whoami_is_keyed_by_numeric_id_and_type_with_the_login_for_display_only():
    world = FakeGitHub()
    me = adapter_for(world).whoami()
    assert me == Actor(ActorKey("42", ActorKind.HUMAN), display="maintainer")

    world.me_login = "maintainer-renamed"  # a rename changes nothing
    renamed = adapter_for(world).whoami()
    assert renamed == me and renamed.display == "maintainer-renamed"

    world.me_login, world.me_id = "maintainer", 43  # same login, another account
    assert adapter_for(world).whoami() != me


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("User", ActorKind.HUMAN),
        ("Bot", ActorKind.BOT),
        ("Organization", ActorKind.UNKNOWN),
        ("Mannequin", ActorKind.UNKNOWN),
        (None, ActorKind.UNKNOWN),
    ],
)
def test_the_actor_kind_comes_from_type(kind, expected):
    actor = mapping.actor({"id": 7, "login": "x", "type": kind})
    assert actor is not None and actor.key == ActorKey("7", expected)


@pytest.mark.parametrize("ident", ["42", 4.2, True, None])
def test_an_actor_without_a_numeric_id_is_refused_never_keyed_by_login(ident):
    with pytest.raises(mapping.PayloadUnreadable) as caught:
        mapping.actor({"id": ident, "login": "maintainer", "type": "User"})
    assert "maintainer" not in str(caught.value)


def test_anonymous_whoami_is_none_and_sends_nothing():
    world = FakeGitHub()
    assert adapter_for(world, tokens=lambda: None).whoami() is None
    assert world.requests == []


def test_bad_credentials_name_the_provenance_never_the_value():
    world = FakeGitHub()
    wrong = "ghp_" + "W" * 36
    tokens = lambda: ResolvedToken(wrong, "env:GH_TOKEN")  # noqa: E731
    with pytest.raises(AuthenticationFailed) as caught:
        adapter_for(world, tokens=tokens).whoami()
    assert str(caught.value) == (
        "401 bad credentials: the token from env:GH_TOKEN is invalid, revoked or expired"
    )


def test_an_installation_token_has_no_user_actor_yet():
    world = FakeGitHub(installation_token=True, scopes=None)
    assert adapter_for(world).whoami() is None  # App actor discovery is deferred (port ADR)


# ---- the probe: GET only, tri-state ------------------------------------------------------------


def test_the_probe_sends_only_get_and_never_writes():
    world = FakeGitHub()
    report, _ = _probe(world, enabled=READ_OPERATIONS | WRITE_OPERATIONS)
    assert {r.method for r in world.requests} == {"GET"}
    assert [r.url.path for r in world.requests] == ["/user", "/repos/acme/widgets"]
    assert world.write_requests() == 0
    assert report.credential_source == "env:GITHUB_TOKEN"
    assert report.actor == Actor(ActorKey("42", ActorKind.HUMAN))


def test_a_classic_token_with_repo_scope_is_yes_but_no_write_is_enabled_or_usable_by_default():
    report, probes = _probe(FakeGitHub(scopes="repo, read:org"))
    assert probes[COMMENT][0] is probes[LABELS][0] is Tri.YES
    assert "not proven" in probes[COMMENT][1]
    for op in WRITE_OPERATIONS:
        state = report.states[op]
        assert not state.enabled and not state.usable
    assert report.states[LABELS].supported  # W9; comment.create is unsupported until W8
    assert not report.states[COMMENT].supported
    for op in READ_OPERATIONS:
        assert report.states[op].supported and report.states[op].usable


@pytest.mark.parametrize(("private", "comment"), [(False, Tri.YES), (True, Tri.NO)])
def test_public_repo_scope_covers_a_public_repository_only(private, comment):
    world = FakeGitHub(scopes="public_repo")
    world.repo_meta["private"] = private
    _, probes = _probe(world)
    assert probes[COMMENT][0] is comment


def test_an_unscoped_token_is_unknown_never_yes():
    _, probes = _probe(FakeGitHub(scopes=None))
    assert probes[COMMENT][0] is probes[LABELS][0] is Tri.UNKNOWN
    assert "first approved write is the probe" in probes[COMMENT][1]


def test_a_role_below_triage_is_a_definitive_no_for_labels_even_when_unscoped():
    world = FakeGitHub(scopes=None)
    world.repo_meta["permissions"] = {"pull": True, "triage": False, "push": False}
    _, probes = _probe(world)
    assert probes[LABELS] == (Tri.NO, "role on the repository is below triage")
    assert probes[COMMENT][0] is Tri.UNKNOWN


@pytest.mark.parametrize(
    ("meta", "reason"),
    [
        ({"archived": True}, "archived"),
        ({"disabled": True}, "disabled"),
        ({"has_issues": False}, "issues are disabled"),
    ],
)
def test_definitive_no_from_the_repository_state(meta, reason):
    world = FakeGitHub()
    world.repo_meta.update(meta)
    _, probes = _probe(world)
    assert probes[COMMENT][0] is Tri.NO and reason in probes[COMMENT][1]


def test_scopes_lacking_repo_are_a_definitive_no():
    _, probes = _probe(FakeGitHub(scopes="gist, read:org"))
    assert probes[COMMENT] == (
        Tri.NO,
        "token scopes lack repo (or public_repo for a public repository)",
    )


def test_anonymous_reads_a_public_repository_but_cannot_write():
    world = FakeGitHub()
    report, probes = _probe(world, tokens=lambda: None)
    assert report.actor is None and report.credential_source is None
    assert probes[Operation.ISSUES_READ][0] is Tri.YES
    assert probes[COMMENT][0] is Tri.NO and "no credential" in probes[COMMENT][1]
    assert [r.url.path for r in world.requests] == ["/repos/acme/widgets"]


def test_bad_credentials_and_an_invisible_repository_are_definitive_no():
    world = FakeGitHub()
    bad = lambda: ResolvedToken("ghp_" + "Z" * 36, "env:GH_TOKEN")  # noqa: E731
    _, probes = _probe(world, tokens=bad)
    assert all(tri is Tri.NO for tri, _ in probes.values())
    assert "env:GH_TOKEN" in probes[COMMENT][1]

    hidden = FakeGitHub()
    hidden.scripted.extend(
        [httpx.Response(200, json=hidden.me()), httpx.Response(404, json={"message": "Not Found"})]
    )
    _, probes = _probe(hidden)
    assert all(tri is Tri.NO for tri, _ in probes.values())
    assert "invisible to this token" in probes[COMMENT][1]


def test_an_unexpected_answer_is_unknown_not_no():
    world = FakeGitHub()
    world.scripted.extend(
        [httpx.Response(200, json=world.me())]
        + [httpx.Response(502, json={"message": "Bad Gateway"}) for _ in range(4)]
    )
    _, probes = _probe(world)
    assert all(tri is Tri.UNKNOWN for tri, _ in probes.values())


def test_a_write_no_is_cached_for_the_process_but_yes_and_unknown_never_are():
    world = FakeGitHub(scopes=None)  # comment UNKNOWN; labels decided by the role
    world.repo_meta["permissions"] = {"pull": True, "triage": False}
    adapter = adapter_for(world)
    first = adapter.probe_capabilities()
    assert first.states[LABELS].probe is Tri.NO and first.states[COMMENT].probe is Tri.UNKNOWN

    world.repo_meta["permissions"] = {"pull": True, "triage": True, "push": True}
    world.scopes = "repo"
    again = adapter.probe_capabilities()
    assert again.states[LABELS].probe is Tri.NO  # a NO holds for this adapter's lifetime
    assert again.states[COMMENT].probe is Tri.YES  # UNKNOWN was not cached as fact
    assert adapter_for(world).probe_capabilities().states[LABELS].probe is Tri.YES  # new process


def test_probe_operation_is_pure_over_the_repository_facts():
    facts = RepositoryFacts(readable=True, permissions={"push": True}, scopes=frozenset({"repo"}))
    assert probe_operation(Operation.ISSUES_READ, facts)[0] is Tri.YES
    unreadable = RepositoryFacts(readable=False, reason="x", definitive=False)
    assert probe_operation(COMMENT, unreadable) == (Tri.UNKNOWN, "x")
