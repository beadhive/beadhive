"""Skip importing test files that have no item a ``-m`` selection can keep (bh-l1jje).

Every xdist worker imports and collects the whole suite before it runs a test: 21-44 s per worker
on the integration lane, which keeps 279 of ~11,000 items from 57 of 581 files. With
``BH_TEST_PREFILTER=1`` and a ``-m`` expression, the controller reads a checked-in marker index
(``collection_index.json``, refreshed by ``just collection-index-refresh``) and tells every worker
which files to skip in ``pytest_ignore_collect``. The rest of collection is unchanged: the same
``tests`` argument, directory walk, conftests, hooks and ordering.

The index records, per test file, the distinct marker-name sets of its items as they stand after
every ``pytest_collection_modifyitems`` hook. A file is skipped only when ALL of these hold:

- the index's global digest matches. It covers every tests-local pytest plugin module and conftest
  (with their tests-local import closure), the collection-relevant ini values, the pytest and
  Python versions and the installed ``pytest11`` plugin distributions. A mismatch, or an unreadable
  index, falls back to full collection for the whole run;
- the file's own digest matches. It covers the file and its tests-local import closure, so a
  marker imported from a harness module is covered. A changed or new file is collected;
- the file had at least one item when indexed (a module-level skip or a collection error records
  nothing, so such a file is always collected);
- no recorded marker set matches the ``-m`` expression, evaluated by pytest's own expression
  parser. An expression using marker keyword arguments (``-m "m(k=v)"``) falls back to full
  collection, because the index records names only.

Other deselection (``-k``, ``--deselect``, the perf filter) only removes more items, so ignoring it
keeps the skipped set a subset of what a full run deselects. Markers taken from outside the test
roots (``src``) are not covered; no test does that today.

Opt out with ``BH_TEST_PREFILTER=0`` (the plugin's own default is off; the lane recipes turn it
on). ``BH_TEST_PREFILTER_INDEX`` points at another index.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from collections.abc import Iterable, Mapping
from importlib import metadata
from pathlib import Path

import pytest

ENABLE_ENV = "BH_TEST_PREFILTER"
INDEX_ENV = "BH_TEST_PREFILTER_INDEX"
#: Set to a path to write a fresh index from this (non-xdist) collection instead of filtering.
RECORD_ENV = "BH_TEST_PREFILTER_RECORD"
DEFAULT_INDEX = Path(__file__).with_name("collection_index.json")
VERSION = 1
_ON = frozenset({"1", "true", "yes", "on"})
_WORKER_KEY = "bh_prefilter_skip"
#: Ini values that shape collection. ``markers`` is left out: plugins append to it during
#: configure, so it differs between configure and collection, and registration selects nothing.
_INI_KEYS = (
    "python_files",
    "python_classes",
    "python_functions",
    "testpaths",
    "empty_parameter_set_mark",
)

_skip_key = pytest.StashKey[frozenset[str]]()
_summary_key = pytest.StashKey[str]()


class Fallback(Exception):
    """The index cannot be trusted for this run; collect everything."""


def enabled(env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return environ.get(ENABLE_ENV, "").strip().lower() in _ON


def index_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    override = environ.get(INDEX_ENV)
    return Path(override) if override else DEFAULT_INDEX


# --- digests -------------------------------------------------------------------------------------


def local_roots(config) -> list[Path]:
    """The test roots: ``testpaths`` directories, else the rootdir."""
    root = Path(config.rootpath)
    roots = [(root / p).resolve() for p in config.getini("testpaths") or ()]
    roots = [p for p in roots if p.is_dir()]
    return roots or [root.resolve()]


def _within(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def _imported_names(path: Path) -> list[tuple[int, str]]:
    """``(level, dotted)`` for every import in ``path``, from-imported names as submodules too."""
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return []
    names: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                names.append((node.level, base))
            for alias in node.names:
                names.append((node.level, f"{base}.{alias.name}" if base else alias.name))
    return names


def _candidates(base: Path, dotted: str) -> list[Path]:
    """Files run by importing ``dotted`` from ``base``: each package ``__init__`` and the leaf."""
    parts = dotted.split(".")
    found = []
    for i in range(1, len(parts) + 1):
        stem = base.joinpath(*parts[:i])
        init = stem / "__init__.py"
        if init.is_file():
            found.append(init)
        elif i == len(parts) and stem.with_suffix(".py").is_file():
            found.append(stem.with_suffix(".py"))
    return found


class Digester:
    """Content digests over tests-local import closures, memoised for one run."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = roots
        self._deps: dict[Path, set[Path]] = {}
        self._content: dict[Path, bytes] = {}

    def _bytes(self, path: Path) -> bytes:
        if path not in self._content:
            try:
                self._content[path] = path.read_bytes()
            except OSError:
                self._content[path] = b"\0missing"
        return self._content[path]

    def deps(self, path: Path) -> set[Path]:
        """Direct tests-local dependencies of ``path``, resolved over-inclusively."""
        if path in self._deps:
            return self._deps[path]
        # Every ancestor inside a test root, and each root's parent (``tests.harness`` imports).
        bases = [p for p in path.parents if _within(p, self.roots)]
        bases += [r.parent for r in self.roots]
        found: set[Path] = set()
        for level, dotted in _imported_names(path):
            if level:
                anchor = path.parents[level - 1] if level - 1 < len(path.parents) else None
                search = [anchor] if anchor is not None else []
            else:
                search = bases
            for base in search:
                found.update(p.resolve() for p in _candidates(base, dotted) if dotted)
        found = {p for p in found if _within(p, self.roots) and p != path}
        self._deps[path] = found
        return found

    def closure(self, paths: Iterable[Path]) -> list[Path]:
        seen: set[Path] = set()
        stack = [p.resolve() for p in paths]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self.deps(current) - seen)
        return sorted(seen)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def global_digest(config, digester: Digester) -> str:
    """Everything outside a test file that can change which markers its items carry."""
    root = Path(config.rootpath).resolve()
    hooks: set[Path] = set()
    for plugin in config.pluginmanager.get_plugins():
        filename = getattr(plugin, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if _within(path, digester.roots):
                hooks.add(path)
    for test_root in digester.roots:
        for dirpath, dirnames, filenames in os.walk(test_root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
            if "conftest.py" in filenames:
                hooks.add((Path(dirpath) / "conftest.py").resolve())
    dists = sorted(
        f"{ep.dist.name if ep.dist else '?'}=={ep.dist.version if ep.dist else '?'}:{ep.value}"
        for ep in metadata.entry_points(group="pytest11")
    )
    ini = json.dumps([str(config.getini(key)) for key in _INI_KEYS])
    # Paths are hashed relative to the rootdir, so a checkout anywhere yields the same digest.
    h = hashlib.sha256()
    for part in (
        f"v{VERSION}",
        pytest.__version__,
        f"{sys.version_info[0]}.{sys.version_info[1]}",
        ini,
        *dists,
    ):
        h.update(part.encode() + b"\0")
    for path in digester.closure(hooks):
        h.update(_rel(path, root).encode() + b"\0")
        h.update(hashlib.sha256(digester._bytes(path)).digest())
    return h.hexdigest()


def file_digest(path: Path, root: Path, digester: Digester) -> str:
    h = hashlib.sha256()
    for dep in digester.closure([path]):
        h.update(_rel(dep, root).encode() + b"\0")
        h.update(hashlib.sha256(digester._bytes(dep)).digest())
    return h.hexdigest()


# --- selection -----------------------------------------------------------------------------------


def _compile(markexpr: str):
    from _pytest.mark.expression import Expression

    try:
        return Expression.compile(markexpr)
    except SyntaxError as exc:
        raise Fallback(f"cannot parse -m {markexpr!r}: {exc}") from None


def selects(expression, names: Iterable[str]) -> bool:
    """Whether ``-m`` keeps an item with exactly these marker names. Keyword args: Fallback."""
    present = frozenset(names)

    def matcher(name: str, /, **kwargs) -> bool:
        if kwargs:
            raise Fallback("-m uses marker keyword arguments; the index records names only")
        return name in present

    return bool(expression.evaluate(matcher))


def skipped_files(config, index: Mapping, markexpr: str) -> frozenset[str]:
    """Absolute paths of the files a run of ``markexpr`` may skip. Raises Fallback."""
    if not isinstance(index, Mapping) or index.get("version") != VERSION:
        raise Fallback("index version mismatch")
    files = index.get("files")
    if not isinstance(files, Mapping):
        raise Fallback("index has no files table")
    expression = _compile(markexpr)
    root = Path(config.rootpath).resolve()
    digester = Digester(local_roots(config))
    if index.get("global") != global_digest(config, digester):
        raise Fallback("global digest mismatch (conftest, plugin, ini or pytest changed)")
    skip: set[str] = set()
    for rel, entry in files.items():
        marks = entry.get("marks") if isinstance(entry, Mapping) else None
        if not isinstance(marks, list) or not marks:
            continue
        if any(selects(expression, names) for names in marks):
            continue
        path = root / rel
        if not path.is_file() or entry.get("digest") != file_digest(path, root, digester):
            continue
        skip.add(str(path))
    return frozenset(skip)


# --- recording -----------------------------------------------------------------------------------


def build_index(config, items) -> dict:
    root = Path(config.rootpath).resolve()
    digester = Digester(local_roots(config))
    marks: dict[str, set[tuple[str, ...]]] = {}
    for item in items:
        path = Path(item.path).resolve()
        names = tuple(sorted({mark.name for mark in item.iter_markers()}))
        marks.setdefault(_rel(path, root), set()).add(names)
    return {
        "version": VERSION,
        "global": global_digest(config, digester),
        "files": {
            rel: {
                "digest": file_digest(root / rel, root, digester),
                "marks": [list(names) for names in sorted(sets)],
            }
            for rel, sets in sorted(marks.items())
        },
    }


def render_index(index: Mapping) -> str:
    """One line per file, so a refresh diffs per file."""
    lines = [
        f"  {json.dumps(rel)}: {json.dumps(entry, sort_keys=True)}"
        for rel, entry in index["files"].items()
    ]
    return (
        "{\n"
        f'"version": {index["version"]},\n'
        f'"global": {json.dumps(index["global"])},\n'
        '"files": {\n' + ",\n".join(lines) + "\n}\n}\n"
    )


# --- hooks ---------------------------------------------------------------------------------------


def pytest_configure(config) -> None:
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        config.stash[_skip_key] = frozenset(workerinput.get(_WORKER_KEY, ()))
        return
    config.stash[_skip_key] = frozenset()
    if os.environ.get(RECORD_ENV) or not enabled():
        return
    markexpr = config.getoption("markexpr", "") or ""
    if not markexpr:
        config.stash[_summary_key] = "collection prefilter: inactive (no -m expression)"
        return
    path = index_path()
    try:
        try:
            index = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise Fallback(f"cannot read {path}: {exc}") from None
        skip = skipped_files(config, index, markexpr)
    except Fallback as exc:
        config.stash[_summary_key] = f"collection prefilter: FALLBACK to full collection: {exc}"
        return
    config.stash[_skip_key] = skip
    config.stash[_summary_key] = (
        f"collection prefilter: skipping {len(skip)} of {len(index['files'])} indexed files "
        f"for -m {markexpr!r}"
    )


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    """Controller: hand each xdist worker the same skip set."""
    node.workerinput[_WORKER_KEY] = sorted(node.config.stash.get(_skip_key, frozenset()))


def pytest_report_header(config):
    summary = config.stash.get(_summary_key, None)
    return [summary] if summary else []


def pytest_ignore_collect(collection_path, config):
    if str(collection_path) in config.stash.get(_skip_key, frozenset()):
        return True
    return None


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(session, config, items):
    record = os.environ.get(RECORD_ENV)
    everything = list(items) if record else None
    result = yield
    if record and getattr(config, "workerinput", None) is None:
        Path(record).write_text(render_index(build_index(config, everything)))
    return result
