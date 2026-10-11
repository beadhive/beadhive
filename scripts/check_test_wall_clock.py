"""Guard tests/ against new wall-clock waits (bh-zbn8a, epic bh-vcx4i).

A test never waits on real time to make time pass (docs/TEST_PROCESS_POLICY.md, "Time in
tests"). This stdlib-AST check flags, in `tests/**/*.py`:

- `sleep` — a call to `time.sleep` / `asyncio.sleep` (or a bare `sleep` imported from either)
  whose first argument is not the literal `0`; and
- `elapsed-assert` — an `assert` comparing a measured wall duration (a `time.monotonic()` /
  `time.time()` / `time.perf_counter()` difference, directly or through a local assigned from
  one) to a bound. Plain timestamps (`time.time()` as "now" for an expiry fixture) are not flagged.

A function (or class) decorated `@pytest.mark.perf` is exempt: such tests are deselected from
every gate lane and run only on a quiet host (`-m perf` or `BH_PERF_TESTS=1`).

Code inside string literals (e.g. `python -c "time.sleep(300)"` child programs) is not parsed.
A finding is accepted only when `tests/wall_clock_allowlist.toml` has an `[[allow]]` entry whose
`path` and `anchor` (the enclosing function qualname, or `<module>`) match and whose `kind`
matches, with a non-empty `reason`. Stale allowlist entries (no matching finding) also fail, so
the list shrinks as sleeps are removed.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = Path("tests") / "wall_clock_allowlist.toml"
KINDS = ("sleep", "elapsed-assert")
_SLEEP_MODULES = {"time", "asyncio", "real_time", "_time"}
_CLOCK_CALLS = {"monotonic", "time", "perf_counter", "monotonic_ns", "time_ns"}
_CLOCK_MODULES = {"time", "real_time", "_time"}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    anchor: str
    kind: str

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: un-allowlisted wall-clock {self.kind} in {self.anchor} "
            "(use FakeClock / a readiness handshake, or add a reasoned entry to "
            f"{ALLOWLIST.as_posix()})"
        )


def _is_clock_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _CLOCK_CALLS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _CLOCK_MODULES
    )


def _has_clock(node: ast.AST) -> bool:
    return any(_is_clock_call(n) for n in ast.walk(node))


def _measures_duration(node: ast.AST, tainted: set[str]) -> bool:
    """True if `node` holds a clock difference (`monotonic() - start`) or a local holding one."""
    for n in ast.walk(node):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub) and _has_clock(n):
            return True
        if isinstance(n, ast.Name) and n.id in tainted:
            return True
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str, sleep_names: set[str]) -> None:
        self.rel = rel
        self.sleep_names = sleep_names
        self.scope: list[str] = []
        self.taint: list[set[str]] = [set()]
        self.perf = 0
        self._found: list[Finding] = []

    @property
    def findings(self) -> list[Finding]:
        return self._found

    def _add(self, finding: Finding) -> None:
        # `@pytest.mark.perf` tests are deselected from every gate lane (harness/perf_marker.py).
        if not self.perf:
            self._found.append(finding)

    def _anchor(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _scoped(self, node: ast.AST, name: str, *, fn: bool) -> None:
        perf = any(
            ast.unparse(d).endswith("mark.perf") for d in getattr(node, "decorator_list", ())
        )
        self.scope.append(name)
        self.perf += perf
        if fn:
            self.taint.append(set())
        self.generic_visit(node)
        if fn:
            self.taint.pop()
        self.perf -= perf
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name, fn=False)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name, fn=True)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:
        if _measures_duration(node.value, self.taint[-1]):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.taint[-1].add(target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_sleep = (
            isinstance(func, ast.Attribute)
            and func.attr == "sleep"
            and isinstance(func.value, ast.Name)
            and func.value.id in _SLEEP_MODULES
        ) or (isinstance(func, ast.Name) and func.id in self.sleep_names)
        if is_sleep:
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Constant) and first.value == 0):
                self._add(Finding(self.rel, node.lineno, self._anchor(), "sleep"))
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Compare) and _measures_duration(sub, self.taint[-1]):
                self._add(Finding(self.rel, node.lineno, self._anchor(), "elapsed-assert"))
                break
        self.generic_visit(node)


def _bare_sleep_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"time", "asyncio"}:
            names.update(a.asname or a.name for a in node.names if a.name == "sleep")
    return names


def scan(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted((root / "tests").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _Visitor(rel, _bare_sleep_names(tree))
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return sorted(set(findings))


def load_allowlist(root: Path) -> tuple[set[tuple[str, str, str]], list[str]]:
    data = tomllib.loads((root / ALLOWLIST).read_text(encoding="utf-8"))
    keys: set[tuple[str, str, str]] = set()
    errors: list[str] = []
    for index, entry in enumerate(data.get("allow", [])):
        where = f"{ALLOWLIST.as_posix()} entry {index + 1}"
        if entry.get("kind") not in KINDS:
            errors.append(f"{where}: kind must be one of {KINDS}")
        for field in ("path", "anchor", "reason"):
            if not str(entry.get(field, "")).strip():
                errors.append(f"{where}: missing {field}")
        keys.add((entry.get("path", ""), entry.get("anchor", ""), entry.get("kind", "")))
    return keys, errors


def check(root: Path = ROOT) -> list[str]:
    allowed, errors = load_allowlist(root)
    findings = scan(root)
    seen = {(f.path, f.anchor, f.kind) for f in findings}
    errors.extend(f.render() for f in findings if (f.path, f.anchor, f.kind) not in allowed)
    errors.extend(
        f"{ALLOWLIST.as_posix()}: stale entry {key} matches no finding; remove it"
        for key in sorted(allowed - seen)
    )
    return errors


def main() -> int:
    errors = check()
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
