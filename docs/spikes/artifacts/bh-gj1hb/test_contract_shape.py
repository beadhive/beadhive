"""Static properties of the proposed contract (spike bh-gj1hb).

These are the checks the implementation molecule would turn into architecture tests:
the contract is standard-library-only, carries no GitHub vocabulary, and nothing under ``src/``
imports a spike artifact.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import sys
import warnings
from pathlib import Path

import tracker_contracts

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]

#: Words that would mean a GitHub concept leaked into the tracker-neutral contract.
GITHUB_VOCABULARY = {
    "number",
    "node_id",
    "html_url",
    "login",
    "pull_request",
    "repository",
    "owner",
    "repo",
    "github",
    "gh",
    "scopes",
    "oauth",
    "app_id",
    "installation",
    "link",
    "etag",
}


def _module_imports(path: Path) -> set[str]:
    with warnings.catch_warnings():  # a product file's escape warnings are not ours to report
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)  # the 3.11 spelling
        tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_contract_is_standard_library_only() -> None:
    imported = _module_imports(HERE / "tracker_contracts.py")
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_contract_has_no_github_field_or_parameter() -> None:
    offenders = []
    for name, obj in vars(tracker_contracts).items():
        if dataclasses.is_dataclass(obj):
            for item in dataclasses.fields(obj):
                if set(item.name.split("_")) & GITHUB_VOCABULARY or item.name in GITHUB_VOCABULARY:
                    offenders.append(f"{name}.{item.name}")
        if inspect.isclass(obj) and getattr(obj, "_is_protocol", False):
            for method_name, method in vars(obj).items():
                if callable(method) and not method_name.startswith("_"):
                    for param in inspect.signature(method).parameters:
                        if param in GITHUB_VOCABULARY:
                            offenders.append(f"{name}.{method_name}({param})")
    assert offenders == []


def test_writes_require_a_grant_and_reads_do_not() -> None:
    writers = (tracker_contracts.CommentWriter, tracker_contracts.LabelWriter)
    for protocol in writers:
        for method_name, method in vars(protocol).items():
            if callable(method) and not method_name.startswith("_"):
                param = inspect.signature(method).parameters["grant"]
                assert param.kind is inspect.Parameter.KEYWORD_ONLY
    for method_name, method in vars(tracker_contracts.TrackerReader).items():
        if callable(method) and not method_name.startswith("_"):
            assert "grant" not in inspect.signature(method).parameters


def test_nothing_under_src_imports_a_spike_artifact() -> None:
    artifact_modules = {p.stem for p in HERE.glob("*.py")}
    src = REPO_ROOT / "src"
    if not src.is_dir():  # running from an exported copy of the artifacts
        return
    for path in src.rglob("*.py"):
        imported = _module_imports(path)
        assert not imported & artifact_modules, f"{path} imports {imported & artifact_modules}"
