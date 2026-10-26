"""P1 and G4: the import rules of the GitHub tracker adapter plugin (tracker-adapter-plugin-adr.md).

* **P1** ``githubkit`` is imported only lazily and only under ``adapter/``: never outside it, and
  inside it only within a function body (never while the module is being imported). It costs about
  250 ms to import and is an optional dependency, so ``import beadhive`` and this package's own
  modules must import cleanly without it.
* **G4** the package imports no ``beadhive.kernel.*`` and, from the plugin layer
  (``beadhive_plugins``), only ``beadhive_plugins.credentials``. Root builds the
  ``ProviderBinding``, so nothing here needs the plugin kernel.

Both rules are checked structurally on the package source (AST) and, for P1, behaviourally in a
fresh interpreter whose importer refuses ``githubkit``. Every check has a negative control: the
same checker run on a synthetic tree that breaks the rule must report it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src" / "beadhive_tracker_github"

#: The optional third-party transport and its generated model packages. ``hishel`` is githubkit's
#: transitive cache; it is never imported directly.
_TRANSPORT_ROOTS = frozenset({"githubkit", "githubkit_schemas", "hishel"})
_DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__"})


@dataclass(frozen=True)
class ImportSite:
    path: Path
    line: int
    module: str  # dotted module as written (absolute imports only)
    names: tuple[str, ...]  # imported names for ``from m import a, b``; () for ``import m``
    runtime_at_import: bool  # executed while the module is imported (not in a function body)
    dynamic: bool = False


class _Collector(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sites: list[ImportSite] = []
        self._function_depth = 0
        self._type_checking_depth = 0

    def _add(self, node: ast.AST, module: str, names: tuple[str, ...], dynamic: bool = False):
        self.sites.append(
            ImportSite(
                self.path,
                node.lineno,  # type: ignore[attr-defined]
                module,
                names,
                self._function_depth == 0 and self._type_checking_depth == 0,
                dynamic,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(node, alias.name, ())

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self._add(node, node.module, tuple(alias.name for alias in node.names))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in _DYNAMIC_IMPORTERS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self._add(node, first.value, (), dynamic=True)
        self.generic_visit(node)

    def _function(self, node: ast.AST) -> None:
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _function

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        guarded = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if guarded:  # never executed at run time; a typing-only import is not a runtime import
            self._type_checking_depth += 1
            for child in node.body:
                self.visit(child)
            self._type_checking_depth -= 1
            for child in node.orelse:
                self.visit(child)
        else:
            self.generic_visit(node)


def import_sites(src: Path) -> Iterator[ImportSite]:
    for path in sorted(src.rglob("*.py")):
        collector = _Collector(path)
        collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        yield from collector.sites


def _root(module: str) -> str:
    return module.split(".", maxsplit=1)[0]


def transport_violations(src: Path) -> list[str]:
    """P1: a transport import outside ``adapter/``, or one that runs at import time."""
    problems: list[str] = []
    for site in import_sites(src):
        if _root(site.module) not in _TRANSPORT_ROOTS:
            continue
        where = f"{site.path.relative_to(src)}:{site.line}"
        under_adapter = site.path.relative_to(src).parts[0] == "adapter"
        if not under_adapter:
            problems.append(f"{where}: {site.module} imported outside adapter/")
        elif site.runtime_at_import:
            problems.append(f"{where}: {site.module} imported at module level, not lazily")
    return problems


def plugin_layer_violations(src: Path) -> list[str]:
    """G4: ``beadhive.kernel.*``, or ``beadhive_plugins`` beyond ``.credentials``."""
    problems: list[str] = []
    for site in import_sites(src):
        where = f"{site.path.relative_to(src)}:{site.line}"
        parts = site.module.split(".")
        if parts[0] == "beadhive" and (
            (len(parts) > 1 and parts[1] == "kernel")
            or (len(parts) == 1 and "kernel" in site.names)
        ):
            problems.append(f"{where}: imports the plugin kernel ({site.module})")
        if parts[0] == "beadhive_plugins":
            allowed = (len(parts) == 2 and parts[1] == "credentials") or (
                len(parts) == 1 and site.names == ("credentials",)
            )
            if not allowed:
                shown = site.module + (f" ({', '.join(site.names)})" if site.names else "")
                problems.append(f"{where}: imports {shown}, only beadhive_plugins.credentials")
    return problems


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body), encoding="utf-8")
    return root


# ---- P1: structural -------------------------------------------------------------------------


def test_p1_the_package_imports_githubkit_only_lazily_and_only_under_adapter():
    assert any(PACKAGE_SRC.rglob("*.py"))
    assert transport_violations(PACKAGE_SRC) == []


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"factory.py": "import githubkit\n"}, "outside adapter/"),
        ({"factory.py": "from githubkit import GitHub\n"}, "outside adapter/"),
        ({"factory.py": "def build():\n    import githubkit\n"}, "outside adapter/"),
        (
            {"factory.py": "from importlib import import_module\nimport_module('githubkit')\n"},
            "outside adapter/",
        ),
        ({"adapter/client.py": "import githubkit\n"}, "not lazily"),
        ({"adapter/client.py": "from githubkit.exception import RequestFailed\n"}, "not lazily"),
        ({"adapter/client.py": "import githubkit_schemas\n"}, "not lazily"),
        ({"adapter/client.py": "import hishel\n"}, "not lazily"),
        (
            {"adapter/client.py": "class C:\n    import githubkit\n"},
            "not lazily",
        ),
        (
            {"adapter/client.py": "try:\n    import githubkit\nexcept ImportError:\n    pass\n"},
            "not lazily",
        ),
    ],
)
def test_p1_negative_control_a_breach_is_reported(tmp_path, files, expected):
    problems = transport_violations(_write(tmp_path, files))
    assert any(expected in problem for problem in problems), problems


def test_p1_lazy_imports_under_adapter_and_typing_only_imports_are_allowed(tmp_path):
    src = _write(
        tmp_path,
        {
            "adapter/client.py": """
                from __future__ import annotations
                from typing import TYPE_CHECKING

                if TYPE_CHECKING:
                    from githubkit import GitHub

                def make() -> GitHub:
                    from githubkit import GitHub
                    return GitHub()

                async def amake():
                    import githubkit
                    return githubkit
            """,
            "factory.py": "import json\n",
        },
    )
    assert transport_violations(src) == []


# ---- P1: behavioural, in a fresh interpreter ---------------------------------------------------

_BLOCKER = """
import importlib.abc, sys

class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in {"githubkit", "githubkit_schemas", "hishel"}:
            raise ImportError("the github extra is not installed: " + name)

sys.meta_path.insert(0, _Block())
"""

_IMPORT_EVERYTHING = """
import importlib, pkgutil
import beadhive, beadhive.bootstrap.tracker
import beadhive_tracker_github
for info in pkgutil.walk_packages(beadhive_tracker_github.__path__, "beadhive_tracker_github."):
    importlib.import_module(info.name)
"""

_NO_TRANSPORT_LOADED = """
import sys
loaded = sorted(m for m in sys.modules if m.split(".")[0] in {"githubkit", "githubkit_schemas"})
assert not loaded, loaded
"""


def _python(code: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def test_p1_everything_imports_without_the_extra():
    """``beadhive`` and every module of this package import with githubkit unimportable."""
    result = _python(_BLOCKER + _IMPORT_EVERYTHING)
    assert result.returncode == 0, result.stderr


def test_p1_importing_everything_never_loads_githubkit_even_when_it_is_installed():
    """With the extra installed, importing beadhive and the whole package still loads no
    transport: it is imported only when an adapter is actually built."""
    result = _python(_IMPORT_EVERYTHING + _NO_TRANSPORT_LOADED)
    assert result.returncode == 0, result.stderr


def test_p1_negative_control_the_blocker_and_the_loaded_check_actually_bite(tmp_path):
    # A top-level githubkit import is refused by the blocker...
    _write(tmp_path, {"eager_probe.py": "import githubkit\n"})
    refused = _python(
        _BLOCKER + f"import sys; sys.path.insert(0, {str(tmp_path)!r})\nimport eager_probe\n"
    )
    assert refused.returncode != 0
    assert "the github extra is not installed: githubkit" in refused.stderr
    # ... and the loaded-module check fails when a transport module really is loaded.
    loaded = _python(
        "import sys, types\nsys.modules['githubkit'] = types.ModuleType('githubkit')\n"
        + _NO_TRANSPORT_LOADED
    )
    assert loaded.returncode != 0
    assert "githubkit" in loaded.stderr


# ---- G4 ----------------------------------------------------------------------------------------


def test_g4_the_package_imports_no_kernel_and_only_the_credentials_module_from_plugins():
    sites = list(import_sites(PACKAGE_SRC))
    assert any(site.module == "beadhive_plugins.credentials" for site in sites)  # non-vacuous
    assert plugin_layer_violations(PACKAGE_SRC) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from beadhive.kernel.plugins import ProviderKey\n", "plugin kernel"),
        ("import beadhive.kernel.plugins\n", "plugin kernel"),
        ("from beadhive import kernel\n", "plugin kernel"),
        ("from beadhive.kernel.plugins.contracts import CapabilityRef\n", "plugin kernel"),
        ("from beadhive_plugins.contracts import ManifestDocument\n", "only beadhive_plugins.cred"),
        ("from beadhive_plugins import contracts\n", "only beadhive_plugins.cred"),
        ("import beadhive_plugins\n", "only beadhive_plugins.cred"),
        ("from beadhive_plugins.worktree_slots import SlotPort\n", "only beadhive_plugins.cred"),
        (
            "from importlib import import_module\nimport_module('beadhive.kernel.plugins')\n",
            "plugin kernel",
        ),
    ],
)
def test_g4_negative_control_a_breach_is_reported(tmp_path, source, expected):
    problems = plugin_layer_violations(_write(tmp_path, {"adapter/wiring.py": source}))
    assert any(expected in problem for problem in problems), problems


def test_g4_the_allowed_imports_are_not_flagged(tmp_path):
    src = _write(
        tmp_path,
        {
            "adapter/ok.py": """
                from beadhive_plugins.credentials import ResolvedToken, TokenSource
                from beadhive_plugins import credentials
                from beadhive.modules.tracker.contracts import TrackerAdapter
                from . import sibling
            """
        },
    )
    assert plugin_layer_violations(src) == []
