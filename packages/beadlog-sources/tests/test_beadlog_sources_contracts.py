"""beadlog-sources: contract shapes and the package boundary (backfill platform ADR §2)."""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from importlib import resources
from pathlib import Path

import beadlog
import beadlog_sources
from beadlog_sources import (
    ABSTAIN,
    ENTRY_POINT_GROUP,
    ChoiceQuery,
    ExtractRequest,
    Judgment,
    JudgmentPort,
    ScoreQuery,
    SourceStrategy,
)

SOURCE = Path(beadlog_sources.__file__).parent
STDLIB = set(sys.stdlib_module_names) | {"__future__"}
# The one third-party import: httpx, the `[haiku]` extra, only in the reference judge command.
OPTIONAL = {"haiku.py": {"httpx"}, "doc_formats.py": {"markdown_it"}}
GROUP = "urn:uuid:00000000-0000-4000-8000-000000000003"


class _OneCommit:
    name = "one-commit"
    version = "0"

    def extract(self, request: ExtractRequest) -> Iterable[beadlog.BeadEvent]:
        yield beadlog.mint_event(
            "create",
            beadlog.BeadRef(request.group, "bh-1"),
            {"type": "task", "fields": {"title": "from a commit"}},
            occurred_at="2026-01-01T00:00:00Z",
            recorded_at=None,
            extracted_at=request.extracted_at,
            source=("git", "git:commit/abc"),
            provenance="derived",
        )


class _Abstainer:
    def choose(self, query: ChoiceQuery) -> Judgment:
        return Judgment(None, {}, 0.0, len(query.options), "no signal", "fake", "0")

    def score(self, query: ScoreQuery) -> Judgment:
        return Judgment(None, {}, 0.0, len(query.criteria), "no signal", "fake", "0")


def test_strategies_and_judges_conform_structurally() -> None:
    strategy = _OneCommit()
    assert isinstance(strategy, SourceStrategy)
    first = ExtractRequest(Path("."), GROUP, "2026-10-30T00:00:00Z")
    again = ExtractRequest(Path("."), GROUP, "2026-10-31T00:00:00Z", options={"depth": 1})
    (one,), (two,) = list(strategy.extract(first)), list(strategy.extract(again))
    assert one.id == two.id  # re-extraction mints the same id

    judge = _Abstainer()
    assert isinstance(judge, JudgmentPort)
    verdict = judge.choose(ChoiceQuery("which bead?", {"bh-1": "first", ABSTAIN: "none"}))
    assert verdict.outcome is None and verdict.option_count == 2
    assert judge.score(ScoreQuery("how complex?", ("SIMPLE", "COMPLEX"))).option_count == 2


def test_contract_constants() -> None:
    assert ENTRY_POINT_GROUP == "beadlog.sources"
    assert ABSTAIN == "abstain"
    assert ExtractRequest(Path("."), GROUP, "2026-10-30T00:00:00Z").baseline == ()


def test_public_surface_is_declared_and_importable() -> None:
    for module in (beadlog_sources, beadlog_sources.contracts):
        assert module.__all__
        for name in module.__all__:
            assert hasattr(module, name), f"{module.__name__} lacks {name}"


def test_imports_only_stdlib_and_beadlogs_public_surface() -> None:
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
                allowed = STDLIB | OPTIONAL.get(path.name, set())
                assert roots <= allowed, f"{path.name}: import {sorted(roots - allowed)}"
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".", 1)[0]
                if root == "beadlog":
                    assert node.module == "beadlog", f"{path.name}: {node.module} is not public"
                    names = {alias.name for alias in node.names}
                    assert names <= set(beadlog.__all__), f"{path.name}: {sorted(names)}"
                else:
                    assert root in STDLIB | {"beadlog_sources"}, f"{path.name}: {node.module}"


def test_httpx_is_imported_lazily_inside_a_function() -> None:
    tree = ast.parse((SOURCE / "haiku.py").read_text(encoding="utf-8"))
    top_level = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    assert "httpx" not in top_level
    probe = (
        "import sys, beadlog_sources, beadlog_sources.cli, beadlog_sources.haiku; "
        "sys.exit(1 if 'httpx' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-I", "-c", probe], check=False).returncode == 0


def test_markdown_it_is_imported_lazily() -> None:
    probe = (
        "import sys, beadlog_sources, beadlog_sources.cli, beadlog_sources.doc_formats; "
        "sys.exit(1 if 'markdown_it' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-I", "-c", probe], check=False).returncode == 0


def test_declares_only_beadlog_and_is_a_library() -> None:
    pyproject = tomllib.loads((SOURCE.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == ["beadlog"]
    assert pyproject["project"]["optional-dependencies"] == {
        "haiku": ["httpx>=0.28,<1"],
        "md": ["markdown-it-py>=3,<5"],
    }
    assert pyproject["project"]["scripts"] == {
        "beadlog": "beadlog_sources.cli:main",
        "bh-judge-haiku": "beadlog_sources.haiku:main",
    }
    assert "Private :: Do Not Upload" in pyproject["project"]["classifiers"]
    assert not (SOURCE / "plugin.json").exists()
    assert (resources.files(beadlog_sources) / "py.typed").is_file()


def test_tool_versions_match_the_distribution_version() -> None:
    from beadlog_sources import cli, haiku

    pyproject = tomllib.loads((SOURCE.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    assert cli.VERSION == haiku.ENGINE_VERSION == version
    assert cli.GENERATOR.name == "beadlog-sources"
