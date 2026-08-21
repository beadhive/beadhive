"""Molecule spec loader + validator — the accuracy lever for the planning plane.

A molecule spec is a transient YAML doc that compiles into a beads swarm (an epic
plus child issues wired into a dependency DAG). This module is the pure,
CLI-free core: it loads the YAML (via ruamel, the repo's parser — pyyaml is not
installed) and validates the spec against the schema + the hive's closed label
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
        complexity: COMPLEX  # closed, provider-neutral routing capability
        model: anthropic/claude-opus-4 # optional provider/model preference (open)
        harness: claude      # closed dim (routing)
        component: runtime    # open dim
        batch: same-file     # group handled as ONE parallel unit (open dim)
        tag: spike            # open dim — e.g. the spike-loop's tag:spike / tag:decision
        deps: [b, c]         # local handles this issue depends on

`tag` also reads on the top-level `epic:` mapping (a spike epic is labeled `tag: spike` too;
see docs/PLANNING-PLANE.md "Spike loop") — `_issue_labels` in plan.py applies the same
dimension fields to the epic dict as to each issue dict.

Acceptance stubs: acceptance text starting with ``STUB:`` (STUB_MARKER) is an explicit
placeholder — the planner skill's ``--allow-stubs`` mode writes it when drafting real
acceptance isn't possible yet. A stub is PRESENT (validate_spec raises no error, so it
never blocks where only errors block) but it is visible debt: `acceptance_records`
reports it as a warning ("acceptance is stubbed — replace before review") so
`bh plan check` / `bh plan verify` never render the molecule silently convention-clean.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from . import complexity, config
from .registry import closed_dimensions

# Label fields on an issue that become a `<field>:<value>` label and may map to a closed
# dimension. Only those actually declared closed in config are enforced; the rest are open
# (anything goes). `batch` is the grouping label — open by nature (group names are per-molecule).
# `release` is a code-owned CLOSED dimension (breaking|feature|fix — see registry.RELEASE_VALUES)
# regardless of config, mirroring the state-vocabulary dims. `wave` is an OPEN batching label —
# release cohesion, distinct from the worktree-collapse `batch:<group>` grouping.
#
# `tag` (bh-0a6g): an OPEN dimension so a molecule spec can declare a bead's `tag:<value>`
# label — chiefly the spike-loop convention (docs/PLANNING-PLANE.md "Spike loop"): spike beads
# `tag:spike`, the decision bead `tag:decision`, the spike epic also `tag:spike`. Before this,
# `bh plan file` (the ONLY sanctioned filing path) had no way to express those labels at all,
# so filing a spike molecule "by the book" still required manual `bh bd label add` calls after
# the fact — a molecule that had passed `bh plan verify` could still be non-conformant to a
# convention the compiler itself couldn't produce.
#
# Trade-off record — three mechanisms were on the table (see the bead for the full sketch):
#   (a) add `tag` here, as a generic open dimension field — CHOSEN. Smallest diff, reuses the
#       existing per-issue label machinery verbatim (no new code path), and every real
#       spike/decision bead observed so far (bh-vxxw, bh-i6jp) carries exactly ONE `tag:`
#       value, so the "one field per issue" ceiling costs nothing in practice today. The
#       trade-off: `tag:` is OPEN (any string), unlike the closed dims around it, and a bead
#       that someday needs TWO tags at once cannot express both through this field — if that
#       need materializes, extend `tag` to accept a list (or add a general `labels:` list,
#       option (b) below) then; YAGNI says not to build it speculatively now.
#   (b) a general `labels:` list on the spec schema — more flexible (arbitrary labels, not just
#       one `tag:`), but overlaps this very dimension-fields mechanism and invites the two to
#       drift (which one is authoritative for `model:`/`size:`/etc?), plus label values could
#       silently diverge from the registry's closed-dimension checks.
#   (c) derive `tag:spike`/`tag:decision` structurally from molecule shape (a decision bead
#       depending on every other leaf ⇒ spike molecule) rather than by declaration — unforgeable
#       (can't file a mislabeled spike), but by far the largest change: `bh plan file` would need
#       shape-detection logic mirrored in the compiler AND the verifier, and every non-spike
#       molecule shaped similarly (e.g. a "rollup" bead depending on every sibling) risks a false
#       positive. Declarative (a) is enough now that the compiler can express the label at all.
DIMENSION_FIELDS = (
    "complexity",
    "model",
    "harness",
    "component",
    "size",
    "batch",
    "release",
    "wave",
    "tag",
)
_DIMENSION_FIELDS = DIMENSION_FIELDS  # compatibility for callers predating the public declaration

# THE acceptance stub-marker convention (see module docstring): text starting with this is an
# explicit placeholder — reported as a WARNING, distinct from the missing-acceptance ERROR.
STUB_MARKER = "STUB:"

# Shared message tails so the string problems (validate_spec) and the structured records
# (acceptance_records) cannot drift apart.
_MISSING_ACCEPTANCE = "missing 'acceptance' (required for accuracy)"
_STUBBED_ACCEPTANCE = "acceptance is stubbed — replace before review"

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
    dependency graph is acyclic; any label value (complexity/harness/component/size) mapping to
    a CLOSED dimension is in that dimension's allowed set; and every declared `batch:<group>`
    is cohesive enough to run as one unit (shared model, within the size cap, same component
    or contiguous in the DAG).
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
    problems += _check_batches(issues, cfg)
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
    problems = []
    if not str(epic.get("title") or "").strip():
        problems.append("epic is missing a title")
    problems += _routing_field_problems(epic, "epic", "epic")
    return problems


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
            problems.append(f"{label}: {_MISSING_ACCEPTANCE}")
        problems += _routing_field_problems(issue, issue.get("type") or "task", label)
    return handles


def _routing_field_problems(item: dict, issue_type: str, label: str) -> list[str]:
    """Validate singular spec fields for a routable item after compiler normalization."""
    if not complexity.is_routable_issue_type(issue_type):
        return []
    problems = []
    value = item.get("complexity")
    try:
        complexity.ComplexityTier.parse(value)
    except ValueError:
        expected = ", ".join(complexity.tier_names())
        if value in (None, ""):
            problems.append(f"{label}: missing 'complexity' (expected one of {{{expected}}})")
        else:
            problems.append(f"{label}: complexity '{value}' not in closed set {{{expected}}}")
    model = item.get("model")
    if model not in (None, "") and not complexity.valid_model_preference(model):
        problems.append(f"{label}: {complexity.MODEL_PREFERENCE_ERROR}")
    return problems


# ---- acceptance records (machine surface) -----------------------------------


def is_stub_acceptance(text) -> bool:
    """True when acceptance text is an explicit placeholder (starts with STUB_MARKER)."""
    return str(text or "").strip().startswith(STUB_MARKER)


def acceptance_records(issues) -> list[dict]:
    """Structured acceptance-problem records: one ``{id, field, severity, message}`` dict per
    issue whose acceptance is missing (severity ``error`` — the same condition validate_spec
    flags) or stubbed via STUB_MARKER (severity ``warning`` — visible debt, never blocking).

    ``id`` is the issue handle (a bead id when the issues come from a filed epic), ``""`` when
    unset; ``message`` matches the human rendering. This is the machine surface
    `bh plan check --json` and the MCP `plan_check` tool expose for the planner skill's
    acceptance-drafting modes. Non-list / non-dict input yields no records.
    """
    records: list[dict] = []
    if not isinstance(issues, list):
        return records
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            continue
        label = _handle_label(issue, index)
        handle = str(issue.get("handle") or "").strip()
        text = str(issue.get("acceptance") or "").strip()
        if not text:
            severity, message = "error", f"{label}: {_MISSING_ACCEPTANCE}"
        elif text.startswith(STUB_MARKER):
            severity, message = "warning", f"{label}: {_STUBBED_ACCEPTANCE}"
        else:
            continue
        records.append(
            {"id": handle, "field": "acceptance", "severity": severity, "message": message}
        )
    return records


def acceptance_summary(issues) -> dict:
    """The machine-readable acceptance block shared by `bh plan check --json` and the MCP
    `plan_check` tool: stub ``warnings`` (messages), ``missing_acceptance`` /
    ``stubbed_acceptance`` id lists, and the full per-record ``acceptance_problems``."""
    records = acceptance_records(issues)
    return {
        "warnings": [r["message"] for r in records if r["severity"] == "warning"],
        "missing_acceptance": [r["id"] for r in records if r["severity"] == "error"],
        "stubbed_acceptance": [r["id"] for r in records if r["severity"] == "warning"],
        "acceptance_problems": records,
    }


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
    """Any complexity/harness/component/size value mapping to a CLOSED dim must be allowed."""
    closed = closed_dimensions(cfg)
    problems: list[str] = []
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            continue
        for field in DIMENSION_FIELDS:
            if field == "complexity":  # canonical + required is owned by _routing_field_problems
                continue
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


# ---- batch grouping --------------------------------------------------------


def _batch_groups(issues: list) -> dict[str, list[dict]]:
    """Map batch group name -> member issue dicts (spec order). Issues without a non-empty
    'batch' field are unbatched and excluded."""
    groups: dict[str, list[dict]] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        group = str(issue.get("batch") or "").strip()
        if group:
            groups.setdefault(group, []).append(issue)
    return groups


def _check_batches(issues: list, cfg) -> list[str]:
    """A `batch:<group>` gathers issues the coordinator runs as ONE parallel unit — one
    worktree, validated and merged once. Each declared group must be cohesive enough to do
    that: members share a model preference, stay within the size cap, and hang together (same
    component OR contiguous in the dep DAG). Reject otherwise so the coordinator never
    schedules a batch that cannot be run as a unit.
    """
    groups = _batch_groups(issues)
    if not groups:
        return []
    cap = config.batch_max_size(cfg, None)
    problems: list[str] = []
    for group, members in groups.items():
        problems += _check_batch_model(group, members)
        problems += _check_batch_cap(group, members, cap)
        problems += _check_batch_cohesion(group, members)
    return problems


def _check_batch_model(group: str, members: list[dict]) -> list[str]:
    """A batch runs as one unit, so its members cannot ask for different model preferences (members
    may omit model to inherit; only an explicit conflict is rejected)."""
    models = {str(m.get("model")).strip() for m in members if m.get("model") not in (None, "")}
    if len(models) > 1:
        return [
            f"batch '{group}': mixed model preferences "
            f"{{{', '.join(sorted(models))}}} — a batch runs "
            f"as one unit and must share a model (omit model to inherit)"
        ]
    return []


def _check_batch_cap(group: str, members: list[dict], cap: int) -> list[str]:
    """A batch bubble stays small enough to review/bisect — cap the member count."""
    if len(members) > cap:
        return [
            f"batch '{group}': {len(members)} members exceeds the cap of {cap} — split the "
            f"group or raise work.batch_max_size"
        ]
    return []


def _check_batch_cohesion(group: str, members: list[dict]) -> list[str]:
    """Members must hang together: all share one component, or form a contiguous (connected)
    subgraph in the dependency DAG. A single-member batch is trivially cohesive."""
    if len(members) < 2:
        return []
    declared = [str(m.get("component") or "").strip() for m in members]
    same_component = all(declared) and len(set(declared)) == 1
    if same_component or _members_contiguous(members):
        return []
    return [
        f"batch '{group}': not cohesive — members must share a component or be contiguous "
        f"(connected via deps) in the DAG"
    ]


def _members_contiguous(members: list[dict]) -> bool:
    """True if the batch members form a connected subgraph under their mutual deps (treated as
    undirected edges); deps pointing outside the batch are ignored."""
    handles = {h for m in members if (h := str(m.get("handle") or "").strip())}
    if len(handles) <= 1:
        return True
    adj: dict[str, set[str]] = {h: set() for h in handles}
    for m in members:
        handle = str(m.get("handle") or "").strip()
        if not handle:
            continue
        for dep in m.get("deps") or []:
            dep = str(dep).strip()
            if dep in handles:
                adj[handle].add(dep)
                adj[dep].add(handle)
    start = next(iter(handles))
    seen = {start}
    stack = [start]
    while stack:
        for nxt in adj[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen == handles
