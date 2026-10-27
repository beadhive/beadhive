"""The leak check and the sanitizer seam (``bh-6wj9y`` R10): R14 cases X12 and X13.

X12 is the leak matrix: every finding kind with and without an override, through the machine
(draft holds, override publishes, approve records the overrides with the actor), plus the
never-overridable kinds. X13 is marker privacy: an encoded marker and a rendered payload with no
override match no fleet bead-id pattern. Pure; no tracker, no store.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.domain import (
    ApprovalRequest,
    Capabilities,
    DraftRequest,
    IntakeRefused,
    IssueBinding,
    LabelBase,
    LabelIntent,
    LeakKind,
    LeakPolicy,
    Message,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxEvent,
    OutboxPolicy,
    OwnMark,
    RefusalReason,
    RenderContext,
    leak_check,
    leaks,
    marker_bead_ids,
    mint_outbox_id,
)
from beadhive.modules.intake.domain import outbox as ob
from beadhive.testing.intake import RecordingSanitizer, SequentialIdSource

URL = "https://tracker.example/acme/widgets/issues/7"
BINDING = IssueBinding("acme-r1", "memory", "acme/widgets", URL, "#7")
FLEET = frozenset({"bh", "acme", "pub", "hq"})
POLICY = LeakPolicy(
    bead_prefixes=FLEET,
    private_dimensions=frozenset({"intake", "origin", "dispatch", "review", "delivery"}),
    owned_label_prefixes=("beadhive:",),
    path_roots=("/srv/hives",),
)
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
ON = OutboxPolicy(enabled=True)
ALL = Capabilities(frozenset({"comment.create", "labels.change", "comments.read"}))


def ctx(**changes) -> RenderContext:
    base = RenderContext(
        URL, POLICY, RecordingSanitizer.of([SECRET]), labels=LabelBase("r1", frozenset())
    )
    return dataclasses.replace(base, **changes)


def kinds(text: str, **kw) -> list[LeakKind]:
    return [
        f.kind for f in leak_check(text, POLICY, sanitizer=RecordingSanitizer.of([SECRET]), **kw)
    ]


# -- the pure check, kind by kind --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "match"),
    [
        ("Fixed in bh-g62yn.", LeakKind.BEAD_ID, "bh-g62yn"),
        ("see acme-24ay9.3", LeakKind.BEAD_ID, "acme-24ay9.3"),
        ("mirrored from pub-k2", LeakKind.BEAD_ID, "pub-k2"),
        ("on wt/bead/issue/x", LeakKind.BRANCH, "wt/bead/issue/x"),
        ("on origin/wt/batch/g1", LeakKind.BRANCH, "wt/batch/g1"),
        ("from mol/release-7", LeakKind.BRANCH, "mol/release-7"),
        ("labelled intake:untriaged", LeakKind.PRIVATE_LABEL, "intake:untriaged"),
        ("now review:pending", LeakKind.PRIVATE_LABEL, "review:pending"),
        ("by dev/intake-i8", LeakKind.SEAT, "dev/intake-i8"),
        ("ask disp/intake", LeakKind.SEAT, "disp/intake"),
        ("see /home/brian/x.log", LeakKind.PATH, "/home/brian/x.log"),
        ("in .bh/state.json", LeakKind.PATH, ".bh/state.json"),
        ("under /srv/hives/acme", LeakKind.PATH, "/srv/hives/acme"),
        ("this fixes #12", LeakKind.CLOSING_KEYWORD, "fixes #12"),
        ("Closes acme/widgets#3", LeakKind.CLOSING_KEYWORD, "Closes acme/widgets#3"),
    ],
)
def test_each_leak_class_is_found(text, kind, match) -> None:
    findings = leak_check(text, POLICY, sanitizer=RecordingSanitizer())
    assert [(f.kind, f.match) for f in findings] == [(kind, match)]
    assert findings[0].id == f"{kind.value}:1"


def test_a_secret_is_found_and_never_repeated() -> None:
    (finding,) = leak_check(
        f"token {SECRET} here", POLICY, sanitizer=RecordingSanitizer.of([SECRET])
    )
    assert finding.kind is LeakKind.SECRET and finding.match == leaks.REDACTED
    assert SECRET not in repr(finding) and not finding.overridable


def test_no_sanitizer_is_a_never_overridable_finding() -> None:
    (finding,) = leak_check("hello", POLICY, sanitizer=None)
    assert finding.kind is LeakKind.NO_SANITIZER and not finding.overridable


def test_label_not_owned_is_a_never_overridable_finding() -> None:
    findings = leak_check("add: x", POLICY, sanitizer=RecordingSanitizer(), labels=["triage:x"])
    assert [(f.kind, f.match, f.overridable) for f in findings] == [
        (LeakKind.LABEL_NOT_OWNED, "triage:x", False)
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Thanks! This shipped in v1.2.0; see https://github.com/acme/widgets/releases/tag/v1.2.0",
        "read-only mode and e-mail are fine; so is ob1_abc and gh-pages",
        "the dev team reviewed it; release notes follow",
        "https://github.com/acme/widgets/issues/7",
    ],
)
def test_ordinary_public_text_passes(text) -> None:
    assert kinds(text) == []


def test_findings_are_numbered_per_kind_in_text_order() -> None:
    findings = leak_check("bh-aa1 then acme-bb2 by dev/x", POLICY, sanitizer=RecordingSanitizer())
    assert [f.id for f in findings] == ["bead_id:1", "bead_id:2", "seat:1"]
    assert [f.match for f in findings[:2]] == ["bh-aa1", "acme-bb2"]


def test_a_policy_without_fleet_prefixes_finds_no_bead_ids() -> None:
    bare = LeakPolicy(bead_prefixes=frozenset())
    assert leak_check("bh-g62yn", bare, sanitizer=RecordingSanitizer()) == ()


def test_unresolved_ignores_warnings_and_published_findings() -> None:
    findings = leak_check("bh-aa1 fixes #2", POLICY, sanitizer=RecordingSanitizer())
    published = leaks.Override("bead_id:1", LeakKind.BEAD_ID, "bh-aa1", "alice")
    assert [f.kind for f in leaks.unresolved(findings)] == [LeakKind.BEAD_ID]
    assert leaks.unresolved(findings, [published]) == ()


# -- X12: the leak matrix through the machine --------------------------------------------------

OVERRIDABLE = [
    ("Fixed in bh-g62yn.", LeakKind.BEAD_ID),
    ("on wt/bead/issue/bh-x", LeakKind.BRANCH),
    ("labelled intake:untriaged", LeakKind.PRIVATE_LABEL),
    ("assigned to dev/intake-i8", LeakKind.SEAT),
    ("see /home/brian/x.log", LeakKind.PATH),
]


def draft(text: str, render: RenderContext | None = None, **kw) -> tuple[OutboxDocument, str]:
    oid = SequentialIdSource().outbox_id()
    request = DraftRequest(
        BINDING, OutboxEvent.REPLY, "r1", "outbox", message=Message("m", text), **kw
    )
    applied = ob.draft(OutboxDocument(), request, outbox_id=oid, policy=ON, ctx=render or ctx())
    return applied.document, oid


def approve(
    doc: OutboxDocument,
    oid: str,
    render: RenderContext | None = None,
    kind: OpKind = OpKind.COMMENT,
) -> OutboxDocument:
    render = render or ctx()
    digest = ob.render(doc.record(oid), kind, render).short_digest
    return ob.approve(
        doc,
        ApprovalRequest(oid, kind, digest, "alice"),
        grant_id=SequentialIdSource().grant_id(),
        policy=ON,
        ctx=render,
        capabilities=ALL,
    ).document


@pytest.mark.parametrize(("text", "kind"), OVERRIDABLE)
def test_x12_an_overridable_leak_holds_until_deliberately_published(text, kind) -> None:
    doc, oid = draft(text)
    op = doc.record(oid).op(OpKind.COMMENT)
    assert op.state is OpState.HELD
    finding = next(f for f in op.findings if f.kind is kind)

    with pytest.raises(IntakeRefused) as caught:
        approve(doc, oid)
    assert caught.value.reason is RefusalReason.OUTBOX_TRANSITION

    names = [f.id for f in op.findings]
    doc = ob.override(doc, oid, OpKind.COMMENT, names, actor="alice", policy=ON, ctx=ctx()).document
    doc = approve(doc, oid)
    approval = doc.record(oid).op(OpKind.COMMENT).approval
    assert any(o.kind is kind and o.match == finding.match for o in approval.overrides)
    assert {o.actor for o in approval.overrides} == {"alice"}


def test_x12_a_secret_never_passes() -> None:
    doc, oid = draft(f"my token is {SECRET}")
    op = doc.record(oid).op(OpKind.COMMENT)
    assert op.state is OpState.HELD and [f.kind for f in op.findings] == [LeakKind.SECRET]
    with pytest.raises(IntakeRefused) as caught:
        ob.override(doc, oid, OpKind.COMMENT, ["secret:1"], actor="alice", policy=ON, ctx=ctx())
    assert caught.value.reason is RefusalReason.NEVER_OVERRIDABLE
    assert SECRET not in repr(op.findings)


def test_x12_no_sanitizer_never_passes() -> None:
    doc, oid = draft("Thanks!", render=ctx(sanitizer=None))
    with pytest.raises(IntakeRefused) as caught:
        ob.override(
            doc,
            oid,
            OpKind.COMMENT,
            ["no_sanitizer:1"],
            actor="alice",
            policy=ON,
            ctx=ctx(sanitizer=None),
        )
    assert caught.value.reason is RefusalReason.NEVER_OVERRIDABLE


def test_x12_a_label_not_owned_never_passes() -> None:
    # At draft: refused outright (O1's guard).
    with pytest.raises(IntakeRefused) as caught:
        draft("x", comment=False, labels=LabelIntent(add=frozenset({"wontfix"})))
    assert caught.value.reason is RefusalReason.LABEL_NOT_OWNED

    # Owned at draft, no longer owned when rendered: held as a finding nobody can publish.
    doc, oid = draft("x", comment=False, labels=LabelIntent(add=frozenset({"beadhive:merged"})))
    narrowed = ctx(policy=dataclasses.replace(POLICY, owned_label_prefixes=("bot:",)))
    rendered = ob.render(doc.record(oid), OpKind.LABELS, narrowed)
    assert [f.kind for f in rendered.findings] == [LeakKind.LABEL_NOT_OWNED]
    held = ob.recheck(doc, oid, OpKind.LABELS, ctx=narrowed, actor="outbox").document
    assert held.record(oid).op(OpKind.LABELS).state is OpState.HELD
    with pytest.raises(IntakeRefused) as caught:
        ob.override(
            held, oid, OpKind.LABELS, ["label_not_owned:1"], actor="alice", policy=ON, ctx=narrowed
        )
    assert caught.value.reason is RefusalReason.NEVER_OVERRIDABLE


def test_x12_a_closing_keyword_warns_and_never_holds() -> None:
    doc, oid = draft("This fixes #12.")
    op = doc.record(oid).op(OpKind.COMMENT)
    assert op.state is OpState.DRAFTED and op.findings[0].warning
    assert approve(doc, oid).record(oid).op(OpKind.COMMENT).state is OpState.APPROVED


def test_x12_a_publish_covers_only_the_text_it_named() -> None:
    doc, oid = draft("Fixed in bh-aa1.")
    doc = ob.override(
        doc, oid, OpKind.COMMENT, ["bead_id:1"], actor="alice", policy=ON, ctx=ctx()
    ).document
    doc = ob.edit(doc, oid, actor="alice", policy=ON, ctx=ctx(), text="Fixed in bh-bb2.").document
    assert doc.record(oid).op(OpKind.COMMENT).state is OpState.HELD


# -- X13: marker privacy -----------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 2**64 + 7, 2**128 - 1])
def test_x13_an_outbox_id_never_has_a_bead_id_shape(n) -> None:
    oid = mint_outbox_id(n.to_bytes(16, "big"))
    marker = OwnMark("acme/widgets", "5f0c6a3e-2b8d-4f7e-9a51-0d6c2b8e4f10", oid)
    assert ob.is_outbox_id(oid) and len(oid) == 30
    assert marker_bead_ids(marker, FLEET | {"ob1", "ob"}) == ()
    assert kinds(oid) == []


def test_x13_a_rendered_payload_with_its_marker_matches_no_bead_id() -> None:
    ids = SequentialIdSource()
    oid = ids.outbox_id()
    line = f"<!-- bh:v1 hive=acme/widgets install=5f0c6a3e outbox={oid} -->"
    render = ctx(marker_line=line)
    doc, _ = draft("Thanks, this shipped in v1.2.", render=render)
    record = next(iter(doc.records.values()))
    rendered = ob.render(record, OpKind.COMMENT, render)
    assert rendered.payload.endswith(line)
    assert rendered.findings == ()
    assert ids.grant_id() and kinds(ids.grant_id()) == []


def test_x13_a_bead_id_is_never_accepted_as_an_outbox_id() -> None:
    with pytest.raises(IntakeRefused) as caught:
        ob.draft(
            OutboxDocument(),
            DraftRequest(BINDING, OutboxEvent.REPLY, "r", "o", message=Message("m", "hi")),
            outbox_id="acme-r1",
            policy=ON,
            ctx=ctx(),
        )
    assert caught.value.reason is RefusalReason.INVALID_OUTBOX_ID
    with pytest.raises(ValueError):
        mint_outbox_id(b"short")
