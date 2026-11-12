"""Golden extract outputs of the ``git-commits``, ``docs`` and ``planning`` strategies.

Each case builds a small, fully deterministic repository (fixed identities and dates, global and
system git config ignored, so commit shas are stable), runs one strategy with a fixed baseline and
``extracted_at``, and compares the result byte for byte with ``golden/strategies/<case>.golden``:
one canonical JSON line per proposed event (event ids included), then the report, or one line
naming the refused extraction. The goldens pin the strategies' observable outputs so the readers
can replace their parsing without changing a single event (bh-98jpq).

Paths of the throwaway directory are written as ``<tmp>``. A git failure is pinned by its error
class only: its text is git's own diagnostics (which argv was echoed, which stderr line came
last), not an extract output.

Set ``BEADLOG_SOURCES_UPDATE_GOLDENS=1`` to rewrite the goldens (only when an output change is
intended and reviewed).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from beadlog import BeadEvent, BeadRef, mint_event
from beadlog_sources import (
    ABSTAIN,
    ChoiceQuery,
    DeterministicJudge,
    DocsStrategy,
    ExtractRequest,
    GitCommitsStrategy,
    Judgment,
    PlanningStrategy,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "strategies"
UPDATE = os.environ.get("BEADLOG_SOURCES_UPDATE_GOLDENS") == "1"
GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000f1"
OTHER_GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000f2"
EXTRACTED = "2026-10-31T00:00:00Z"
BASE_AT = "2026-08-01T00:00:00Z"


# -- a deterministic repository ------------------------------------------------------------


def _env(when: str) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_AUTHOR_EMAIL": "ada@example.invalid",
        "GIT_COMMITTER_NAME": "Ada",
        "GIT_COMMITTER_EMAIL": "ada@example.invalid",
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


class Repo:
    """A throwaway repository whose every commit has a fixed, increasing date."""

    def __init__(self, root: Path, *, git: bool = True) -> None:
        self.root = root
        self.tick = 0
        root.mkdir(parents=True)
        if git:
            self.git("init", "-q", "-b", "main")

    def git(self, *args: str, when: str = "2026-09-01T00:00:00+00:00") -> str:
        done = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
            env=_env(when),
        )
        return done.stdout.strip()

    def write(self, path: str, text: str | bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text, encoding="utf-8")

    def commit(self, message: str, *, when: str | None = None, **files: str) -> str:
        for path, text in files.items():
            self.write(path.replace("__", "/"), text)
        self.tick += 1
        when = when or f"2026-09-{1 + self.tick // 24:02d}T{self.tick % 24:02d}:00:00+02:00"
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, when=when)
        return self.git("rev-parse", "HEAD")


def _create(
    bead: str,
    title: str | None = None,
    *,
    group: str = GROUP,
    type: str = "task",
    at: str = BASE_AT,
    **fields: Any,
) -> BeadEvent:
    return mint_event(
        "create",
        BeadRef(group, bead),
        {"type": type, "fields": {"title": title or f"Bead {bead}", **fields}},
        occurred_at=at,
        recorded_at=at,
        extracted_at=EXTRACTED,
        source=("bd", f"issues/{bead}#create"),
        provenance="observed",
    )


def _event(kind: str, bead: str, data: Mapping[str, Any], at: str = BASE_AT) -> BeadEvent:
    return mint_event(
        kind,
        BeadRef(GROUP, bead),
        data,
        occurred_at=at,
        recorded_at=at,
        extracted_at=EXTRACTED,
        source=("bd", f"issues/{bead}#{kind}"),
        provenance="observed",
    )


class ScriptedJudge:
    """Answers each doc from a script keyed by the first context line (the doc's title)."""

    engine = "scripted"
    engine_version = "1"
    model = "scripted-model"

    def __init__(self, answers: Mapping[str, tuple[str | None, float]]) -> None:
        self.answers = dict(answers)

    def choose(self, query: ChoiceQuery) -> Judgment:
        subject = query.context.split("\n", 1)[0]
        outcome, confidence = self.answers.get(subject, (None, 0.0))
        return Judgment(
            outcome,
            {outcome or ABSTAIN: 1.0},
            confidence,
            len(query.options),
            f"scripted verdict for {subject}",
            self.engine,
            self.engine_version,
            self.model,
        )

    def score(self, query: Any) -> Judgment:  # pragma: no cover - never asked
        raise AssertionError("docs never scores")


# -- rendering -----------------------------------------------------------------------------


def _line(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def render(
    strategy: Any,
    root: Path,
    baseline: Sequence[BeadEvent],
    tmp: Path,
    judge: Any = None,
    **options: str,
) -> str:
    request = ExtractRequest(
        root=root,
        group=GROUP,
        extracted_at=EXTRACTED,
        baseline=tuple(baseline),
        options=options,
        judge=judge,
    )
    try:
        events = strategy.extract(request)
    except Exception as exc:  # noqa: BLE001 - the refusal is the pinned output
        message: str | None = str(exc)
        if " failed " in str(exc):  # a git failure: its text is git's diagnostics, not output
            message = None
        lines = [_line({"error": type(exc).__name__, "message": message})]
    else:
        lines = [_line(event.to_json()) for event in events]
        lines.append(_line({"report": strategy.report()}))
    text = "\n".join(lines) + "\n"
    return text.replace(str(tmp), "<tmp>")


def check(name: str, text: str) -> None:
    path = GOLDEN_DIR / f"{name}.golden"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.is_file(), f"missing golden {path.name}: run with BEADLOG_SOURCES_UPDATE_GOLDENS=1"
    assert text == path.read_text(encoding="utf-8")


# -- git-commits ---------------------------------------------------------------------------

COMMIT_LIVE = (
    "bh-100",
    "bh-200",
    "bh-200.3",
    "bh-300",
    "bh-400",
    "bh-500",
    "bh-600",
    "bh-700",
    "bh-art",
    "hq-1a",
    "BH-UP9",
)


def commits_repo(tmp: Path) -> tuple[Repo, dict[str, str]]:
    repo = Repo(tmp / "commits")
    shas: dict[str, str] = {}
    shas["start"] = repo.commit("chore: start", README="hello\n")
    repo.git("tag", "start")
    shas["multi"] = repo.commit("feat: bh-100 and bh-200 together", a="1\n")
    shas["body"] = repo.commit("fix: subject names nothing\n\nThe body names bh-300.", b="1\n")
    shas["repeat"] = repo.commit("fix(bh-100): bh-100 again, BH-100 too", c="1\n")
    shas["noise"] = repo.commit("chore: bh-infra bh-version xbh-100 bh-100x_y wt/bh-art", d="1\n")
    shas["trim"] = repo.commit("feat: bh-400-style and bh-200.3.9 and bh-200.3", e="1\n")
    shas["hq"] = repo.commit("feat: HQ-1A in caps, hq-1a-extra", f="1\n")
    shas["trailers"] = repo.commit(
        "feat: trailers carry ids\n\nBody text.\n\nRefs: bh-500\nCo-authored-by: X <x@y>\n"
        "Fixes: bh-art\n  continued bh-700",
        g="1\n",
    )
    shas["key"] = repo.commit("fix: a trailer key\n\nbh-600: done", h="1\n")
    repo.git("checkout", "-q", "-b", "side")
    shas["side"] = repo.commit("feat: side work for bh-700", side_txt="1\n")
    repo.git("checkout", "-q", "main")
    repo.git("mv", "README", "README.md")
    shas["rename"] = repo.commit("docs: rename the readme for bh-art")
    repo.tick += 1
    repo.git(
        "merge",
        "-q",
        "--no-ff",
        "side",
        "-m",
        "Merge branch side (bh-500 bh-up9)",
        when="2026-09-02T09:00:00+02:00",
    )
    shas["merge"] = repo.git("rev-parse", "HEAD")
    shas["late"] = repo.commit(
        "feat: same second as the recorded link bh-400", when="2026-08-15T12:00:00+00:00"
    )
    return repo, shas


def commits_baseline(shas: Mapping[str, str]) -> list[BeadEvent]:
    out = [_create(bead) for bead in COMMIT_LIVE if bead not in ("bh-200", "bh-400", "bh-500")]
    # an existing linkage to append to, one fully recorded, one malformed (drift)
    out.append(_create("bh-200", **{"metadata.git.commits": json.dumps(["0" * 40])}))
    out.append(_create("bh-500", **{"metadata.git.commits": "not json"}))
    # a set decided at the same instant as a later commit: the proposal must step past it
    out.append(_create("bh-400"))
    out.append(
        _event(
            "set",
            "bh-400",
            {"field": "metadata.git.commits", "value": json.dumps([shas["trim"]])},
            at="2026-08-15T12:00:00Z",
        )
    )
    out.append(_create("bh-gone"))
    out.append(_event("delete", "bh-gone", {}))
    out.append(_create("bh-800", group=OTHER_GROUP))
    return out


@pytest.fixture
def commits(tmp_path: Path) -> tuple[Repo, dict[str, str]]:
    return commits_repo(tmp_path)


def test_git_commits_full_history(commits: Any, tmp_path: Path) -> None:
    repo, shas = commits
    out = render(GitCommitsStrategy(), repo.root, commits_baseline(shas), tmp_path)
    check("git-commits-full", out)


def test_git_commits_already_recorded_proposes_nothing(commits: Any, tmp_path: Path) -> None:
    repo, shas = commits
    baseline = [
        _create("bh-100", **{"metadata.git.commits": json.dumps([shas["multi"], shas["repeat"]])})
    ]
    check("git-commits-present", render(GitCommitsStrategy(), repo.root, baseline, tmp_path))


@pytest.mark.parametrize(
    ("name", "options"),
    [
        ("git-commits-rev-side", {"rev": "side"}),
        ("git-commits-since-tag", {"since": "start"}),
        ("git-commits-since-date", {"since": "2026-09-01T05:00:00+00:00", "rev": "HEAD~1"}),
        ("git-commits-since-digits", {"since": "20260901", "rev": "HEAD~1"}),
        ("git-commits-bad-rev", {"rev": "no-such-rev"}),
    ],
)
def test_git_commits_walk_options(
    commits: Any, tmp_path: Path, name: str, options: dict[str, str]
) -> None:
    repo, shas = commits
    out = render(GitCommitsStrategy(), repo.root, commits_baseline(shas), tmp_path, **options)
    check(name, out)


def test_git_commits_since_a_sha(commits: Any, tmp_path: Path) -> None:
    repo, shas = commits
    out = render(
        GitCommitsStrategy(), repo.root, commits_baseline(shas), tmp_path, since=shas["key"]
    )
    check("git-commits-since-sha", out)


def test_git_commits_empty_hive_is_refused(commits: Any, tmp_path: Path) -> None:
    repo, _ = commits
    baseline = [_create("bh-800", group=OTHER_GROUP)]
    check("git-commits-empty-hive", render(GitCommitsStrategy(), repo.root, baseline, tmp_path))


def test_git_commits_ids_without_a_namespace_are_refused(commits: Any, tmp_path: Path) -> None:
    repo, _ = commits
    baseline = [_create("plain"), _create("other")]
    check("git-commits-no-namespace", render(GitCommitsStrategy(), repo.root, baseline, tmp_path))


def test_git_commits_shallow_clone(commits: Any, tmp_path: Path) -> None:
    repo, shas = commits
    dest = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "2", f"file://{repo.root}", str(dest)],
        check=True,
        capture_output=True,
        env=_env("2026-09-01T00:00:00+00:00"),
    )
    baseline = commits_baseline(shas)
    check("git-commits-shallow", render(GitCommitsStrategy(), dest, baseline, tmp_path))
    out = render(GitCommitsStrategy(), dest, baseline, tmp_path, allow_shallow="true")
    check("git-commits-shallow-allowed", out)


# -- docs ----------------------------------------------------------------------------------

DOCS_LIVE = (
    _create("bh-example", "Example bead"),
    _create("bh-example.5", "Example child"),
    _create("bh-stamped", "Stamped bead", external_ref="docs/design/elsewhere.md"),
    _create("bh-linked", "Linked bead", external_ref="docs/decisions/0001-linked.md"),
    _create("bh-trailer", "Trailer bead"),
    _create("bh-upper", "Upper bead"),
    _create("bh-second", "Second bead"),
    _create("bh-renamed", "Renamed bead"),
    _create("bh-crlf", "Carriage bead"),
    _create("hq-alpha1", "Alpha headquarters bead"),
    _create("bh-epic1", "Storage engine epic", type="epic"),
    _create("bh-cache", "Cache layer for the storage engine"),
    _create("bh-search", "Search indexing pipeline"),
    _create("bh-gate1", "A gate", type="gate"),
)

MADR = """# Use a storage engine

## Status

Accepted

## Context

The storage engine decision. Beads: bh-example (a body line is not a back-ref line)
"""


def docs_repo(tmp: Path, *, git: bool = True) -> Repo:
    repo = Repo(tmp / "docs", git=git)
    files = {
        "docs/decisions/0001-linked.md": "# Linked decision\n\nAlready linked.\n",
        "docs/decisions/0002-last-wins.md": (
            "---\ntitle: Last wins\nBeads: bh-second, bh-example\n---\nBeads: bh-upper\n"
        ),
        "docs/decisions/0003-bead-dash.md": "- Bead: bh-example-foo\n# Dash form\n",
        "docs/decisions/0004-child.md": "---\nBeads: bh-example.5\n---\n# Child\n",
        "docs/decisions/0005-dangling.md": "---\nBeads: bh-missing9\n---\n# Dangling\n",
        "docs/decisions/0006-drifted.md": "---\nBeads: bh-stamped\n---\n# Drifted\n",
        "docs/decisions/0007-upper.md": "---\nBEADS: BH-UPPER\n---\n# Upper\n",
        "docs/decisions/0008-empty-line.md": "---\nBeads: none here\nBeads: bh-example\n---\n",
        "docs/decisions/0009-hq.md": "---\nBeads: hq-alpha1\n---\n# Alpha in HQ\n",
        "docs/decisions/0010-repeat.md": (
            "---\nBeads: bh-example, bh-second, bh-example\nBeads: bh-upper\n---\n# Repeat\n"
        ),
        "docs/design/storage-engine.md": MADR,
        "docs/design/cache-layer.md": "# Cache layer for the storage engine\n\nNotes.\n",
        "docs/design/search.md": "---\ntitle: Search indexing pipeline\n---\nBody.\n",
        "docs/design/fenced.md": "```\n# not a heading\n```\n\n# Fenced title\n",
        "docs/design/indented.md": "   # Indented heading\n\n# Plain heading\n",
        "docs/design/setext.md": "Setext title\n============\n\n## Second\n",
        "docs/design/title-key.md": "---\nTitle: Capital key\n---\n# Heading title\n",
        "docs/design/empty-title.md": '---\ntitle: ""\ntitle: Second title\n---\n# H\n',
        "docs/design/no-title.md": "Just prose, no heading.\n",
        "docs/design/sub/nested.md": "# Nested design\n",
        "docs/design/notes.txt": "Beads: bh-example\n",
        "docs/other/outside.md": "# Outside the default dirs\n",
    }
    if not git:
        for path, text in files.items():
            repo.write(path, text)
        return repo
    first = dict(list(files.items())[:6])
    for path, text in first.items():
        repo.write(path, text)
    repo.commit("docs: first decisions")
    for path, text in list(files.items())[6:]:
        repo.write(path, text)
    repo.commit("docs: design notes for wt/bh-infra")
    repo.write("docs/design/trailer.md", "# Trailer\n")
    repo.commit("docs(bh-trailer): add the trailer doc")
    repo.write("docs/design/scope.md", "# Scoped\n")
    repo.commit("docs(bh-infra): a scope naming no bead, then bh-trailer")
    repo.write("docs/drafts/renamed.md", "# Renamed\n")
    repo.commit("docs: draft for bh-renamed")
    repo.git("mv", "docs/drafts/renamed.md", "docs/design/renamed.md")
    repo.commit("docs: promote the draft")
    repo.write(
        "docs/design/crlf.md", b"---\r\nBeads: bh-crlf\r\n---\r\n# Carriage\r\n\r\nText.\r\n"
    )
    repo.write("docs/design/crlf-judged.md", b"# Carriage judged\r\n\r\nBody text.\r\n")
    repo.commit("docs: crlf docs")
    return repo


@pytest.fixture
def docs(tmp_path: Path) -> Repo:
    return docs_repo(tmp_path)


def test_docs_judge_off(docs: Repo, tmp_path: Path) -> None:
    out = render(DocsStrategy(), docs.root, DOCS_LIVE, tmp_path, id_prefix="bh")
    check("docs-judge-off", out)


def test_docs_deterministic_judge(docs: Repo, tmp_path: Path) -> None:
    out = render(
        DocsStrategy(), docs.root, DOCS_LIVE, tmp_path, DeterministicJudge(), id_prefix="bh"
    )
    check("docs-deterministic-judge", out)


SCRIPT = {
    "Use a storage engine": ("noise:1", 0.8),
    "Cache layer for the storage engine": ("present:1", 0.9),
    "Search indexing pipeline": ("present:1", 0.7),
    "Fenced title": ("new", 0.6),
    "not a heading": ("new", 0.6),
    "Indented heading": ("new", 0.5),
    "Plain heading": ("new", 0.5),
    "Setext title": ("new", 0.4),
    "Second": ("new", 0.4),
    "Capital key": ("new", 0.3),
    "Heading title": ("new", 0.3),
    "Second title": ("new", 0.2),
    "H": ("new", 0.2),
    "no-title": ("new", 0.1),
    "Carriage judged": ("new", 0.1),
    "Scoped": ("abstain", 0.0),
}


def test_docs_scripted_judge(docs: Repo, tmp_path: Path) -> None:
    out = render(
        DocsStrategy(),
        docs.root,
        DOCS_LIVE,
        tmp_path,
        ScriptedJudge(SCRIPT),
        id_prefix="bh",
        labels="org:x,repo:y",
        new_type="feature",
        top_k="2",
    )
    check("docs-scripted-judge", out)


def test_docs_explicit_dirs_and_file(docs: Repo, tmp_path: Path) -> None:
    out = render(
        DocsStrategy(),
        docs.root,
        DOCS_LIVE,
        tmp_path,
        ScriptedJudge(SCRIPT),
        id_prefix="bh",
        docs="docs/design,docs/other/outside.md,docs/missing",
    )
    check("docs-explicit-dirs", out)


def test_docs_without_git(tmp_path: Path) -> None:
    repo = docs_repo(tmp_path, git=False)
    judge = ScriptedJudge(SCRIPT)
    out = render(DocsStrategy(), repo.root, DOCS_LIVE, tmp_path, judge, id_prefix="t")
    check("docs-no-git", out)


def test_docs_empty_hive(docs: Repo, tmp_path: Path) -> None:
    check("docs-empty-hive-judge-off", render(DocsStrategy(), docs.root, (), tmp_path))
    out = render(DocsStrategy(), docs.root, (), tmp_path, ScriptedJudge(SCRIPT), id_prefix="bh")
    check("docs-empty-hive-judged", out)
    out = render(DocsStrategy(), docs.root, (), tmp_path, ScriptedJudge(SCRIPT))
    check("docs-empty-hive-no-prefix", out)


def test_docs_new_without_prefix_is_refused(docs: Repo, tmp_path: Path) -> None:
    out = render(DocsStrategy(), docs.root, DOCS_LIVE, tmp_path, ScriptedJudge(SCRIPT))
    check("docs-no-prefix", out)


# -- planning ------------------------------------------------------------------------------

TREE = ".planning/phases"


def planning_repo(tmp: Path, *, git: bool = True) -> Repo:
    repo = Repo(tmp / "planning", git=git)
    plans = {
        f"{TREE}/01-foundation/01-01-PLAN.md": (
            "---\nphase: 01\ndepends_on: []\n---\n# Foundation: set up the repo\n"
        ),
        f"{TREE}/01-foundation/01-02-PLAN.md": (
            '---\nphase: 01\ndepends_on:\n  - 01-01\n  - "01-01-PLAN.md"\nnext: 1\n---\n'
            "# Foundation: wire CI\n"
        ),
        f"{TREE}/02-features/02-01-PLAN.md": (
            '---\ntitle: "Features: first slice"\ndepends_on: ["01-02"]\n---\n# Ignored\n'
        ),
        f"{TREE}/02-features/02-02-PLAN.md": (
            "---\ndepends_on: [02-01, 99-99, 02-02, 03-01]\n---\n```\n# fenced\n```\n# Real\n"
        ),
        f"{TREE}/02-features/03-01-PLAN.md": "no frontmatter, no heading\n",
        f"{TREE}/04_under_score/03-01-PLAN.md": "---\ndepends_on: [01-01]\n---\n# Dup key\n",
        f"{TREE}/03-empty/.gitkeep": "",
        f"{TREE}/notes/README.md": "# not a phase name\n",
    }
    for path, text in plans.items():
        repo.write(path, text)
    if not git:
        repo.write(f"{TREE}/01-foundation/01-01-SUMMARY.md", "# done\n")
        return repo
    repo.commit("docs(planning): add plans", when="2026-09-01T10:00:00+00:00")
    repo.write(f"{TREE}/01-foundation/01-01-SUMMARY.md", "# done\n")
    repo.write(f"{TREE}/01-foundation/01-02-SUMMARY.md", "# done\n")
    repo.write(f"{TREE}/02-features/02-01-SUMMARY.md", "# done\n")
    repo.commit("docs(planning): summaries", when="2026-09-05T12:30:00+00:00")
    repo.write(".planning/staging/05-01-PLAN.md", "---\ndepends_on: [01-01]\n---\n# Moved\n")
    repo.commit("docs(planning): stage a plan", when="2026-09-06T08:00:00+00:00")
    (repo.root / TREE / "05-moved").mkdir()
    repo.git("mv", ".planning/staging/05-01-PLAN.md", f"{TREE}/05-moved/05-01-PLAN.md")
    repo.commit("docs(planning): move it", when="2026-09-07T08:00:00+00:00")
    return repo


@pytest.fixture
def planning(tmp_path: Path) -> Repo:
    return planning_repo(tmp_path)


SEED = (_create("ag-infra-1", "Seed bead"),)


def test_planning_tree(planning: Repo, tmp_path: Path) -> None:
    out = render(PlanningStrategy(), planning.root, SEED, tmp_path, id_prefix="ag-infra")
    check("planning-tree", out)


def test_planning_options(planning: Repo, tmp_path: Path) -> None:
    out = render(
        PlanningStrategy(),
        planning.root,
        SEED,
        tmp_path,
        id_prefix="pl",
        labels="org:x,origin:backfill",
        issue_type="feature",
    )
    check("planning-options", out)


def test_planning_against_existing_beads(planning: Repo, tmp_path: Path) -> None:
    baseline = [
        *SEED,
        _create("pl-epic", "Hand epic", type="epic", external_ref=f"{TREE}/01-foundation"),
        _create("pl-a1", "Hand plan", external_ref=f"{TREE}/01-foundation/01-01-PLAN.md"),
        _create("pl-a2", "Closed plan", external_ref=f"{TREE}/01-foundation/01-02-PLAN.md"),
        _event("phase", "pl-a2", {"phase": "closed", "reason": "done"}),
    ]
    out = render(PlanningStrategy(), planning.root, baseline, tmp_path, id_prefix="pl")
    check("planning-existing", out)


def test_planning_without_prefix_is_refused(planning: Repo, tmp_path: Path) -> None:
    check("planning-no-prefix", render(PlanningStrategy(), planning.root, SEED, tmp_path))


def test_planning_empty_hive(planning: Repo, tmp_path: Path) -> None:
    out = render(PlanningStrategy(), planning.root, (), tmp_path, id_prefix="pl")
    check("planning-empty-hive", out)


def test_planning_untracked_tree(tmp_path: Path) -> None:
    repo = planning_repo(tmp_path, git=False)
    out = render(PlanningStrategy(), repo.root, SEED, tmp_path, id_prefix="pl")
    check("planning-untracked", out)


def test_planning_other_tree_and_missing_tree(planning: Repo, tmp_path: Path) -> None:
    out = render(PlanningStrategy(), planning.root, SEED, tmp_path, id_prefix="pl", planning=".x")
    check("planning-missing-tree", out)
