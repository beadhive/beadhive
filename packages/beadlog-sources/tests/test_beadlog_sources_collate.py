"""beadlog-sources: collate B1 indexes and B2 reference resolution (``bdlg.collation/1``)."""

from __future__ import annotations

import json

import pytest

import beadlog
from beadlog_sources import (
    AMBIGUOUS,
    FOREIGN,
    LOCAL,
    REF_ARTIFACT,
    UNRESOLVED,
    CollationFormatError,
    build_indexes,
    collate,
    collation_schema,
    dumps_collation,
    loads_collation,
    read_collation,
    write_collation,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000aa", "bh")
T = "2026-01-0{}T00:00:00Z"

A, B, C, D, E, F = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40)
M, M2 = "1" * 40, "2" * 40


def _baseline(*ids: str) -> list[beadlog.BeadEvent]:
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


def _commits():
    # main: A <- B <- M(merge of branch) <- M2 (merge of nested), branch: B <- C <- D(merge) ...
    return [
        ev.change(A, occurred_at=T.format(1), paths=["src/a.py"], parents=[]),
        ev.change(B, occurred_at=T.format(2), paths=["docs/x.md", "src/a.py"], parents=[A]),
        ev.change(C, occurred_at=T.format(3), paths=["src/c.py"], parents=[B]),
        ev.change(D, occurred_at=T.format(4), paths=["docs/x.md"], parents=[C]),
        ev.change(M, occurred_at=T.format(5), parents=[B, D], kind="merge"),
        ev.change(E, occurred_at=T.format(6), paths=["src/e.py"], parents=[M]),
        ev.change(F, occurred_at=T.format(7), paths=["src/f.py"], parents=[E]),
    ]


def _corpus(*extra):
    return ev.corpus(
        *_commits(),
        ev.review(7, commits=[C, D[:8]]),
        ev.tracker_item("12"),
        ev.design_record("docs/x.md"),
        ev.release_marker("v1", commit_range=f"{A}..{M}"),
        *extra,
    )


def test_indexes() -> None:
    ix = build_indexes(_corpus())
    key = lambda sha: f"git:commit/{sha}"  # noqa: E731
    assert ix.by_sha[C] == key(C)
    assert ix.artifact("gh:pr/7").key == "gh:pr/7"
    # the merge brings in the second-parent side only
    assert ix.merge_children[key(M)] == (key(C), key(D))
    assert key(M) in ix.merge_children and key(E) not in ix.merge_children
    assert ix.pr_commits["gh:pr/7"] == (key(C), key(D))
    assert ix.pr_of_commit[key(D)] == ("gh:pr/7",)
    assert ix.path_changes["docs/x.md"] == (key(B), key(D))
    assert ix.doc_add["doc:docs/x.md"] == key(B)
    # (A, M] by ancestry
    assert ix.tag_range["git:tag/v1"] == (key(B), key(C), key(D), key(M))


def test_tag_without_range_is_empty() -> None:
    ix = build_indexes(ev.corpus(ev.change(A), ev.release_marker("v0")))
    assert ix.tag_range["git:tag/v0"] == ()


def test_nested_merge_children_are_all_included() -> None:
    inner = ev.change(F, occurred_at=T.format(4), parents=[A], kind="merge")
    ix = build_indexes(
        ev.corpus(
            ev.change(A),
            ev.change(B, parents=[A]),
            ev.change(C, occurred_at=T.format(2), parents=[A]),
            ev.change(D, occurred_at=T.format(3), parents=[C]),
            ev.change(E, occurred_at=T.format(5), parents=[A, D], kind="merge"),
            inner,
        )
    )
    assert ix.merge_children[f"git:commit/{E}"] == (f"git:commit/{C}", f"git:commit/{D}")


def _refs(*refs: ev.Ref):
    carrier = ev.design_record("docs/carrier.md", refs=refs)
    c = collate(
        ev.corpus(
            *_commits(),
            ev.review(7),
            ev.tracker_item("12"),
            carrier,
            ev.release_marker("v1", commit_range=f"{A}..{M}"),
            ev.design_record("docs/x.md"),
        ),
        _baseline("bh-1", "shared-1"),
        InMemoryLedgerDirectory(
            {"hq": ["hq-9", "shared-1"], "ops": ["ops-1", "dup-1"], "inf": ["dup-1"]}
        ),
        local="bh",
    )
    return {r.ref.raw: r for r in c.refs_of("doc:docs/carrier.md")}


def test_bead_token_outcomes() -> None:
    out = _refs(
        ev.ref("bh-1"),
        ev.ref("hq-9"),
        ev.ref("shared-1"),
        ev.ref("dup-1"),
        ev.ref("bh-999"),
        ev.ref("hq-1"),
    )
    assert (out["bh-1"].outcome, out["bh-1"].ledger) == (LOCAL, "bh")
    assert (out["hq-9"].outcome, out["hq-9"].ledger) == (FOREIGN, "hq")
    assert (out["shared-1"].outcome, out["shared-1"].ledger) == (LOCAL, "bh")
    assert out["shared-1"].candidates == ("hq",)
    assert out["dup-1"].outcome == AMBIGUOUS and out["dup-1"].candidates == ("inf", "ops")
    assert out["bh-999"].outcome == UNRESOLVED
    # an "hq-" prefix is not an hq bead: nothing resolves by prefix
    assert out["hq-1"].outcome == UNRESOLVED and out["hq-1"].ledger is None


def test_artifact_ref_kinds() -> None:
    out = _refs(
        ev.ref(C, "sha"),
        ev.ref(D[:8], "sha"),
        ev.ref("deadbee", "sha"),
        ev.ref("#7", "pr"),
        ev.ref("gh:pr/8", "pr"),
        ev.ref("12", "issue"),
        ev.ref("#13", "issue"),
        ev.ref("docs/x.md", "path"),
        ev.ref("./src/c.py", "path"),
        ev.ref("nope.py", "path"),
        ev.ref("v1", "tag"),
        ev.ref("v2", "tag"),
        ev.ref("https://example.invalid/x", "url"),
        ev.ref("gh:pr/7", "url"),
    )
    assert out[C].targets == (f"git:commit/{C}",) and out[C].outcome == REF_ARTIFACT
    assert out[D[:8]].targets == (f"git:commit/{D}",)
    assert out["deadbee"].outcome == UNRESOLVED
    assert out["#7"].targets == ("gh:pr/7",)
    assert out["gh:pr/8"].outcome == UNRESOLVED
    assert out["12"].targets == ("gh:issue/12",)
    assert out["#13"].outcome == UNRESOLVED
    assert out["docs/x.md"].targets == ("doc:docs/x.md",)
    assert out["./src/c.py"].targets == (f"git:commit/{C}",)
    assert out["nope.py"].outcome == UNRESOLVED
    assert out["v1"].targets == ("git:tag/v1",) and out["v2"].outcome == UNRESOLVED
    assert out["https://example.invalid/x"].outcome == UNRESOLVED
    assert out["gh:pr/7"].targets == ("gh:pr/7",)  # a raw artifact key resolves whatever the kind


def test_ambiguous_short_sha() -> None:
    a1, a2 = "abcdef1" + "0" * 33, "abcdef1" + "1" * 33
    c = collate(
        ev.corpus(
            ev.change(a1),
            ev.change(a2),
            ev.design_record("docs/y.md", refs=[ev.ref("abcdef1", "sha")]),
        ),
        [],
        InMemoryLedgerDirectory({}),
        local="bh",
    )
    (r,) = c.refs_of("doc:docs/y.md")
    assert r.outcome == AMBIGUOUS and len(r.targets) == 2


def test_directory_loads_each_ledger_once() -> None:
    d = InMemoryLedgerDirectory({"hq": ["hq-1"]})
    refs = [ev.ref("hq-1"), ev.ref("hq-1", where="body"), ev.ref("x-1")]
    collate(ev.corpus(ev.design_record("docs/z.md", refs=refs)), [], d, local="bh")
    assert d.loads == {"hq": 1}


def test_collation_is_deterministic_and_round_trips(tmp_path) -> None:
    def run():
        return collate(
            _corpus(ev.design_record("docs/q.md", refs=[ev.ref("bh-1"), ev.ref(C, "sha")])),
            _baseline("bh-1"),
            InMemoryLedgerDirectory({"hq": ["hq-1"]}),
            local="bh",
        )

    first, second = run(), run()
    data = dumps_collation(first)
    assert data == dumps_collation(second) and data.endswith(b"\n")
    assert loads_collation(data) == first
    obj = json.loads(data)
    assert obj["schema"] == "bdlg.collation/1"
    assert obj["conflicts"] == []  # the opt-in clock rules are off by default
    # B5 runs by default (judge off, no threshold): units are decided or residual, and NEW only
    # by the derived no-candidate rule.
    assert all(v["step"] == "no-candidate" for v in obj["verdicts"] if v["class"] == "NEW")
    decided = {v["unit"] for v in obj["verdicts"]} | {r["unit"] for r in obj["residual"]}
    assert decided == {u["key"] for u in obj["units"]}
    assert [u["unit"] for u in obj["lifecycle"]] == [u["key"] for u in obj["units"]]
    assert obj["units"] and loads_collation(data).units == first.units
    path = tmp_path / "c.json"
    write_collation(first, path)
    assert read_collation(path) == first and first.indexes is not None


def test_loader_refuses_malformed() -> None:
    good = json.loads(
        dumps_collation(
            collate(ev.corpus(ev.change(A)), [], InMemoryLedgerDirectory({}), local="bh")
        )
    )
    with pytest.raises(CollationFormatError):
        loads_collation(b"nope")
    with pytest.raises(CollationFormatError):
        loads_collation(json.dumps({**good, "schema": "bdlg.collation/2"}).encode())
    with pytest.raises(CollationFormatError):
        loads_collation(json.dumps({**good, "extra": 1}).encode())
    with pytest.raises(CollationFormatError):
        loads_collation(json.dumps(good, indent=2).encode() + b"\n")  # not canonical


def test_schema_describes_output() -> None:
    schema = collation_schema()
    assert schema["$id"].endswith("bdlg-collation:1")
    out = json.loads(
        dumps_collation(collate(_corpus(), [], InMemoryLedgerDirectory({}), local="bh"))
    )
    assert sorted(out) == sorted(schema["required"])


def test_linear_indexing_over_large_corpus() -> None:
    n = 2000
    shas = [f"{i:040x}" for i in range(n)]
    arts = [
        ev.change(
            s,
            occurred_at=T.format(1),
            parents=[shas[i - 1]] if i else [],
            paths=[f"src/{i % 50}.py"],
        )
        for i, s in enumerate(shas)
    ]
    ix = build_indexes(ev.corpus(*arts))
    assert len(ix.by_sha) == n and len(ix.path_changes) == 50
