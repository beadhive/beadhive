"""bh-7jm7v.3 — the structural guard that a public publish path cannot reach the aggregate.

`src/beadhive/publish_export.py` is the one sanctioned public-snapshot entry point. This file
is what makes that claim load-bearing rather than decorative: it walks the static import graph
of this package and fails if `publish_export` can reach any CROSS-HIVE module (`hub`,
`hub_bulk`, `hq`, `hq_restore`) by any static import at any depth — module level or deferred
inside a function body — or if it directly references the `-a`/`-r` fan-out helpers.

**Every guard below is paired with an anti-vacuity test that proves it FIRES.** A boundary test
that passes because it is checking nothing is worse than no test: it is dischargeable by doing
nothing and it reads as coverage. So each checker is a pure function over inputs, exercised
twice — once on the real code (must report zero violations) and once on a deliberately
widened input (must report a violation). The widened inputs are not toys where it matters:
`test_closure_guard_fires_on_deferred_import_injected_into_the_real_module` re-parses the
REAL `publish_export.py` source with `from . import hub` injected into the deepest point of
`export_public_snapshot`'s body, and
`test_walker_flags_the_real_hub_capable_modules_in_this_package` runs the same walker over
this package's genuinely Hub-capable modules (`cli`, `hq`, `storage_migrate`) and requires it
to find them, re-verifying every hop of the reported path against the source.

See `docs/design/publish-boundary-adr.md` for why the boundary is drawn where it is —
notably why `route`/`registry` are banned by DIRECT reference rather than transitively.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
from collections import deque
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from beadhive import config, publish_export

PKG_DIR = Path(publish_export.__file__).parent
PKG_NAME = PKG_DIR.name

# The cross-hive stores. Reaching ANY of these from the publish path means the path can
# address hives other than the one being published — some of which are private.
AGGREGATE_MODULES = frozenset({"hub", "hub_bulk", "hq", "hq_restore"})

# Banned by DIRECT reference only. `route`/`registry` arrive transitively through `bd.run`
# (as they do for every bd invocation in this package), so a transitive ban is not expressible
# without duplicating the bd seam; what IS expressible is that the publish module never names
# the fan-out entry points itself. See the ADR.
DIRECT_BANNED_MODULES = frozenset(AGGREGATE_MODULES | {"route", "registry"})
DIRECT_BANNED_ATTRS = frozenset({"passthrough", "fan_out", "targets"})

# A dynamic import would route around the static walk entirely. None of these appear in the
# guarded module, and that absence is what makes "static imports are the only way in" true.
DIRECT_BANNED_NAMES = frozenset({"importlib", "__import__", "exec", "eval", "compile"})

# The signature is PINNED, not pattern-matched: any added/renamed parameter fails here and
# forces whoever adds it to read the ADR first. `hive_root` is a path (the checkout you are
# standing in); a hive NAME would be a registry lookup, i.e. the `-r <hive>` shape.
PINNED_EXPORT_PARAMS = ("hive_root", "dest_dir")


# --------------------------------------------------------------------------------------
# checkers — pure functions, so each can be run against real AND widened input
# --------------------------------------------------------------------------------------


def _docstring_constants(tree: ast.AST) -> set[int]:
    """`id()`s of the Constant nodes that are docstrings, so prose describing the banned
    machinery (this repo documents heavily) is never mistaken for a reference to it."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                out.add(id(first.value))
    return out


def sibling_imports(tree: ast.AST) -> set[str]:
    """Every `beadhive.*` sibling module imported by this AST, at any nesting depth.

    `ast.walk` deliberately: a deferred `from . import hub` inside a function body is the
    likeliest way a boundary rots (this package uses lazy imports to break real cycles), and
    it must be caught exactly like a module-level one. All five spellings are handled —
    `from . import x`, `from .x import y`, `from beadhive import x`, `from beadhive.x import y`,
    `import beadhive.x` — and pinned by `test_walker_sees_every_import_spelling`.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:  # from . import x  /  from .x import y
                if node.module is None:
                    found.update(alias.name for alias in node.names)
                else:
                    found.add(node.module.split(".")[0])
            elif node.level == 0 and node.module == PKG_NAME:  # from beadhive import x
                found.update(alias.name for alias in node.names)
            elif node.level == 0 and (node.module or "").startswith(f"{PKG_NAME}."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):  # import beadhive.x
            for alias in node.names:
                if alias.name.startswith(f"{PKG_NAME}."):
                    found.add(alias.name.split(".")[1])
    return found


def module_imports(module: str, *, src: Path) -> set[str]:
    """`sibling_imports` for one module by name. A module with no file on disk is a LEAF, not
    an error: the injected-violation tests deliberately parse a one-file tree, and the walker
    must still report the banned name it saw rather than crashing or silently dropping it."""
    path = src / f"{module}.py"
    if not path.exists():
        path = src / module / "__init__.py"
        if not path.exists():
            return set()
    return sibling_imports(ast.parse(path.read_text(), filename=str(path)))


def import_closure(entry: str, *, src: Path) -> dict[str, tuple[str, ...]]:
    """Every sibling module transitively reachable from `entry`, mapped to a shortest import
    path (BFS) so a violation message names the actual chain, not just the endpoint."""
    seen: dict[str, tuple[str, ...]] = {}
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(entry, (entry,))])
    while queue:
        module, path = queue.popleft()
        for imported in sorted(module_imports(module, src=src)):
            if imported not in seen:
                seen[imported] = path + (imported,)
                queue.append((imported, path + (imported,)))
    return seen


def closure_violations(
    entry: str, forbidden: Iterable[str], *, src: Path
) -> dict[str, tuple[str, ...]]:
    """The forbidden modules reachable from `entry`, each with the chain that reaches it."""
    closure = import_closure(entry, src=src)
    return {mod: closure[mod] for mod in sorted(forbidden) if mod in closure}


def direct_reference_violations(source: str) -> list[str]:
    """Bans this module may only break by naming the machinery itself: a direct import of a
    cross-hive/routing module, a call to a fan-out entry point, or a dynamic-import escape."""
    tree = ast.parse(source)
    docstrings = _docstring_constants(tree)
    bad = [f"imports {PKG_NAME}.{m}" for m in sorted(sibling_imports(tree) & DIRECT_BANNED_MODULES)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in DIRECT_BANNED_ATTRS:
            bad.append(f"references .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in DIRECT_BANNED_NAMES | DIRECT_BANNED_ATTRS:
            bad.append(f"references {node.id}")
        elif isinstance(node, ast.Constant) and id(node) not in docstrings:
            if isinstance(node.value, str) and node.value in DIRECT_BANNED_NAMES:
                bad.append(f"names {node.value} in a string")
    return sorted(set(bad))


def argv_violations(argv: Sequence[str]) -> list[str]:
    """bh-7jm7v.1's negative flag list, plus the cross-hive routing flags, asserted against the
    CONSTRUCTED command line — that ADR section asks for exactly this rather than eyeballing
    one run's output."""
    banned = (*publish_export.FORBIDDEN_EXPORT_FLAGS, *publish_export.FORBIDDEN_ROUTING_FLAGS)
    return sorted({f"forbidden flag {flag}" for flag in banned if flag in argv})


def signature_violations(params: Sequence[str]) -> list[str]:
    """The pinned-signature check. Any drift from `PINNED_EXPORT_PARAMS` is a violation,
    including a *safe-looking* addition — `hive=None` is still a hive-selection parameter."""
    if tuple(params) == PINNED_EXPORT_PARAMS:
        return []
    return [f"signature drifted: {tuple(params)} != {PINNED_EXPORT_PARAMS}"]


# --------------------------------------------------------------------------------------
# the guards, against the real code
# --------------------------------------------------------------------------------------


def test_publish_export_cannot_reach_any_aggregate_module():
    """THE guard: no static import path of any depth from the publish entry point to the
    cross-hive stores."""
    violations = closure_violations("publish_export", AGGREGATE_MODULES, src=PKG_DIR)
    assert violations == {}, (
        "publish_export can now reach a cross-hive module — a public snapshot could span "
        f"private hives: { {k: ' -> '.join(v) for k, v in violations.items()} }. "
        "See docs/design/publish-boundary-adr.md before widening this."
    )


def test_publish_export_makes_no_direct_routing_reference():
    source = (PKG_DIR / "publish_export.py").read_text()
    assert direct_reference_violations(source) == []


def test_public_snapshot_argv_is_exactly_the_decided_invocation(tmp_path):
    argv = publish_export.public_snapshot_argv(tmp_path)
    assert argv == ["export", "-o", str(tmp_path / "issues.jsonl")]
    assert argv_violations(argv) == []


def test_export_public_snapshot_has_no_hive_selection_parameter():
    params = list(inspect.signature(publish_export.export_public_snapshot).parameters)
    assert signature_violations(params) == []


def test_export_public_snapshot_runs_the_decided_invocation(tmp_path, monkeypatch):
    """The one integration-shaped assertion: the sanctioned function really does emit
    bh-7jm7v.1's argv, scoped by `-C` to the hive it was handed and no other."""
    hive = tmp_path / "hive"
    (hive / ".beads").mkdir(parents=True)
    dest = tmp_path / "out"
    calls = []

    def fake_run(args, cwd, **kwargs):
        calls.append((list(args), Path(cwd), kwargs))
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(publish_export.bd, "run", fake_run)
    out = publish_export.export_public_snapshot(hive, dest)

    assert out == dest / "issues.jsonl"
    assert len(calls) == 1, "a public snapshot is ONE bd invocation against ONE hive"
    argv, cwd, kwargs = calls[0]
    assert argv == ["export", "-o", str(dest / "issues.jsonl")]
    assert argv_violations(argv) == []
    assert cwd == hive.resolve()
    assert kwargs == {}


def test_export_public_snapshot_raises_when_bd_fails(tmp_path, monkeypatch):
    hive = tmp_path / "hive"
    (hive / ".beads").mkdir(parents=True)
    monkeypatch.setattr(
        publish_export.bd,
        "run",
        lambda args, cwd, **kw: subprocess.CompletedProcess(args=args, returncode=2),
    )
    with pytest.raises(RuntimeError, match="exit 2"):
        publish_export.export_public_snapshot(hive, tmp_path / "out")


@pytest.mark.parametrize("store", ["home", "hub_dir", "hq_dir", "cache_dir"])
def test_export_public_snapshot_refuses_the_aggregate_stores(store, tmp_path, monkeypatch):
    """The runtime half of the boundary: `hive_root` is a path, and the one path that would
    still address the cross-hive view is bh's own machine-local aggregate area."""
    root = getattr(config, store)()
    (root / ".beads").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(publish_export.bd, "run", lambda *a, **kw: pytest.fail("bd was invoked"))
    with pytest.raises(publish_export.PublishScopeError, match="aggregate area"):
        publish_export.export_public_snapshot(root, tmp_path / "out")


def test_export_public_snapshot_refuses_a_non_hive_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_export.bd, "run", lambda *a, **kw: pytest.fail("bd was invoked"))
    with pytest.raises(publish_export.PublishScopeError, match="not a hive checkout"):
        publish_export.export_public_snapshot(tmp_path / "nothing-here", tmp_path / "out")


# --------------------------------------------------------------------------------------
# anti-vacuity — each guard above, proven to fire
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry,target",
    [
        ("cli", "hub"),  # `bh hub <bd cmd>` — the aggregated cross-hive DB
        ("cli", "hq"),
        ("cli", "hub_bulk"),
        ("hq", "hub"),
        ("storage_migrate", "hq"),
    ],
)
def test_walker_flags_the_real_hub_capable_modules_in_this_package(entry, target):
    """Positive control on REAL source: the same walker, pointed at modules that legitimately
    reach the aggregate, must find them — and every hop of the path it reports must be a real
    import, so the walker cannot pass this by inventing chains."""
    violations = closure_violations(entry, AGGREGATE_MODULES, src=PKG_DIR)
    assert target in violations, f"walker failed to see {entry} -> ... -> {target}"
    path = violations[target]
    assert path[0] == entry and path[-1] == target
    for src_mod, dst_mod in zip(path[:-1], path[1:], strict=True):
        assert dst_mod in module_imports(src_mod, src=PKG_DIR), (
            f"reported path {' -> '.join(path)} is not real: {src_mod} does not import {dst_mod}"
        )


def test_walker_sees_every_import_spelling(tmp_path):
    pkg = tmp_path / PKG_NAME
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "from . import a\n"
        "from .b import thing\n"
        f"from {PKG_NAME} import c\n"
        f"from {PKG_NAME}.d import thing\n"
        f"import {PKG_NAME}.e\n"
        "import os\n"
        "from pathlib import Path\n"
    )
    assert module_imports("m", src=pkg) == {"a", "b", "c", "d", "e"}


def test_walker_follows_deferred_imports_through_a_transitive_chain(tmp_path):
    """Two properties at once: the walk is transitive, and an import buried inside a nested
    function body (the shape a lazy cycle-breaking import takes in this package) is still seen."""
    pkg = tmp_path / PKG_NAME
    pkg.mkdir()
    (pkg / "entry.py").write_text("from . import middle\n")
    (pkg / "middle.py").write_text(
        "def outer():\n"
        "    def inner():\n"
        "        if True:\n"
        "            from . import hub\n"
        "            return hub\n"
        "    return inner\n"
    )
    (pkg / "hub.py").write_text("")
    assert closure_violations("entry", AGGREGATE_MODULES, src=pkg) == {
        "hub": ("entry", "middle", "hub")
    }


def _inject_into_function(source: str, func: str, statement: str) -> str:
    """Splice `statement` in as the last statement of `func`'s body — i.e. as deep inside the
    real function as a widening would plausibly land, not at the top of the file where it
    would be obvious."""
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func
    )
    anchor = fn.body[-1]
    lines = source.splitlines()
    lines.insert(anchor.lineno - 1, " " * anchor.col_offset + statement)
    return "\n".join(lines) + "\n"


def _widened_copy(tmp_path: Path, source: str) -> Path:
    """Write a mutated `publish_export.py` into a throwaway package dir and return that dir.
    Siblings are absent on purpose: they parse as leaves, so what this proves is precisely
    that the injected name is still reported."""
    pkg = tmp_path / PKG_NAME
    pkg.mkdir()
    (pkg / "publish_export.py").write_text(source)
    return pkg


@pytest.fixture
def real_source() -> str:
    return (PKG_DIR / "publish_export.py").read_text()


def test_closure_guard_fires_on_a_deferred_import_injected_into_the_real_module(
    real_source, tmp_path
):
    """The mutation that matters: the REAL module's source, with `from . import hub` buried at
    the bottom of `export_public_snapshot`. If this passes, the guard above proves nothing."""
    widened = _inject_into_function(real_source, "export_public_snapshot", "from . import hub")
    assert ast.parse(widened)  # still valid Python, so this is a plausible regression
    violations = closure_violations(
        "publish_export", AGGREGATE_MODULES, src=_widened_copy(tmp_path, widened)
    )
    assert violations == {"hub": ("publish_export", "hub")}


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("from . import hq", "hq"),
        ("from .hub import sync", "hub"),
        (f"from {PKG_NAME} import hub_bulk", "hub_bulk"),
        (f"from {PKG_NAME}.hq_restore import restore", "hq_restore"),
        (f"import {PKG_NAME}.hub", "hub"),
    ],
)
def test_closure_guard_fires_on_every_module_level_widening_spelling(
    real_source, tmp_path, statement, expected
):
    widened = f"{statement}\n{real_source}"
    violations = closure_violations(
        "publish_export", AGGREGATE_MODULES, src=_widened_copy(tmp_path, widened)
    )
    assert expected in violations


@pytest.mark.parametrize(
    "statement",
    [
        "route.fan_out(targets, runner)",  # the `-a`/`-r` fan-out itself
        "bd.passthrough('all', None, ['export'])",
        "importlib.import_module('beadhive.hub')",  # dynamic-import escape
    ],
)
def test_direct_reference_guard_fires_on_injected_fanout_and_dynamic_imports(
    real_source, statement
):
    widened = _inject_into_function(real_source, "export_public_snapshot", statement)
    assert direct_reference_violations(widened) != []


def test_direct_reference_guard_fires_on_an_injected_route_import(real_source):
    assert direct_reference_violations(f"from . import route\n{real_source}") != []


def test_direct_reference_guard_tolerates_prose_about_the_banned_machinery():
    """The counterpart to the check above: docstrings in this repo describe `hub.py`,
    `route.fan_out` and friends at length (the module under guard does), and describing the
    boundary must never be mistaken for crossing it — or the guard would push authors toward
    deleting the explanation."""
    assert direct_reference_violations('"""Never call route.fan_out or importlib here."""\n') == []


@pytest.mark.parametrize(
    "flag", ["--all", "--include-memories", "--include-infra", "-a", "-r", "--hive", "--global"]
)
def test_argv_guard_fires_on_each_forbidden_flag(flag):
    assert argv_violations(["export", "-o", "out/issues.jsonl", flag]) != []


@pytest.mark.parametrize(
    "params",
    [
        ("hive_root", "dest_dir", "hive"),  # the `-r <hive>` shape
        ("hive_root", "dest_dir", "all_hives"),
        ("hive_root", "dest_dir", "scope"),
        ("hive_root",),
    ],
)
def test_signature_guard_fires_on_any_drift(params):
    assert signature_violations(params) != []
