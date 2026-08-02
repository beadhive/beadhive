"""bh-l44i's `validate_cmd` "does it actually run tests" probe.

The first cut of this check pattern-matched the top-level command string for a ``"test"``
substring. That reads *every* hive using the fleet-wide dominant convention — a bare
``just check`` that fans out through ``check: lint lint-md test`` before it ever reaches
``uv run pytest`` — as compile-only, because the string ``"just check"`` itself never mentions
tests. Firing wrong on the common case (confirmed live: it fired on ~20 of 20 hives) trains
operators to ignore the warning, the exact failure mode bh-05w7 fixed for
``bh config validate``.

This resolves ``just <recipe>`` through the hive's own justfile instead: follow the recipe's
declared dependencies (``check: lint lint-md test``) and any ``just <other-recipe>`` calls
inside a body, transitively, and look for a test signal in the whole reachable graph — not just
the one line the operator typed.

Tri-state, not boolean, because "we resolved it and it's clean" and "we couldn't resolve it at
all" are different findings with different confidence:

- ``False`` — a test signal was found (in the raw command, or anywhere in the resolved graph).
  Never warn.
- ``True``  — the ENTIRE reachable graph was resolved and none of it looks like a test run.
  The only warn-worthy result.
- ``None``  — can't tell: not a ``just`` command, no justfile, an unresolvable recipe reference,
  or a graph deeper than the safety cap. Never warn — an unresolvable command is exactly the
  case a false "compile-only" verdict would repeat bh-05w7's mistake in.

Only ``just`` is resolved today (the pattern actually observed firing wrong); any other command
shape — ``make check``, a bespoke script, an empty ``validate_cmd`` — degrades to ``None``
rather than guessing. Extending to other task runners (``make``, ``npm``) is future work, not a
regression: they were already ``None``-equivalent (guessed test-free) under the old heuristic
whenever their command text didn't literally say "test", so this is strictly more conservative,
never less.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_TEST_SIGNAL_RE = re.compile(r"test", re.IGNORECASE)
_JUST_INVOKE_RE = re.compile(r"(?<![\w-])just\s+([A-Za-z_][\w-]*)")
_JUSTFILE_NAMES = ("justfile", "Justfile", ".justfile")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][\w-]*\s*:=")
_RECIPE_HEADER_RE = re.compile(r"^([A-Za-z_][\w-]*)[^:]*:(.*)$")
_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_SHELL_WRAPPERS = ("sh", "bash", "zsh")
# Defensive only — a valid justfile can't have dependency cycles, but nothing here re-validates
# that, so a corrupt/huge file degrades to "unknown" instead of recursing unboundedly.
_MAX_DEPTH = 20


def _looks_like_tests(text: str) -> bool:
    return bool(_TEST_SIGNAL_RE.search(text or ""))


def _find_justfile(root: Path) -> Path | None:
    for name in _JUSTFILE_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _dep_names(deps_part: str) -> list[str]:
    """Dependency names from the text after a recipe's ``:`` — bare tokens (``lint``) and the
    call-name of a parenthesized recipe-with-args (``(test FULL)`` -> ``test``, dropping the
    arg)."""
    names: list[str] = []
    in_call = False
    for tok in deps_part.split():
        if in_call:
            if tok.endswith(")"):
                in_call = False
            continue
        if tok.startswith("("):
            name = tok[1:].rstrip(")")
            if _NAME_RE.match(name):
                names.append(name)
            in_call = not tok.endswith(")")
            continue
        bare = tok.rstrip(")")
        if _NAME_RE.match(bare):
            names.append(bare)
    return names


def _parse_recipes(text: str) -> dict[str, dict]:
    """A tolerant, best-effort parse of a justfile's recipes: ``name -> {"deps": [...], "body":
    str}``. Anything this can't confidently parse (comments, ``NAME := value`` variable
    assignments, ``[attribute]`` lines, recipe parameters like ``test set=FAST:``) is simply not
    treated as a recipe header — worst case a recipe goes unrecognized and the caller degrades
    to "unknown", never misattributed as test-free."""
    recipes: dict[str, dict] = {}
    current: str | None = None
    for raw in text.splitlines():
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            current = None
            continue
        if raw[:1] in (" ", "\t"):
            if current is not None:
                recipes[current]["body"] += raw + "\n"
            continue
        if raw.startswith("[") or _ASSIGNMENT_RE.match(raw):
            current = None
            continue
        m = _RECIPE_HEADER_RE.match(raw)
        if not m:
            current = None
            continue
        name, deps_part = m.group(1), m.group(2)
        recipes[name] = {"deps": _dep_names(deps_part), "body": ""}
        current = name
    return recipes


def _collect(recipes: dict[str, dict], name: str, visited: set[str], depth: int) -> str | None:
    """DFS-collect ``name``'s own text plus everything it transitively reaches (declared deps +
    any ``just <other>`` call inside a body). ``None`` the moment anything can't be trusted: an
    unresolved reference or a graph past ``_MAX_DEPTH``."""
    if depth > _MAX_DEPTH:
        return None
    if name in visited:
        return ""  # already counted elsewhere in the graph — no new signal, not a failure
    recipe = recipes.get(name)
    if recipe is None:
        return None  # a dependency this parse never found a header for — can't trust the graph
    visited.add(name)
    parts = [name, recipe["body"]]
    called = list(recipe["deps"]) + [c for c in _JUST_INVOKE_RE.findall(recipe["body"])]
    for dep in called:
        sub = _collect(recipes, dep, visited, depth + 1)
        if sub is None:
            return None
        parts.append(sub)
    return "\n".join(parts)


def _resolve_just_recipe_text(root: Path, target: str) -> str | None:
    """The combined name+body text of ``target`` and everything it transitively reaches in
    ``root``'s justfile, or ``None`` if there's no justfile / the graph can't be fully
    resolved."""
    justfile = _find_justfile(root)
    if justfile is None:
        return None
    try:
        recipes = _parse_recipes(justfile.read_text(errors="ignore"))
    except OSError:
        return None
    return _collect(recipes, target, set(), 0)


def _bare_just_target(cmd: str) -> str | None:
    """The recipe name of a bare ``just <recipe> …`` invocation, peeling back one level of
    ``sh -c '…'`` / ``bash -c '…'`` wrapping first. ``None`` for anything else (flags-only
    invocations, a non-``just`` command, unparseable shell quoting)."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    if not tokens:
        return None
    if tokens[0] in _SHELL_WRAPPERS and "-c" in tokens:
        idx = tokens.index("-c")
        if len(tokens) > idx + 1:
            return _bare_just_target(tokens[idx + 1])
        return None
    if tokens[0] != "just":
        return None
    for tok in tokens[1:]:
        if tok.startswith("-"):
            continue
        return tok
    return None


def probe_validate_cmd(cmd: str, root: Path | None) -> bool | None:
    """Tri-state verdict on whether *cmd* (a hive's effective ``validate_cmd``) looks like it
    runs tests, resolving a bare ``just <recipe>`` through *root*'s justfile instead of
    pattern-matching the string alone. See the module docstring for the full rationale and the
    True/False/None contract."""
    if _looks_like_tests(cmd):
        return False
    target = _bare_just_target(cmd)
    if target is None or root is None:
        return None
    resolved = _resolve_just_recipe_text(root, target)
    if resolved is None:
        return None
    return not _looks_like_tests(resolved)
