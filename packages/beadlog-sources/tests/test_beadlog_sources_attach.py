"""beadlog-sources: collate B4, attaching design records to work units."""

from __future__ import annotations

import json

import pytest

import beadlog
from beadlog_sources import (
    ATTACH_REASONS,
    ATTACH_TIERS,
    Attachment,
    AttachOptions,
    AttachOptionsError,
    ClusterOptions,
    CollationFormatError,
    IdentifyOptions,
    StatusOptions,
    attach,
    claimed_artifacts,
    collate,
    collation_schema,
    dumps_collation,
    loads_collation,
)
from beadlog_sources.attach import tokens
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000aa", "bh")


def sha(n: int) -> str:
    return f"{n:040x}"


def key(n: int) -> str:
    return f"git:commit/{sha(n)}"


def at(n: int) -> str:
    return f"2026-01-01T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"


def commit(n: int, parents=(), *, cc_type="feat", paths=(), refs=(), **kw):
    return ev.change(
        sha(n),
        occurred_at=at(n),
        parents=[sha(p) for p in parents],
        cc_type=cc_type,
        paths=list(paths),
        refs=list(refs),
        **kw,
    )


def doc(path: str, *, refs=(), title=None):
    kw = {"title": title} if title else {}
    return ev.design_record(path, refs=list(refs), **kw)


def baseline(*ids: str) -> list[beadlog.BeadEvent]:
    return [
        beadlog.mint_event(
            "create",
            beadlog.BeadRef(GROUP.id, bead),
            {"type": "task", "fields": {"title": f"bead {bead}"}},
            occurred_at="2026-07-01T00:00:00Z",
            recorded_at="2026-07-01T00:00:00Z",
            extracted_at="2026-10-31T00:00:00Z",
            source=("bd", f"bd:create/{bead}"),
            provenance="observed",
        )
        for bead in ids
    ]


def run(*artifacts, beads=(), cluster=None, **options):
    return collate(
        ev.corpus(*artifacts),
        baseline(*beads),
        InMemoryLedgerDirectory({"hq": ["hq-9"]}),
        local="bh",
        options=ClusterOptions(**(cluster or {})),
        attach_options=AttachOptions(**options),
    )


def att(col):
    return {a.doc: a for a in col.attachments}


def test_explicit_doc_refs_unit_member() -> None:
    col = run(
        commit(1, paths=["src/a.py"]),
        doc("docs/d.md", refs=[ev.ref(sha(1), "sha", "body")]),
    )
    a = att(col)["doc:docs/d.md"]
    assert (a.unit, a.reason, a.evidence) == (key(1), "explicit", (key(1),))
    assert col.unit("doc:docs/d.md") is None


def test_explicit_doc_refs_pr_and_local_bead() -> None:
    col = run(
        commit(1, paths=["src/a.py"]),
        ev.review(7, commits=[sha(1)], title="PR seven"),
        commit(2, refs=[ev.ref("bh-abc")], paths=["src/b.py"], cc_type="fix"),
        doc("docs/p.md", refs=[ev.ref("#7", "pr", "body")]),
        doc("docs/b.md", refs=[ev.ref("bh-abc", "bead-token", "frontmatter")]),
        beads=("bh-abc",),
    )
    got = att(col)
    assert (got["doc:docs/p.md"].unit, got["doc:docs/p.md"].evidence) == ("gh:pr/7", ("gh:pr/7",))
    assert got["doc:docs/b.md"].unit == "bead:bh-abc"
    assert got["doc:docs/b.md"].evidence == ("bead:bh-abc",)
    assert {a.reason for a in got.values()} == {"explicit"}


def test_explicit_unit_cites_doc() -> None:
    col = run(
        commit(1, paths=["src/a.py"], refs=[ev.ref("docs/d.md", "path", "body")]),
        doc("docs/d.md"),
    )
    a = att(col)["doc:docs/d.md"]
    assert (a.unit, a.reason, a.evidence) == (key(1), "explicit", (key(1),))


def test_explicit_reaches_plan_epic_others_do_not() -> None:
    plan = [
        ev.plan_item("p-epic", title="Epic items", order=0),
        ev.plan_item("p-kid", title="Kid item", parent="p-epic", order=1),
    ]
    col = run(*plan, doc("docs/e.md", refs=[ev.ref("plan:p-epic", "tag")]))
    # a raw value that is an artifact key resolves to that artifact whatever its kind
    assert col.unit("plan:p-epic").kind == "epic"
    assert att(col)["doc:docs/e.md"].unit == "plan:p-epic"
    # lexical never lands on an epic: same words, no explicit ref -> standalone
    col = run(*plan, doc("docs/epic-items.md", title="Epic items"))
    assert att(col)["doc:docs/epic-items.md"].reason == "standalone"


def test_same_change_commit_adds_doc_and_code() -> None:
    col = run(commit(1, paths=["src/a.py", "docs/d.md"]), doc("docs/d.md"))
    a = att(col)["doc:docs/d.md"]
    assert (a.unit, a.reason, a.evidence) == (key(1), "same-change", (key(1),))


def test_same_change_via_pr_and_needs_code() -> None:
    docs_only = commit(2, [1], paths=["docs/d.md"], cc_type="docs")
    col = run(
        commit(1, paths=["src/a.py"]),
        docs_only,
        ev.review(7, commits=[sha(1), sha(2)], title="PR seven"),
        doc("docs/d.md"),
    )
    a = att(col)["doc:docs/d.md"]
    assert (a.unit, a.reason) == ("gh:pr/7", "same-change")
    assert a.evidence == tuple(sorted((key(2), "gh:pr/7")))
    # a lone docs commit in a unit with no code behind it is no same-change evidence
    lone = run(commit(3, paths=["docs/d.md"], cc_type="feat"), doc("docs/d.md"))
    assert att(lone)["doc:docs/d.md"].reason == "standalone"


def test_shared_path_later_commit_and_path_ref() -> None:
    col = run(
        commit(1, paths=["docs/d.md"], cc_type="docs"),
        commit(2, [1], paths=["docs/d.md", "src/z.py"], cc_type="feat"),
        commit(3, [2], paths=["src/m.py"], cc_type="feat"),
        doc("docs/d.md"),
        doc("docs/m.md", refs=[ev.ref("src/m.py", "path", "body")]),
    )
    got = att(col)
    assert (got["doc:docs/d.md"].unit, got["doc:docs/d.md"].reason) == (key(2), "shared-path")
    assert got["doc:docs/d.md"].evidence == (key(2),)
    assert (got["doc:docs/m.md"].unit, got["doc:docs/m.md"].evidence) == (key(3), (key(3),))
    assert got["doc:docs/m.md"].reason == "shared-path"


def test_lexical_and_threshold() -> None:
    arts = [
        commit(1, paths=["src/a.py"], title="feat: Quantum scheduler gizmo rewrite"),
        doc("docs/quantum-scheduler.md", title="Quantum scheduler gizmo handler"),
    ]
    col = run(*arts)
    a = att(col)["doc:docs/quantum-scheduler.md"]
    assert (a.unit, a.reason, a.evidence) == (key(1), "lexical", (key(1),))
    # a stricter threshold or a higher token floor refuses it; dropping the tier too
    for opts in ({"lexical_percent": 100}, {"lexical_min_tokens": 4}, {"tiers": ["explicit"]}):
        other = att(run(*arts, **opts))["doc:docs/quantum-scheduler.md"]
        assert (other.reason, other.unit) == ("standalone", "doc:docs/quantum-scheduler.md")


def test_tokens_drop_noise() -> None:
    assert tokens("feat(hq): Add the Widget v2 scheduler 123") == {"widget", "scheduler"}


def test_precedence_explicit_beats_same_change_beats_shared_beats_lexical() -> None:
    # unit 1 (feat) adds the doc with code; unit 2 later touches it; unit 3 shares its words;
    # unit 4 cites it explicitly. Each tier is dropped in turn to expose the next.
    arts = [
        commit(1, paths=["src/a.py", "docs/d.md"], title="feat: unrelated one"),
        commit(2, [1], paths=["docs/d.md"], title="feat: unrelated two"),
        commit(3, [2], paths=["src/c.py"], title="feat: gadget frobnicator design"),
        commit(
            4,
            [3],
            paths=["src/e.py"],
            title="feat: unrelated four",
            refs=[ev.ref("docs/d.md", "path", "body")],
        ),
        doc("docs/d.md", title="Gadget frobnicator design"),
    ]
    order = []
    tiers = list(ATTACH_TIERS)
    while tiers:
        a = att(run(*arts, tiers=tiers))["doc:docs/d.md"]
        order.append((a.reason, a.unit))
        tiers.pop(0)
    assert order == [
        ("explicit", key(4)),
        ("same-change", key(1)),
        ("shared-path", key(2)),
        ("lexical", key(3)),
    ]
    assert att(run(*arts, tiers=[]))["doc:docs/d.md"].reason == "standalone"


def test_standalone_doc_unit_and_opt_out() -> None:
    col = run(commit(1, paths=["src/a.py"]), doc("docs/solo.md", title="Zzz yyy"))
    unit = col.unit("doc:docs/solo.md")
    assert (unit.kind, unit.rule, unit.title, unit.artifacts) == (
        "issue",
        "doc",
        "Zzz yyy",
        ("doc:docs/solo.md",),
    )
    assert att(col)["doc:docs/solo.md"] == Attachment(
        "doc:docs/solo.md", "doc:docs/solo.md", "standalone", ()
    )
    assert col.docs_of("doc:docs/solo.md") == (att(col)["doc:docs/solo.md"],)
    off = run(commit(1, paths=["src/a.py"]), doc("docs/solo.md", title="Zzz yyy"), standalone=False)
    assert off.unit("doc:docs/solo.md") is None and off.attachments == ()


def test_each_doc_attached_once_and_deterministic() -> None:
    arts = [
        commit(1, paths=["src/a.py", "docs/a.md"]),
        commit(2, [1], paths=["src/b.py"], refs=[ev.ref("docs/a.md", "path", "body")]),
        doc("docs/a.md", refs=[ev.ref(sha(1), "sha"), ev.ref(sha(2), "sha")]),
        doc("docs/b.md"),
        doc("docs/c.md"),
    ]
    col = run(*arts)
    docs = [a.doc for a in col.attachments]
    assert docs == sorted(set(docs)) == ["doc:docs/a.md", "doc:docs/b.md", "doc:docs/c.md"]
    # attached docs are not unit members; doc-units claim only themselves
    assert "doc:docs/a.md" not in claimed_artifacts(col.units)
    assert len(claimed_artifacts(col.units)) == sum(len(u.artifacts) for u in col.units)
    assert dumps_collation(col) == dumps_collation(run(*reversed(arts)))
    # explicit ties on evidence count go to the smaller unit key
    assert att(col)["doc:docs/a.md"].unit == key(1)


def test_options_validation_and_mapping() -> None:
    assert AttachOptions().tiers == ATTACH_TIERS
    assert AttachOptions.from_mapping({"lexical_percent": 100, "other": 3}).lexical_percent == 100
    for bad in (
        {"tiers": "explicit"},
        {"tiers": ["guess"]},
        {"tiers": ["lexical", "lexical"]},
        {"lexical_percent": 0},
        {"lexical_percent": 101},
        {"lexical_percent": 0.6},
        {"lexical_percent": True},
        {"lexical_min_tokens": 0},
        {"standalone": 1},
    ):
        with pytest.raises(AttachOptionsError):
            AttachOptions(**bad)
    with pytest.raises(AttachOptionsError):
        AttachOptions.from_json({"tiers": []})
    with pytest.raises(CollationFormatError):
        attach(loads_collation(dumps_collation(run(doc("docs/a.md")))))


def test_options_section_round_trip() -> None:
    arts = [commit(1, paths=["src/a.py"], title="feat: gizmo handler"), doc("docs/gizmo.md")]
    default = run(*arts)
    assert default.options == {
        "cluster": ClusterOptions().to_json(),
        "attach": AttachOptions().to_json(),
        "identify": IdentifyOptions().to_json(),
        "status": StatusOptions().to_json(),
    }
    variants = [
        run(*arts, lexical_percent=90),
        run(*arts, lexical_min_tokens=3),
        run(*arts, tiers=["explicit"]),
        run(*arts, standalone=False),
    ]
    data = [dumps_collation(c) for c in (default, *variants)]
    assert len(set(data)) == len(data)
    for col in (default, *variants):
        loaded = loads_collation(dumps_collation(col))
        assert (
            loaded == col
            and loaded.options == col.options
            and loaded.attachments == col.attachments
        )
        assert AttachOptions.from_json(loaded.options["attach"]) == AttachOptions(
            **col.options["attach"]
        )


def test_schema_and_strict_loading() -> None:
    col = run(
        commit(1, paths=["src/a.py", "docs/a.md"]),
        doc("docs/a.md"),
        doc("docs/solo.md", title="Zzz"),
    )
    obj = json.loads(dumps_collation(col))
    assert [a["reason"] for a in obj["attachments"]] == ["same-change", "standalone"]
    assert set(ATTACH_REASONS) >= {a["reason"] for a in obj["attachments"]}
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(collation_schema())
    validator.validate(obj)

    def load(**change):
        return loads_collation(beadlog.canonical_json({**obj, **change}).encode() + b"\n")

    first = obj["attachments"][0]
    for bad in (
        {"attachments": [{**first, "reason": "guess"}]},
        {"attachments": [{**first, "extra": 1}]},
        {"attachments": [{k: v for k, v in first.items() if k != "evidence"}]},
        {"options": {"cluster": obj["options"]["cluster"]}},
        {"options": {**obj["options"], "attach": {**obj["options"]["attach"], "tiers": ["x"]}}},
    ):
        with pytest.raises(CollationFormatError):
            load(**bad)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate({**obj, **bad})
    # standalone exactly when the unit is the doc itself: a loader rule, beyond the schema
    with pytest.raises(CollationFormatError):
        load(attachments=[{**first, "reason": "standalone"}])
    missing = {k: v for k, v in obj.items() if k != "attachments"}
    with pytest.raises(CollationFormatError):
        loads_collation(beadlog.canonical_json(missing).encode() + b"\n")
