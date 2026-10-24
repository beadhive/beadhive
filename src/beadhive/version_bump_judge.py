"""Content judge: is this change a version bump and nothing else? (bh-d21ng)

A release bump commit changes only ``CHANGELOG.md`` plus the bh version value in
``pyproject.toml``, ``uv.lock`` and ``server.json``. Path globs alone cannot decide that, since a
dependency change touches the same files, so this judge PARSES the old and new content:

* ``pyproject.toml`` -- identical once ``[project].version`` is removed from both;
* ``uv.lock`` -- identical once ``version`` is removed from the root package entry(ies), i.e.
  the packages named like the pyproject project;
* ``server.json`` -- identical once the top-level ``version`` and each ``packages[].version`` are
  removed;
* ``CHANGELOG.md`` -- any change.

Any other path, an added or deleted file, unparsable content, or no version actually changing is
not a yes. The answer vocabulary is the trivial-selection one: ``yes`` / ``no`` / ``unknown``;
``unknown`` means "cannot tell" and always resolves to the normal route.

The judge is pure over ``{path: (old_text | None, new_text | None)}``; reading git is the
caller's (:func:`read_changes`).
"""

from __future__ import annotations

import copy
import fnmatch
import json
import tomllib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

YES = "yes"
NO = "no"
UNKNOWN = "unknown"

CHANGELOG = "CHANGELOG.md"
PYPROJECT = "pyproject.toml"
UV_LOCK = "uv.lock"
SERVER_JSON = "server.json"

Change = tuple[str | None, str | None]


class _Unparsable(ValueError):
    pass


def _toml(text: str | None) -> dict[str, Any]:
    if text is None:
        raise _Unparsable("file added or deleted")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _Unparsable(f"toml: {exc}") from exc


def _project_name(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    names = {
        (doc.get("project") or {}).get("name") for doc in (old, new) if isinstance(doc, Mapping)
    }
    names.discard(None)
    if len(names) != 1 or not all(isinstance(n, str) and n for n in names):
        raise _Unparsable("project name missing or changed")
    return names.pop()


def _strip_project_version(doc: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    doc = copy.deepcopy(doc)
    project = doc.get("project")
    if not isinstance(project, dict) or "version" not in project:
        raise _Unparsable("pyproject has no static [project].version")
    version = project.pop("version")
    return doc, str(version)


def _strip_lock_versions(doc: dict[str, Any], name: str) -> tuple[dict[str, Any], list[Any]]:
    doc = copy.deepcopy(doc)
    packages = doc.get("package")
    if not isinstance(packages, list):
        raise _Unparsable("uv.lock has no package entries")
    versions: list[Any] = []
    for entry in packages:
        if isinstance(entry, dict) and entry.get("name") == name:
            source = entry.get("source")
            # The root is the editable/virtual local package, never a registry copy.
            if not isinstance(source, dict) or not (
                "editable" in source or "virtual" in source or "directory" in source
            ):
                raise _Unparsable("project name matches a non-local lock package")
            versions.append(entry.pop("version", None))
    if not versions:
        raise _Unparsable("uv.lock has no root package entry")
    return doc, versions


def _strip_server_versions(text: str | None) -> tuple[dict[str, Any], list[Any]]:
    if text is None:
        raise _Unparsable("file added or deleted")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Unparsable(f"json: {exc}") from exc
    if not isinstance(doc, dict):
        raise _Unparsable("server.json is not an object")
    versions = [doc.pop("version", None)]
    packages = doc.get("packages")
    if isinstance(packages, list):
        for pkg in packages:
            if isinstance(pkg, dict):
                versions.append(pkg.pop("version", None))
    return doc, versions


def judge_version_bump(changes: Mapping[str, Change]) -> tuple[str, str, bool]:
    """``(verdict, reason, version_changed)`` for the per-path old/new content of one change.

    ``yes`` only when every changed path is an allowed file whose diff is confined to the
    version values (or is the changelog) AND at least one version value really changed.
    """
    if not changes:
        return UNKNOWN, "no changed paths", False
    allowed = {CHANGELOG, PYPROJECT, UV_LOCK, SERVER_JSON}
    extra = sorted(set(changes) - allowed)
    if extra:
        return NO, f"changes beyond a version bump: {extra[:5]}", False
    bumped = False
    try:
        py = changes.get(PYPROJECT)
        old_py = _toml(py[0]) if py else None
        new_py = _toml(py[1]) if py else None
        if py is not None:
            old_stripped, old_v = _strip_project_version(old_py)
            new_stripped, new_v = _strip_project_version(new_py)
            if old_stripped != new_stripped:
                return NO, "pyproject.toml changes more than [project].version", False
            bumped = bumped or old_v != new_v
        lock = changes.get(UV_LOCK)
        if lock is not None:
            if old_py is None:
                # The root package name is needed; without pyproject in the diff it is not
                # derivable from the diff alone, so cannot be judged here.
                return UNKNOWN, "uv.lock changed without pyproject.toml", False
            name = _project_name(old_py, new_py)
            old_stripped, old_vs = _strip_lock_versions(_toml(lock[0]), name)
            new_stripped, new_vs = _strip_lock_versions(_toml(lock[1]), name)
            if old_stripped != new_stripped:
                return NO, "uv.lock changes more than the root package version", False
            bumped = bumped or old_vs != new_vs
        server = changes.get(SERVER_JSON)
        if server is not None:
            old_stripped, old_vs = _strip_server_versions(server[0])
            new_stripped, new_vs = _strip_server_versions(server[1])
            if old_stripped != new_stripped:
                return NO, "server.json changes more than its version fields", False
            bumped = bumped or old_vs != new_vs
    except _Unparsable as exc:
        return UNKNOWN, f"unparsable: {exc}", False
    if not bumped:
        return UNKNOWN, "no version value changed", False
    return YES, "diff is a version bump only", True


def path_judge(paths: Sequence[str], patterns: Sequence[str]) -> str:
    """Glob judge: ``yes`` when EVERY changed path matches a pattern; ``unknown`` if unset."""
    if not patterns or not paths:
        return UNKNOWN
    if all(any(fnmatch.fnmatchcase(p, pat) for pat in patterns) for p in paths):
        return YES
    return NO


def decide(
    policy,
    paths: Sequence[str],
    read: Callable[[Sequence[str]], Mapping[str, Change]],
    *,
    parents: Sequence[str],
    base_tree: str,
    parent_tree: str,
) -> tuple[bool, dict[str, Any]]:
    """``(carry?, judge_record)``. Two independent judges must BOTH say yes; every doubt, any
    exception, a non-single parent, or a base that is not that parent's tree is no carry."""
    record: dict[str, Any] = {
        "kind": "version-bump",
        "path_verdict": UNKNOWN,
        "content_verdict": UNKNOWN,
        "applied": False,
        "parent_tree": base_tree,
    }
    if policy is None or not policy.enabled or not policy.keys:
        return False, record
    if len(parents) != 1:
        record["reason"] = f"head has {len(parents)} parents, need exactly one"
        return False, record
    if parent_tree != base_tree:
        record["reason"] = "base tree is not the head's parent tree"
        return False, record
    pv = path_judge(paths, policy.paths)
    record["path_verdict"] = pv
    if pv != YES:
        record["reason"] = "path judge: not confined to configured version-bump files"
        return False, record
    try:
        cv, reason, _ = judge_version_bump(read(paths))
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot tell"
        cv, reason = UNKNOWN, f"{type(exc).__name__}: {exc}"[:200]
    record["content_verdict"] = cv
    record["reason"] = reason
    if cv != YES:
        return False, record
    record["applied"] = True
    return True, record
