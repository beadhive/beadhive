"""The ``planning`` reader: GSD parity with the strategy, roadmap nesting, determinism, tokens."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from beadlog import fold
from beadlog_sources import (
    BUILTIN_READERS,
    ExtractRequest,
    PlanningReader,
    PlanningStrategy,
    ReadRequest,
    discover_readers,
)
from beadlog_sources.evidence import Category, build_corpus, dumps_evidence
from beadlog_sources.planning_reader import roadmap_items, slugify
from beadlog_sources.readers import load_reader

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000e2"
TREE = ".planning/phases"
GSD = {
    f"{TREE}/01-foundation/01-01-PLAN.md": (
        "---\ntitle: Base\ndepends_on: []\n---\n# Base bh-ab1c2\n"
    ),
    f"{TREE}/01-foundation/01-01-SUMMARY.md": "done\n",
    f"{TREE}/01-foundation/01-02-PLAN.md": "---\ndepends_on:\n  - 01-01\n---\n# Second\n",
    f"{TREE}/02-features/02-01-PLAN.md": '---\ndepends_on: ["01-01", "99-99"]\n---\n# Third\n',
    f"{TREE}/02-features/02-01-SUMMARY.md": "done\n",
    f"{TREE}/03-empty/.keep": "",
}
ROADMAP = """# Roadmap

## Phase 1
- [x] Setup repo
  - [x] Init git
  - [x] Add CI (depends: init git)
- [ ] Write docs

## Phase 2
- [ ] Ship (depends: Write docs, nothing)
- [ ] Tokens bh-zz9
## Phase 3
### Details
- [ ] Item
- [x] Item
```
- [ ] not an item
```
"""


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = {**os.environ}
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, env=env)


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _read(root: Path, **options: str) -> list:
    return list(PlanningReader().read(ReadRequest(root=root, options=options)))


@pytest.fixture
def gsd(tmp_path: Path) -> Path:
    root = tmp_path / "gsd"
    root.mkdir()
    _write(root, GSD)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A", ":!*SUMMARY.md")
    _git(root, "commit", "-q", "-m", "plans", when="2026-09-01T10:00:00+00:00")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "summaries", when="2026-09-05T12:30:00+00:00")
    return root


def test_registered_as_builtin_reader() -> None:
    assert BUILTIN_READERS["planning"] == "beadlog_sources.planning_reader:PlanningReader"
    assert "planning" in discover_readers()
    assert load_reader(discover_readers()["planning"]).name == "planning"


def test_gsd_parity_with_the_planning_strategy(gsd: Path) -> None:
    arts = _read(gsd, formats="gsd")
    assert all(a.category is Category.PLAN_ITEM and a.provenance == "observed" for a in arts)
    by_path = {a.key.split("#")[0].removeprefix("plan:"): a for a in arts}

    events = PlanningStrategy().extract(
        ExtractRequest(
            root=gsd,
            group=GROUP,
            baseline=(),
            options={"id_prefix": "t"},
            extracted_at="2026-10-31T00:00:00Z",
        )
    )
    states = {ref.id: st for ref, st in fold(events).items()}
    path_of = {st.fields["external_ref"]: ident for ident, st in states.items()}
    assert set(path_of) == set(by_path)  # the same phases and plans
    ident_to_key = {path_of[p]: a.key for p, a in by_path.items()}
    for path, art in by_path.items():
        state = states[path_of[path]]
        parents = [e.target.id for e in state.edges if e.type == "parent-child"]
        blocks = sorted(ident_to_key[e.target.id] for e in state.edges if e.type == "blocks")
        assert (ident_to_key[parents[0]] if parents else None) == art.attrs["parent"], path
        assert blocks == sorted(art.attrs["depends_on"]), path
        assert (state.phase == "closed") == art.attrs["done_marker"], path
        assert state.fields["title"] == art.title, path


def test_gsd_items_have_stable_keys_order_and_dependencies(gsd: Path) -> None:
    arts = {a.key: a for a in _read(gsd, formats="gsd")}
    p1 = f"plan:{TREE}/01-foundation#phase"
    a1 = f"plan:{TREE}/01-foundation/01-01-PLAN.md#01-01"
    a2 = f"plan:{TREE}/01-foundation/01-02-PLAN.md#01-02"
    b1 = f"plan:{TREE}/02-features/02-01-PLAN.md#02-01"
    assert arts[a2].attrs["parent"] == p1 and arts[a2].attrs["depends_on"] == (a1,)
    assert arts[b1].attrs["depends_on"] == (a1,)
    assert arts[b1].raw_attrs["unresolved_depends_on"] == ("99-99",)
    assert [arts[k].attrs["order"] for k in (a1, a2)] == [0, 1]
    assert arts[p1].attrs["done_marker"] is False and arts[p1].attrs["parent"] is None
    assert arts[f"plan:{TREE}/02-features#phase"].attrs["done_marker"] is True
    assert arts[a1].occurred_at == "2026-09-01T10:00:00Z"
    assert any(r.kind == "bead-token" and r.raw == "bh-ab1c2" for r in arts[a1].refs)


def test_roadmap_nesting_done_markers_and_order(tmp_path: Path) -> None:
    (tmp_path / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
    arts = {a.key.split("#")[1]: a for a in _read(tmp_path, first_add="false")}
    k = lambda i: f"plan:ROADMAP.md#{i}"  # noqa: E731
    assert "roadmap" not in arts  # the single H1 is the document title
    assert arts["phase-1"].attrs["parent"] is None
    assert arts["phase-1/setup-repo"].attrs["parent"] == k("phase-1")
    assert arts["phase-1/setup-repo/init-git"].attrs["parent"] == k("phase-1/setup-repo")
    assert arts["phase-1/write-docs"].attrs["parent"] == k("phase-1")
    order = [arts[i].attrs["order"] for i in ("phase-1/setup-repo", "phase-1/write-docs")]
    assert order == [0, 1]
    assert arts["phase-3/details"].attrs["parent"] == k("phase-3")
    done = {i: a.attrs["done_marker"] for i, a in arts.items()}
    assert done["phase-1/setup-repo/init-git"] and done["phase-1/setup-repo"]
    assert not done["phase-1"] and not done["phase-1/write-docs"] and not done["phase-2"]
    assert "not-an-item" not in str(list(arts))  # fenced code is ignored
    items = ("phase-3/details/item", "phase-3/details/item-2")
    assert [arts[i].attrs["done_marker"] for i in items] == [False, True]
    assert arts["phase-1/setup-repo/add-ci"].attrs["depends_on"] == (
        k("phase-1/setup-repo/init-git"),
    )
    assert arts["phase-2/ship"].attrs["depends_on"] == (k("phase-1/write-docs"),)
    assert arts["phase-2/ship"].raw_attrs["unresolved_depends_on"] == ("nothing",)
    assert arts["phase-2/ship"].title == "Ship"


def test_roadmap_files_option_and_missing_file(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("- [ ] a\n- [x] b\n", encoding="utf-8")
    arts = _read(tmp_path, files="plan.md", formats="roadmap", first_add="false")
    assert [(a.title, a.attrs["parent"], a.attrs["done_marker"]) for a in arts] == [
        ("a", None, False),
        ("b", None, True),
    ]
    with pytest.raises(ValueError, match="does not exist"):
        _read(tmp_path, files="nope.md", formats="roadmap")
    with pytest.raises(ValueError, match="unknown planning format"):
        _read(tmp_path, formats="zzz")


def test_roadmap_discovery_collapses_names_for_one_file(tmp_path: Path) -> None:
    """A case-insensitive filesystem makes both spellings one file; simulate with a hard link."""
    from beadlog_sources.planning_reader import RoadmapFormat

    (tmp_path / "ROADMAP.md").write_text("- [ ] a\n- [x] b\n", encoding="utf-8")
    os.link(tmp_path / "ROADMAP.md", tmp_path / "roadmap.md")
    assert RoadmapFormat.discover(tmp_path, {}) == ["ROADMAP.md"]
    arts = _read(tmp_path, formats="roadmap", first_add="false")
    assert sorted(a.title for a in arts) == ["a", "b"]


def test_roadmap_discovery_keeps_distinct_files(tmp_path: Path) -> None:
    from beadlog_sources.planning_reader import RoadmapFormat

    (tmp_path / "ROADMAP.md").write_text("- [ ] a\n", encoding="utf-8")
    (tmp_path / "roadmap.md").write_text("- [ ] b\n", encoding="utf-8")
    if (tmp_path / "ROADMAP.md").samefile(tmp_path / "roadmap.md"):
        pytest.skip("case-insensitive host filesystem")
    assert RoadmapFormat.discover(tmp_path, {}) == ["ROADMAP.md", "roadmap.md"]


def test_output_is_deterministic_and_valid_evidence(gsd: Path) -> None:
    (gsd / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
    one, two = _read(gsd), _read(gsd)
    assert one == two and len({a.key for a in one}) == len(one)
    header = PlanningReader.header_options({})
    text = dumps_evidence(build_corpus(one, []))
    assert text == dumps_evidence(build_corpus(two, [])) and header["token_mode"] == "generic"


def test_token_refs_and_token_mode() -> None:
    items = roadmap_items("R.md", "- [ ] Do bh-ab1c2 and hq-9z\n")
    reader = PlanningReader()
    from beadlog_sources.tokens import bead_token_pattern

    art = reader.artifact("roadmap", items[0], bead_token_pattern(["bh"]), {})
    assert [r.raw for r in art.refs] == ["bh-ab1c2"]
    assert PlanningReader.header_options({"namespaces": "bh"})["token_mode"] == "namespaces"
    assert slugify("  !! ") == "item"


def test_roadmap_item_token_refs(tmp_path: Path) -> None:
    (tmp_path / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
    art = next(
        a
        for a in _read(tmp_path, first_add="false", namespaces="bh")
        if a.title.startswith("Tokens")
    )
    assert [(r.kind, r.raw) for r in art.refs] == [("bead-token", "bh-zz9")]


def _keys(text: str) -> list[str]:
    return [i.key for i in roadmap_items("R.md", text)]


def test_roadmap_keys_survive_incidental_edits() -> None:
    base = "## Phase\n- [ ] Write docs\n- [ ] Ship\n"
    edited = "## Phase\n- [x]   Write docs, bh-ab1c2!! (depends: ship)\n- [ ] Ship\n"
    assert (
        _keys(base)
        == _keys(edited)
        == ["plan:R.md#phase", "plan:R.md#phase/write-docs", "plan:R.md#phase/ship"]
    )


def test_roadmap_same_text_under_different_headings_is_distinct_and_order_free() -> None:
    one = "## A\n- [ ] Task\n## B\n- [ ] Task\n"
    two = "## B\n- [ ] Task\n## A\n- [ ] Task\n"
    assert set(_keys(one)) == set(_keys(two))
    assert "plan:R.md#a/task" in _keys(one) and "plan:R.md#b/task" in _keys(one)
    dup = _keys("## A\n- [ ] Task\n- [ ] Task\n")
    assert dup[1:] == ["plan:R.md#a/task", "plan:R.md#a/task-2"]


def test_roadmap_single_h1_is_title_but_several_are_items() -> None:
    assert _keys("# Title\n## A\n") == ["plan:R.md#a"]
    assert [i.parent for i in roadmap_items("R.md", "# Title\n## A\n")] == [None]
    several = roadmap_items("R.md", "# One\n## A\n# Two\n")
    assert [(i.key, i.parent) for i in several] == [
        ("plan:R.md#one", None),
        ("plan:R.md#one/a", "plan:R.md#one"),
        ("plan:R.md#two", None),
    ]
