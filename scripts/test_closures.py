#!/usr/bin/env python3
"""Run and validate the advisory module/plugin test-closure registry."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "tests" / "closures.toml"
_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_KINDS = {"kernel", "module", "adapter", "plugin", "contract", "integration", "system"}
_SELECTOR_FIELDS = ("tests", "shared_contract_tests", "reverse_dependency_tests")
_SOURCE_ROOTS = (("src", "beadhive"), ("tests",), ("docs",))


@dataclass(frozen=True)
class Closure:
    id: str
    kind: str
    status: str
    owner_path: str
    source_paths: tuple[str, ...]
    pytest_args: tuple[str, ...]
    tests: tuple[str, ...]
    shared_contracts: tuple[str, ...]
    shared_contract_tests: tuple[str, ...]
    reverse_dependencies: tuple[str, ...]
    reverse_dependency_tests: tuple[str, ...]

    @property
    def selectors(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (*self.tests, *self.shared_contract_tests, *self.reverse_dependency_tests)
            )
        )


@dataclass(frozen=True)
class Registry:
    full_gate: str
    release_gate: str
    expected_modules: tuple[str, ...]
    closures: tuple[Closure, ...]

    def by_id(self) -> dict[str, Closure]:
        return {closure.id: closure for closure in self.closures}


def load_registry(path: Path = DEFAULT_REGISTRY) -> Registry:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    metadata = raw.get("registry", {})
    closures = tuple(
        Closure(
            id=str(item.get("id", "")),
            kind=str(item.get("kind", "")),
            status=str(item.get("status", "")),
            owner_path=str(item.get("owner_path", "")),
            source_paths=tuple(item.get("source_paths", ())),
            pytest_args=tuple(item.get("pytest_args", ())),
            tests=tuple(item.get("tests", ())),
            shared_contracts=tuple(item.get("shared_contracts", ())),
            shared_contract_tests=tuple(item.get("shared_contract_tests", ())),
            reverse_dependencies=tuple(item.get("reverse_dependencies", ())),
            reverse_dependency_tests=tuple(item.get("reverse_dependency_tests", ())),
        )
        for item in raw.get("closures", ())
    )
    if metadata.get("schema_version") != 1:
        raise ValueError("closure registry schema_version must be 1")
    return Registry(
        full_gate=str(metadata.get("full_gate", "")),
        release_gate=str(metadata.get("release_gate", "")),
        expected_modules=tuple(metadata.get("expected_modules", ())),
        closures=closures,
    )


def discover_plugin_sources(root: Path) -> set[str]:
    discovered: set[str] = set()
    for path in sorted((root / "src" / "beadhive").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            if any(isinstance(target, ast.Name) and target.id == "PLUGIN" for target in targets):
                discovered.add(path.relative_to(root).as_posix())
                break
    return discovered


def discover_modules(root: Path) -> set[str]:
    module_root = root / "src" / "beadhive" / "modules"
    if not module_root.is_dir():
        return set()
    return {
        path.name
        for path in module_root.iterdir()
        if path.is_dir() and not path.name.startswith((".", "__"))
    }


def _has_unsafe_text(value: str) -> bool:
    return "\\" in value or any(ord(character) < 32 for character in value)


def _pytest_selector_error(selector: object) -> str | None:
    if not isinstance(selector, str) or not selector:
        return "must be a non-empty string"
    if selector.startswith("-"):
        return "must not be option-like"
    if _has_unsafe_text(selector):
        return "contains a backslash or control character"
    path_text, *node_parts = selector.split("::")
    raw_parts = path_text.split("/")
    path = PurePosixPath(path_text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in raw_parts):
        return "must be a safe repository-relative path"
    if not raw_parts or raw_parts[0] != "tests":
        return "must be under tests/"
    if len(raw_parts) > 1 and not path_text.endswith(".py"):
        return "must select a Python test file or the tests root"
    if node_parts and (
        path_text == "tests"
        or any(
            not part or _has_unsafe_text(part) or "/" in part or part in {".", ".."}
            for part in node_parts
        )
    ):
        return "has an unsafe or empty node id"
    return None


def _selector_path(selector: str) -> str:
    return selector.split("::", 1)[0]


def _source_path_error(source_path: object) -> str | None:
    if not isinstance(source_path, str) or not source_path:
        return "must be a non-empty string"
    if source_path.startswith("-") or _has_unsafe_text(source_path):
        return "contains an option-like, backslash, or control value"
    raw_parts = source_path.split("/")
    path = PurePosixPath(source_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in raw_parts):
        return "must be a safe repository-relative path"
    if not any(tuple(raw_parts[: len(root)]) == root for root in _SOURCE_ROOTS):
        return "must be under src/beadhive/, tests/, or docs/"
    return None


def _pytest_args_errors(closure: Closure, root: Path) -> tuple[str, ...]:
    errors: list[str] = []
    arguments = closure.pytest_args
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not isinstance(argument, str):
            errors.append(f"has non-string pytest argument {argument!r}")
            index += 1
            continue
        if argument not in {"-m", "-n", "--deselect"}:
            errors.append(f"has unsupported pytest argument {argument!r}")
            index += 1
            continue
        if index + 1 >= len(arguments):
            errors.append(f"pytest argument {argument!r} requires a value")
            break
        value = arguments[index + 1]
        if (
            not isinstance(value, str)
            or not value
            or value.startswith("-")
            or _has_unsafe_text(value)
        ):
            errors.append(f"pytest argument {argument!r} has an unsafe value {value!r}")
        elif argument == "-n" and value != "auto" and not (value.isdigit() and int(value) > 0):
            errors.append(f"pytest argument '-n' has unsupported worker count {value!r}")
        elif argument == "-m" and not re.fullmatch(r"[A-Za-z0-9_ ()!&|.-]+", value):
            errors.append(f"pytest argument '-m' has unsafe expression {value!r}")
        elif argument == "--deselect":
            selector_error = _pytest_selector_error(value)
            if selector_error:
                errors.append(f"has invalid --deselect selector {value!r}: {selector_error}")
            elif not (root / _selector_path(value)).exists():
                errors.append(f"deselects missing test path {_selector_path(value)!r}")
        index += 2
    return tuple(errors)


def validate_registry(registry: Registry, root: Path = ROOT) -> tuple[str, ...]:
    errors: list[str] = []
    if registry.full_gate != "just check":
        errors.append("registry full_gate must remain the authoritative 'just check'")
    if registry.release_gate != "just check-all":
        errors.append("registry release_gate must remain 'just check-all'")
    ids = [closure.id for closure in registry.closures]
    if len(ids) != len(set(ids)):
        errors.append("closure ids must be unique")
    by_id = registry.by_id()
    for closure in registry.closures:
        label = f"closure {closure.id!r}"
        if not _ID.fullmatch(closure.id):
            errors.append(f"{label} has an invalid id")
        if closure.kind not in _KINDS:
            errors.append(f"{label} has invalid kind {closure.kind!r}")
        if closure.status not in {"present", "absent"}:
            errors.append(f"{label} has invalid status {closure.status!r}")
        if (
            not closure.owner_path
            or Path(closure.owner_path).is_absolute()
            or ".." in Path(closure.owner_path).parts
        ):
            errors.append(f"{label} owner_path must be a safe repository-relative path")
        if not closure.source_paths:
            errors.append(f"{label} must declare source_paths")
        for source_path in closure.source_paths:
            source_error = _source_path_error(source_path)
            if source_error:
                errors.append(f"{label} has invalid source path {source_path!r}: {source_error}")
        if closure.status == "present" and not closure.tests:
            errors.append(f"{label} is present but has no direct tests")
        if closure.status == "absent" and any(
            getattr(closure, field) for field in _SELECTOR_FIELDS
        ):
            errors.append(f"{label} is absent but declares test selectors")
        if closure.status == "absent" and closure.pytest_args:
            errors.append(f"{label} is absent but declares pytest_args")
        if closure.shared_contracts and not closure.shared_contract_tests:
            errors.append(f"{label} declares shared contracts but no shared-contract tests")
        if closure.reverse_dependencies and not closure.reverse_dependency_tests:
            errors.append(f"{label} declares reverse dependencies but no reverse-dependent tests")
        for reference in (*closure.shared_contracts, *closure.reverse_dependencies):
            if reference not in by_id:
                errors.append(f"{label} references unknown closure {reference!r}")
        for selector in closure.selectors:
            selector_error = _pytest_selector_error(selector)
            if selector_error:
                errors.append(f"{label} has invalid pytest selector {selector!r}: {selector_error}")
                continue
            path = _selector_path(selector)
            if not (root / path).exists():
                errors.append(f"{label} selects missing test path {path!r}")
        errors.extend(f"{label} {error}" for error in _pytest_args_errors(closure, root))

    expected = set(registry.expected_modules)
    module_rows = {
        closure.id.removeprefix("module."): closure
        for closure in registry.closures
        if closure.kind == "module"
    }
    missing_expected = sorted(expected - module_rows.keys())
    if missing_expected:
        errors.append(f"expected modules without explicit closure rows: {missing_expected}")
    discovered_modules = discover_modules(root)
    for name in sorted(discovered_modules):
        closure = module_rows.get(name)
        if closure is None:
            errors.append(f"registered module {name!r} has no declared test closure")
        elif closure.status != "present":
            errors.append(f"registered module {name!r} is incorrectly declared absent")
    for name, closure in sorted(module_rows.items()):
        exists = (root / closure.owner_path).is_dir()
        if closure.status == "present" and not exists:
            errors.append(
                f"module closure {name!r} is present but {closure.owner_path!r} is absent"
            )
        if closure.status == "absent" and exists:
            errors.append(f"module closure {name!r} is absent but {closure.owner_path!r} exists")

    discovered_plugins = discover_plugin_sources(root)
    plugin_rows = {
        closure.owner_path: closure for closure in registry.closures if closure.kind == "plugin"
    }
    for path in sorted(discovered_plugins - plugin_rows.keys()):
        errors.append(f"registered plugin {path!r} has no declared test closure")
    for path, closure in sorted(plugin_rows.items()):
        if closure.status != "present":
            errors.append(f"plugin closure {closure.id!r} must be present")
        if path not in discovered_plugins:
            errors.append(
                f"plugin closure {closure.id!r} owner {path!r} has no PLUGIN registration"
            )
    return tuple(errors)


def _require_valid(registry: Registry) -> None:
    errors = validate_registry(registry)
    if errors:
        raise SystemExit(
            "closure-registry-check: FAILED\n" + "\n".join(f"- {error}" for error in errors)
        )


def _closure(registry: Registry, closure_id: str) -> Closure:
    try:
        return registry.by_id()[closure_id]
    except KeyError as exc:
        choices = ", ".join(sorted(registry.by_id()))
        raise SystemExit(f"unknown closure {closure_id!r}; choose one of: {choices}") from exc


def _pytest_commands(closure: Closure, *, collect_only: bool) -> tuple[tuple[str, ...], ...]:
    prefix = [sys.executable, "-m", "pytest", "-qq" if collect_only else "-q"]
    if collect_only:
        prefix.append("--collect-only")
    supplemental = tuple(
        dict.fromkeys((*closure.shared_contract_tests, *closure.reverse_dependency_tests))
    )
    if closure.pytest_args:
        commands = [(*prefix, *closure.pytest_args, *closure.tests)]
        if supplemental:
            commands.append((*prefix, *supplemental))
        return tuple(commands)
    return ((*prefix, *closure.selectors),)


def _pytest(closure: Closure, *, collect_only: bool) -> int:
    if closure.status == "absent":
        print(f"test-closure: {closure.id} is explicitly absent; no tests collected")
        return 0
    print(f"test-closure: {closure.id}")
    print(f"  direct: {len(closure.tests)} selector(s)")
    print(f"  shared-contract: {len(closure.shared_contract_tests)} selector(s)")
    print(f"  reverse-dependent: {len(closure.reverse_dependency_tests)} selector(s)")
    commands = _pytest_commands(closure, collect_only=collect_only)
    for index, command in enumerate(commands, 1):
        if len(commands) > 1:
            print(f"  pytest invocation: {index}")
        returncode = subprocess.run(command, cwd=ROOT, check=False).returncode
        if returncode == 5:
            print(
                f"test-closure: {closure.id}: pytest collected/executed zero tests",
                file=sys.stderr,
            )
        if returncode:
            return returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "collect", "list", "run", "show"))
    parser.add_argument("closure", nargs="?")
    args = parser.parse_args(argv)
    if args.action in {"check", "list"} and args.closure:
        parser.error(f"{args.action} does not accept a closure id")
    if args.action in {"collect", "run", "show"} and not args.closure:
        parser.error(f"{args.action} requires a closure id")
    registry = load_registry()
    _require_valid(registry)
    if args.action == "check":
        present = sum(closure.status == "present" for closure in registry.closures)
        absent = len(registry.closures) - present
        print(f"closure-registry-check: OK ({present} present, {absent} absent)")
        return 0
    if args.action == "list":
        for closure in registry.closures:
            print(f"{closure.id}\t{closure.status}\t{closure.kind}")
        return 0
    assert args.closure is not None
    closure = _closure(registry, args.closure)
    if args.action == "show":
        print(f"{closure.id}: {closure.status} ({closure.kind})")
        print(f"sources: {', '.join(closure.source_paths)}")
        print(f"shared contracts: {', '.join(closure.shared_contracts) or '-'}")
        print(f"reverse dependencies: {', '.join(closure.reverse_dependencies) or '-'}")
        print(f"selectors: {len(closure.selectors)}")
        return 0
    return _pytest(closure, collect_only=args.action == "collect")


if __name__ == "__main__":
    raise SystemExit(main())
