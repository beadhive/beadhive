"""Molecule spec loader + validator — the accuracy lever for the planning plane.

A molecule spec is a transient YAML doc that compiles into a beads swarm (an epic
plus child issues wired into a dependency DAG). This module is the pure,
CLI-free core: it loads the YAML (via ruamel, the repo's parser — pyyaml is not
installed) and validates the spec against the schema + the rig's closed label
dimensions, returning a list of human-readable problems (empty ⇒ valid).

Keeping it free of Typer / bd calls keeps it trivially unit-testable; `ws plan`
(a sibling bead) wraps it for the CLI surface.

Spec schema (see docs/PLANNING-PLANE.md "Molecule spec format"):

    epic: {title, description, design}
    issues:
      - handle: a            # local id, referenced by deps
        title: ...
        type: feature|task|bug|chore
        priority: 1
        description: ...
        acceptance: ...      # required (accuracy)
        design: ...
        size: m              # closed dim (if declared)
        model: opus          # closed dim (routing)
        harness: claude      # closed dim (routing)
        component: runtime    # open dim
        deps: [b, c]         # local handles this issue depends on
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from .registry import closed_dimensions

# Label fields on an issue that may map to a closed dimension. Only those that are
# actually declared closed in config are enforced; the rest are open (anything goes).
_DIMENSION_FIELDS = ("model", "harness", "component", "size")

_yaml = YAML()


class MoleculeError(ValueError):
    """Raised when a molecule spec fails validation. Carries the problem list."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid molecule spec:\n  - " + "\n  - ".join(problems))


def load_spec(path) -> dict:
    """Parse a molecule spec YAML file into a plain dict (ruamel round-trip mapping).

    Raises FileNotFoundError if the path is missing and MoleculeError if the
    document is not a top-level mapping (so callers get one consistent error type).
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"molecule spec not found: {p}")
    data = _yaml.load(p.read_text())
    if data is None:
        raise MoleculeError(["empty spec: expected top-level 'epic' and 'issues'"])
    if not isinstance(data, dict):
        raise MoleculeError([f"spec must be a YAML mapping, got {type(data).__name__}"])
    return data


def validate_spec(spec: dict, cfg) -> list[str]:
    """Return a list of validation problems for `spec` (empty ⇒ valid).

    Checks: epic present with a title; every issue has a unique handle, a title,
    and an acceptance; deps reference existing handles (no dangling/orphans); the
    dependency graph is acyclic; and any label value (model/harness/component/size)
    mapping to a CLOSED dimension is in that dimension's allowed set.
    """
    problems: list[str] = []
    problems += _check_epic(spec)
    issues = spec.get("issues")
    if not issues:
        problems.append("no issues: a molecule needs at least one issue under 'issues'")
        return problems
    if not isinstance(issues, list):
        problems.append("'issues' must be a list of issue mappings")
        return problems

    handles = _check_issue_fields(issues, problems)
    problems += _check_deps(issues, handles)
    problems += _check_closed_dimensions(issues, cfg)
    return problems


def validate_or_raise(spec: dict, cfg) -> dict:
    """Validate and return `spec` unchanged, or raise MoleculeError with the problems."""
    problems = validate_spec(spec, cfg)
    if problems:
        raise MoleculeError(problems)
    return spec


# ---- checks ----------------------------------------------------------------


def _check_epic(spec: dict) -> list[str]:
    epic = spec.get("epic")
    if not epic:
        return ["missing epic: spec needs a top-level 'epic' with a title"]
    if not isinstance(epic, dict):
        return ["epic must be a mapping with a 'title'"]
    if not str(epic.get("title") or "").strip():
        return ["epic is missing a title"]
    return []


def _handle_label(issue, index: int) -> str:
    """A stable reference for error messages: the handle if present, else position."""
    handle = str(issue.get("handle") or "").strip() if isinstance(issue, dict) else ""
    return f"issue '{handle}'" if handle else f"issue #{index + 1}"


def _check_issue_fields(issues: list, problems: list[str]) -> set[str]:
    """Validate per-issue required fields + handle uniqueness; return the handle set."""
    handles: set[str] = set()
    for index, issue in enumerate(issues):
        label = _handle_label(issue, index)
        if not isinstance(issue, dict):
            problems.append(f"{label}: must be a mapping")
            continue
        handle = str(issue.get("handle") or "").strip()
        if not handle:
            problems.append(f"{label}: missing 'handle' (local id used by deps)")
        elif handle in handles:
            problems.append(f"{label}: duplicate handle '{handle}'")
        else:
            handles.add(handle)
        if not str(issue.get("title") or "").strip():
            problems.append(f"{label}: missing 'title'")
        if not str(issue.get("acceptance") or "").strip():
            problems.append(f"{label}: missing 'acceptance' (required for accuracy)")
    return handles


def _check_deps(issues: list, handles: set[str]) -> list[str]:
    """Flag dangling dep handles, then detect cycles over the dependency graph."""
    problems: list[str] = []
    graph: dict[str, list[str]] = {}
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            continue
        handle = str(issue.get("handle") or "").strip()
        deps = issue.get("deps") or []
        if not isinstance(deps, list):
            problems.append(f"{_handle_label(issue, index)}: 'deps' must be a list of handles")
            continue
        clean: list[str] = []
        for dep in deps:
            dep = str(dep).strip()
            if dep not in handles:
                problems.append(
                    f"{_handle_label(issue, index)}: dep '{dep}' references an unknown handle"
                )
            else:
                clean.append(dep)
        if handle:
            graph[handle] = clean

    cycle = _find_cycle(graph)
    if cycle:
        problems.append("dependency cycle detected: " + " -> ".join(cycle))
    return problems


def _find_cycle(graph: dict[str, list[str]]) -> list[str]:
    """Return a node sequence forming a cycle (closing back on the first), or [] if acyclic.

    Iterative DFS with a 3-color marking (white/grey/black) so the graph is a DAG iff
    no grey node is revisited on the current path.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in graph}

    for root in graph:
        if color[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        while stack:
            node, i = stack.pop()
            if i == 0:
                color[node] = GREY
                path.append(node)
            neighbors = graph.get(node, [])
            if i < len(neighbors):
                stack.append((node, i + 1))
                nxt = neighbors[i]
                if color.get(nxt, BLACK) == GREY:
                    return path[path.index(nxt) :] + [nxt]
                if color.get(nxt, BLACK) == WHITE:
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                if path and path[-1] == node:
                    path.pop()
    return []


def _check_closed_dimensions(issues: list, cfg) -> list[str]:
    """Any model/harness/component/size value mapping to a CLOSED dim must be allowed."""
    closed = closed_dimensions(cfg)
    problems: list[str] = []
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            continue
        for field in _DIMENSION_FIELDS:
            allowed = closed.get(field)
            if allowed is None:  # open dimension (or not declared) — accept anything
                continue
            value = issue.get(field)
            if value is None:
                continue
            if str(value) not in allowed:
                allowed_str = ", ".join(sorted(allowed))
                problems.append(
                    f"{_handle_label(issue, index)}: {field} '{value}' not in closed set "
                    f"{{{allowed_str}}}"
                )
    return problems
