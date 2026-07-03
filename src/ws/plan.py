"""`ws plan` — the planning-plane driver.

Takes a raw idea (feature / change / refactor) through ideation → research →
architecture → decompose → file molecule, producing a beads swarm (epic + child
issues + dependency DAG) that a coordinator later implements via `ws work`. It is a
thin facade: verbs compose `bd` (Beads) and the molecule-spec mechanics that already
exist. Raw git is for the change *inside* a worktree only — this module owns only the
planning lifecycle.

Test seam: this module shells out to **`bd` only** (via `_bd`); tests patch
`ws.plan.run` to fake Beads while YAML/validation logic runs for real.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from . import adopt, config, molecule, otel, registry, state, validate
from .identity import resolve_actor, workspace_identity
from .run import run

app = typer.Typer(no_args_is_help=True, help="Plan a molecule → swarm (planning plane).")


class PlanError(Exception):
    """A planning-plane operation failed. Typer-free; the CLI maps it to a stderr + exit 1."""


@dataclass
class FileResult:
    """Outcome of filing a molecule: the new epic id and its issue / kickoff-gate counts,
    plus how many originating reports were linked back (0 unless the molecule was adopted)."""

    epic_id: str
    issue_count: int
    root_count: int
    adopt_count: int = 0


# ---- shared plumbing ---------------------------------------------------------

_RIG = typer.Option("", "--rig", "-r", help="target rig (default: cwd's rig)")

# Module-level singleton for the variadic `adopt` positional — a `list[str]` default inline would
# trip flake8-bugbear B008 (list is mutable), so the Argument is read from here (mirrors `_RIG`).
_ADOPT_BEADS = typer.Argument(
    ..., metavar="<intake-bead>...", help="promoted intake bead id(s) to seed a frame from"
)

# Issue fields that map to a label dimension (`<field>:<value>`), filed alongside the
# auto-injected provider/org/repo identity triplet. Mirrors molecule._DIMENSION_FIELDS.
# `batch` carries planner-declared batch membership through to the filed beads as `batch:<group>`.
_DIMENSION_FIELDS = ("model", "harness", "component", "size", "batch")

# --- `bd create --graph <json>` spike (bd 1.0.5) -----------------------------
# Tried a single atomic call: `{"nodes": [{key,title,type,priority,description,labels,
# parent_key,parent_id}], "edges": [{from_key,to_key,type}]}`. It DOES create an epic +
# children + parent links + dependency edges + labels in one shot — but it **silently drops
# `acceptance`/`design`** (warns: "unknown field(s)"). Acceptance is the molecule's required
# accuracy field, so --graph would lose it. It also bypasses the triplet-injection wrapper.
# Decision: file per-issue (`bd create` carries --acceptance/--design/--deps/-l), in
# dependency (topological) order so each `--deps` references an already-created real id.


def _bd(args, cwd, actor="", capture=False, text_input=None):
    """Run a `bd` subcommand scoped to the rig via `-C <cwd>` (so the right Beads DB is hit
    regardless of the process cwd / `--rig`). Prepends `--actor <name>` for the audit trail.
    `text_input` feeds stdin (e.g. a JSONL record for `bd import -`)."""
    cmd = ["bd", "-C", str(cwd)]
    if actor:
        cmd += ["--actor", actor]
    cmd += list(args)
    return run(cmd, check=False, capture=capture, text_input=text_input)


def _bd_json(args, cwd):
    """Parse `bd <args> --json`, or None on failure."""
    res = _bd([*args, "--json"], cwd, capture=True)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "null")
    except json.JSONDecodeError:
        return None


# ---- rig + spec helpers ------------------------------------------------------


def _rig_dir(cfg, rig: str) -> Path:
    """The rig directory bd should target: the resolved managed rig for `--rig`, else cwd."""
    if rig:
        return registry.rig_dir(registry.resolve_rig(cfg, rig))
    return Path.cwd()


def _triplet_labels(cwd) -> list[str]:
    """The provider/org/repo identity labels for `cwd`, mirroring `ws bd create` (bd.py:_create);
    [] when outside a managed workspace path."""
    ident = workspace_identity(cwd)
    if ident is None:
        return []
    provider, org, repo = ident
    return [f"provider:{provider},org:{org},repo:{repo}"]


def _issue_labels(issue: dict, cwd) -> list[str]:
    """`-l <labels>` args for an issue: its declared dimensions + the identity triplet."""
    dims = [f"{f}:{issue[f]}" for f in _DIMENSION_FIELDS if issue.get(f) not in (None, "")]
    labels = dims + _triplet_labels(cwd)
    return ["-l", ",".join(labels)] if labels else []


def _topo_order(issues: list[dict]) -> list[dict]:
    """Issues in dependency order (deps before dependents). The spec is a validated DAG, so a
    stable Kahn sort terminates; it preserves spec order among independent issues."""
    by_handle = {i["handle"]: i for i in issues}
    indegree = {i["handle"]: len(i.get("deps") or []) for i in issues}
    ready = [i for i in issues if indegree[i["handle"]] == 0]
    out: list[dict] = []
    while ready:
        cur = ready.pop(0)
        out.append(cur)
        for issue in issues:  # any issue depending on cur loses an in-edge
            if cur["handle"] in (issue.get("deps") or []):
                indegree[issue["handle"]] -= 1
                if indegree[issue["handle"]] == 0:
                    ready.append(by_handle[issue["handle"]])
    return out


def _roots(issues: list[dict]) -> list[dict]:
    """Issues with no deps — the molecule's kickoff-gated entry points."""
    return [i for i in issues if not (i.get("deps") or [])]


def _opt(flag: str, value) -> list[str]:
    """`[flag, str(value)]` when value is set, else [] — for optional `bd create` flags."""
    return [flag, str(value)] if value not in (None, "") else []


def _abort(msg: str):
    typer.echo(f"✗ {msg}", err=True)
    raise typer.Exit(1)


def _state_val(bead: str, dim: str, cwd) -> str:
    """Current value of a state dimension via `bd state <bead> <dim>` ('' if unset)."""
    res = _bd(["state", bead, dim], cwd, capture=True)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


# ---- create steps (the only mutating surface; all via `_bd`) ------------------


def _create_one(args: list[str], cwd, actor: str) -> str:
    """`bd create … --silent` (id-only output); return the new id or raise PlanError."""
    res = _bd(["create", *args, "--silent"], cwd, actor=actor, capture=True)
    new_id = (res.stdout or "").strip().splitlines()[-1].strip() if res.stdout else ""
    if res.returncode != 0 or not new_id:
        raise PlanError(f"bd create failed ({(res.stderr or '').strip() or 'no id returned'})")
    return new_id


def _epic_import_labels(epic: dict, cwd) -> list[str]:
    """The epic's labels as INDIVIDUAL entries for a `bd import` record (import takes an array of
    literal labels; the comma-joined `-l` form would land as one bogus label)."""
    label_args = _issue_labels(epic, cwd)  # ["-l", "a,b,c"] or []
    return [lbl for lbl in label_args[1].split(",") if lbl] if label_args else []


def _bd_import(record: dict, cwd, actor: str) -> str:
    """Birth one bead from a JSONL `record` via `bd import - --json`; return the created id.

    The sanctioned birth path for a bead that must carry a native `source_system` (settable only
    at creation — no `bd create`/`update` flag exists). NOT the guarded `bd github push/sync`."""
    res = _bd(
        ["import", "-", "--json"], cwd, actor=actor, capture=True, text_input=json.dumps(record)
    )
    data = json.loads(res.stdout or "null") if res.returncode == 0 and res.stdout else None
    ids = data.get("ids") if isinstance(data, dict) else None
    if res.returncode != 0 or not ids:
        raise PlanError(f"bd import failed ({(res.stderr or '').strip() or 'no id returned'})")
    return str(ids[0])


def _create_epic(epic: dict, cwd, actor: str) -> str:
    """Create the molecule epic. An adopted epic carrying native `source_system` provenance is
    BORN via `bd import` (the only way to set `source_system`); otherwise it is `bd create`-d,
    carrying `--external-ref` when an adopted report supplied one."""
    source_system, external_ref = adopt.provenance_of(epic)
    if source_system:
        record = adopt.epic_import_record(epic, _epic_import_labels(epic, cwd))
        return _bd_import(record, cwd, actor)
    args = [
        str(epic["title"]),
        "--type=epic",
        *_opt("-d", epic.get("description")),
        *_opt("--design", epic.get("design")),
        *_opt("--external-ref", external_ref),
        *_issue_labels(epic, cwd),  # epic has no dimensions ⇒ just the identity triplet
    ]
    return _create_one(args, cwd, actor)


def _link_adopted_reports(epic_id: str, epic: dict, cwd, actor: str) -> list[str]:
    """Link each originating report as CHILD-OF the filed epic — `bd dep add <report> <epic>
    -t parent-child`, i.e. the report depends-on the epic. The epic OWNS the report; the report is
    NEVER a blocker of the epic, so it can't wrongly gate the molecule on an open report, and it
    rides the epic to completion. (A `blocks` edge is not usable — bd forbids blocking edges
    between an epic and a task — so parent-child is the sanctioned direction.) Returns the ids."""
    reports = adopt.adopts_of(epic)
    for report_id in reports:
        _bd(["dep", "add", report_id, epic_id, "-t", "parent-child"], cwd, actor=actor)
    return reports


def _create_issue(issue: dict, epic_id: str, dep_ids: list[str], cwd, actor: str) -> str:
    args = [
        str(issue["title"]),
        "--parent",
        epic_id,
        "--type",
        str(issue.get("type") or "task"),
        *_opt("-p", issue.get("priority")),
        *_opt("-d", issue.get("description")),
        *_opt("--acceptance", issue.get("acceptance")),
        *_opt("--design", issue.get("design")),
        *_issue_labels(issue, cwd),
        *(["--deps", ",".join(dep_ids)] if dep_ids else []),
    ]
    return _create_one(args, cwd, actor)


# ---- core (Typer-free; shared by the CLI verbs and the future MCP entrypoint) -


def check_spec(spec: str, cfg) -> list[str]:
    """Load + validate a molecule spec; return its problem list ([] ⇒ valid). Typer-free.

    Raises FileNotFoundError / molecule.MoleculeError on load failure (missing or malformed
    file). This is the standalone validation `check` exposes and `file` runs inline."""
    data = molecule.load_spec(spec)
    return molecule.validate_spec(data, cfg)


def file_molecule(data: dict, cwd: Path, actor: str) -> FileResult:
    """File a validated molecule spec into a beads swarm. Typer-free; raises PlanError.

    Creates the epic + child issues (deps + identity-triplet labels) in dependency order,
    builds the swarm, and opens the kickoff gate (a human gate per root + kickoff=pending).
    The caller is responsible for loading + validating `data` first (molecule.validate_or_raise)."""
    epic = data["epic"]
    issues = data["issues"]

    epic_id = _create_epic(epic, cwd, actor)
    handle_to_id: dict[str, str] = {}
    for issue in _topo_order(issues):
        dep_ids = [handle_to_id[h] for h in (issue.get("deps") or [])]
        handle_to_id[issue["handle"]] = _create_issue(issue, epic_id, dep_ids, cwd, actor)

    if _bd(["swarm", "create", epic_id], cwd, actor=actor).returncode != 0:
        raise PlanError(f"created epic {epic_id} but `bd swarm create` failed — inspect the rig")

    for root in _roots(issues):
        _bd(
            [
                "gate",
                "create",
                "--type=human",
                "--blocks",
                handle_to_id[root["handle"]],
                "--reason",
                f"kickoff {epic_id}",
            ],
            cwd,
            actor=actor,
        )
    _bd(
        ["set-state", epic_id, "kickoff=pending", "--reason", "awaiting kickoff approval"],
        cwd,
        actor=actor,
    )

    # Adopt path: link each originating report as child-of the epic (epic owns/blocks the report,
    # never the reverse). Provenance already rode onto the epic at creation (see _create_epic).
    adopted = _link_adopted_reports(epic_id, epic, cwd, actor)

    return FileResult(
        epic_id=epic_id,
        issue_count=len(issues),
        root_count=len(_roots(issues)),
        adopt_count=len(adopted),
    )


# ---- preview -----------------------------------------------------------------


def _preview(epic: dict, issues: list[dict], cwd) -> None:
    """Render what `file` would create — epic, each issue (labels + deps), and the kickoff gate.
    Pure echo; makes NO bd calls, so `--dry-run` is guaranteed side-effect-free."""
    typer.echo(f"would file molecule into {cwd}:")
    typer.echo(f"  epic: {epic['title']}")
    for issue in _topo_order(issues):
        labels = _issue_labels(issue, cwd)
        label_str = labels[1] if labels else "(none)"
        deps = ",".join(issue.get("deps") or []) or "—"
        typer.echo(
            f"  - [{issue.get('type') or 'task'}] {issue['handle']}: {issue['title']}  "
            f"labels={label_str}  deps={deps}"
        )
    roots = [r["handle"] for r in _roots(issues)]
    typer.echo(f"  kickoff gate (type=human) blocking root(s): {', '.join(roots) or '—'}")
    typer.echo("  + bd swarm create <epic> and set-state kickoff=pending")


def _save_spec(data: dict, path: str) -> None:
    """Write the (normalized) spec to `path` for audit, via the molecule YAML round-tripper."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        molecule._yaml.dump(data, f)
    typer.echo(f"saved spec → {p}")


# ---- show helpers -----------------------------------------------------------

# Label fields to surface in molecule rendering (dimension labels, not identity triplet).
_SHOW_DIM_PREFIXES = tuple(f"{f}:" for f in _DIMENSION_FIELDS)


def _dim_labels_from_spec_issue(issue: dict) -> list[str]:
    """Dimension labels built from a spec issue's named fields (model/harness/component/size)."""
    return [f"{f}:{issue[f]}" for f in _DIMENSION_FIELDS if issue.get(f) not in (None, "")]


def _dim_labels_from_bead(labels: list[str]) -> list[str]:
    """Dimension labels filtered from a bead's labels list (drops org/provider/repo triplet)."""
    return [lbl for lbl in labels if lbl.startswith(_SHOW_DIM_PREFIXES)]


def _render_issue_card(
    handle: str,
    title: str,
    type_: str,
    dim_labels: list[str],
    deps: list[str],
    acceptance: str,
    is_root: bool,
    status: str = "",
) -> None:
    """Print one issue card: header line + optional indented label / dep / acceptance rows."""
    root_mark = "  [root]" if is_root else ""
    status_str = f"  ({status})" if status else ""
    typer.echo(f"  [{type_}] {handle}: {title}{root_mark}{status_str}")
    if dim_labels:
        typer.echo(f"    labels: {', '.join(dim_labels)}")
    if deps:
        typer.echo(f"    deps:   {', '.join(deps)}")
    if acceptance:
        typer.echo(f"    acceptance: {acceptance}")


def _render_from_spec(data: dict, path) -> None:
    """Print the molecule from a spec file: header, epic, issues in topo order, root set."""
    epic = data["epic"]
    issues = data["issues"]

    typer.echo(f"from spec: {path}")
    typer.echo(f"epic: {epic['title']}")
    if epic.get("description"):
        typer.echo(f"  {epic['description']}")
    _render_epic_provenance(epic)
    adopts = adopt.adopts_of(epic)
    if adopts:
        typer.echo(f"  adopts: {', '.join(adopts)}")
    typer.echo()

    root_handles = {r["handle"] for r in _roots(issues)}
    for issue in _topo_order(issues):
        handle = issue["handle"]
        _render_issue_card(
            handle=handle,
            title=issue["title"],
            type_=str(issue.get("type") or "task"),
            dim_labels=_dim_labels_from_spec_issue(issue),
            deps=list(issue.get("deps") or []),
            acceptance=str(issue.get("acceptance") or ""),
            is_root=handle in root_handles,
        )
    typer.echo()
    typer.echo(f"roots: {', '.join(r['handle'] for r in _roots(issues)) or '—'}")


def _origin_report_card(child: dict) -> dict:
    """A render-ready dict for an originating (adopted) report child: id/title/status + its resolved
    intake channel and native system-of-record provenance (source_system/external_ref)."""
    return {
        "id": child.get("id"),
        "title": child.get("title") or "",
        "status": child.get("status") or "",
        "channel": state.channel_of(child.get("labels"), child.get("source_system")) or "",
        "source_system": child.get("source_system") or "",
        "external_ref": child.get("external_ref") or "",
    }


def _epic_molecule(epic_id: str, cwd):
    """Load a FILED epic + its child issues from bd as molecule-shaped dicts.

    Returns (epic_data, issues, origin_reports) — each issue keyed handle(=bead id)/title/type/
    labels/deps (sibling 'blocks' edges)/acceptance/status; origin_reports are the adopted
    originating reports linked child-of the epic, held OUT of the work-sibling set (they carry no
    acceptance and demand no kickoff gate). None if the epic or its children can't be retrieved.
    Shared by `show` (render) and `verify` (validate) so the load logic lives once.
    """
    epic_raw = _bd_json(["show", epic_id], cwd)
    if not isinstance(epic_raw, list) or not epic_raw:
        return None
    epic_data = epic_raw[0]

    # Load ALL children including closed/merged (`--all`), not just the open set. Once a
    # predecessor bead merges (closes) it drops out of the default list; its `blocks` edge to the
    # successor would then vanish and the successor would look like a fresh, ungated root
    # mid-molecule (the verify_epic false-positive). Carrying the closed siblings lets us tell a
    # genuine root (no predecessor at all) from a *satisfied* one (predecessor merged).
    children = _bd_json(["list", "--parent", epic_id, "--all"], cwd)
    if not isinstance(children, list):
        return None

    # An adopted origin report is a child too, but it is a source link, NOT molecule work — pull it
    # out of the sibling set so verify never demands acceptance / a kickoff gate for it, and show
    # renders it in its own section.
    origin_reports = [
        _origin_report_card(c)
        for c in children
        if c.get("issue_type") not in ("epic", "gate") and adopt.is_origin_report(c.get("labels"))
    ]
    origin_ids = {c["id"] for c in origin_reports}

    def _is_sibling(c) -> bool:
        return c.get("issue_type") not in ("epic", "gate") and c["id"] not in origin_ids

    # Full sibling set (open + closed) and the closed/merged subset among them.
    sibling_ids = {c["id"] for c in children if _is_sibling(c)}
    closed_ids = {c["id"] for c in children if _is_sibling(c) and c.get("status") == "closed"}

    # Build molecule-like dicts (handle = bead id) for the LIVE issues only. Merged siblings have
    # left the active molecule, so validate_spec / show / label checks operate on live work — but
    # each live issue records whether its blocking predecessors are still open (`deps`) or have
    # merged away (`satisfied_deps`), so a satisfied root isn't mistaken for an ungated one.
    issues = []
    for child in children:
        if not _is_sibling(child) or child["id"] in closed_ids:
            continue
        cid = child["id"]
        blocks = [
            d["depends_on_id"]
            for d in (child.get("dependencies") or [])
            if d.get("type") == "blocks" and d.get("depends_on_id") in sibling_ids
        ]
        issues.append(
            {
                "handle": cid,
                "title": child.get("title") or "",
                "type": child.get("issue_type") or "task",
                "labels": child.get("labels") or [],
                "deps": [d for d in blocks if d not in closed_ids],
                "satisfied_deps": [d for d in blocks if d in closed_ids],
                "acceptance": child.get("acceptance_criteria") or "",
                "status": child.get("status") or "",
            }
        )
    return epic_data, issues, origin_reports


def _provenance_suffix(source_system: str, external_ref: str) -> str:
    """A `  [<source_system> · <external_ref>]` suffix for a provenance bead ('' if none)."""
    parts = [p for p in (source_system, external_ref) if p]
    return f"  [{' · '.join(parts)}]" if parts else ""


def _render_epic_provenance(epic_data: dict) -> None:
    """Show the epic's surviving system-of-record provenance (source_system / external_ref)."""
    suffix = _provenance_suffix(
        str(epic_data.get("source_system") or ""), str(epic_data.get("external_ref") or "")
    )
    if suffix:
        typer.echo(f"  provenance:{suffix}")


def _render_origin_reports(origin_reports: list[dict]) -> None:
    """Render the originating (adopted) report(s) linked to the epic — the round-trip that proves
    `ws plan adopt` preserved the source link. No-op when the epic was not adopted."""
    if not origin_reports:
        return
    typer.echo()
    typer.echo(f"originating reports ({len(origin_reports)}):")
    for report in origin_reports:
        channel = f" [{report['channel']}]" if report.get("channel") else ""
        status = f" ({report['status']})" if report.get("status") else ""
        suffix = _provenance_suffix(report.get("source_system", ""), report.get("external_ref", ""))
        typer.echo(f"  {report['id']}{channel}: {report['title']}{status}{suffix}")


def _render_from_epic(epic_id: str, cwd) -> None:
    """Print the molecule from a filed epic: query bd, then render like _render_from_spec."""
    loaded = _epic_molecule(epic_id, cwd)
    if loaded is None:
        _abort(f"could not retrieve epic {epic_id} or its children — does it exist in this rig?")
    epic_data, issues, origin_reports = loaded

    typer.echo(f"from beads (filed): {epic_id}")
    typer.echo(f"epic: {epic_data.get('title') or epic_id}")
    if epic_data.get("description"):
        typer.echo(f"  {epic_data['description']}")
    _render_epic_provenance(epic_data)
    typer.echo()

    if not issues:
        typer.echo("  (no child issues)")
        _render_origin_reports(origin_reports)
        return

    root_handles = {r["handle"] for r in _roots(issues)}
    for issue in _topo_order(issues):
        handle = issue["handle"]
        _render_issue_card(
            handle=handle,
            title=issue["title"],
            type_=issue["type"],
            dim_labels=_dim_labels_from_bead(issue["labels"]),
            deps=issue["deps"],
            acceptance=issue["acceptance"],
            is_root=handle in root_handles,
            status=issue["status"],
        )
    typer.echo()
    typer.echo(f"roots: {', '.join(r['handle'] for r in _roots(issues)) or '—'}")
    _render_origin_reports(origin_reports)


# ---- verify: filed-molecule convention gate (Typer-free, read-only) ---------


def _spec_from_filed(epic_data: dict, issues: list[dict]) -> dict:
    """Reconstruct a molecule spec dict from a filed epic so molecule.validate_spec can run its
    structural checks (epic + title, unique handles, per-issue title/acceptance, deps → real
    handles, acyclic DAG). Dimension/identity LABELS are verified separately by _check_child_labels.
    """
    return {
        "epic": {
            "title": epic_data.get("title") or "",
            "description": epic_data.get("description") or "",
        },
        "issues": [
            {
                "handle": i["handle"],
                "title": i["title"],
                "type": i["type"],
                "acceptance": i["acceptance"],
                "deps": i["deps"],
            }
            for i in issues
        ],
    }


def _check_epic_type(epic_data: dict, epic_id: str) -> list[str]:
    """The verified bead must actually be an epic."""
    if epic_data.get("issue_type") != "epic":
        return [f"{epic_id}: not an epic (issue_type={epic_data.get('issue_type') or 'unset'})"]
    return []


def _check_swarm(epic_id: str, cwd) -> list[str]:
    """A bd swarm must have been created over the epic (`bd swarm create <epic>`)."""
    data = _bd_json(["swarm", "list"], cwd)
    swarms = data.get("swarms") if isinstance(data, dict) else None
    if swarms is None:
        return [f"could not retrieve swarm list to verify a swarm for {epic_id}"]
    if not any(sw.get("epic_id") == epic_id for sw in swarms):
        return [f"no bd swarm for epic {epic_id} (expected `bd swarm create {epic_id}`)"]
    return []


def _check_kickoff_state(epic_id: str, cwd) -> list[str]:
    """The epic must carry a kickoff state (pending after file, approved after approve)."""
    if not _state_val(epic_id, "kickoff", cwd):
        return [f"kickoff state unset on {epic_id} (expected pending or approved)"]
    return []


def _check_kickoff_gates(epic_id: str, issues: list[dict], cwd) -> list[str]:
    """Every GENUINE root must have a kickoff gate. Gate descriptions carry both the blocked root
    id and the `kickoff <epic>` marker (see file_molecule), so match on that pair. Uses `--all` so
    an already-approved molecule (gates since resolved) still verifies as gated.

    A root whose blocking predecessors have all merged/closed (`satisfied_deps`) is a *satisfied*
    root, not a fresh entry point: its kickoff gate lived on the original root, which has since
    merged away. Demanding a new gate for it is the mid-molecule false-positive we guard against —
    so only genuine roots (no predecessor, open or merged) require a kickoff gate."""
    roots = [r for r in _roots(issues) if not (r.get("satisfied_deps") or [])]
    if not roots:
        return []
    gates = _bd_json(["gate", "list", "--all"], cwd)
    if not isinstance(gates, list):
        return [f"could not retrieve gate list to verify kickoff gates for {epic_id}"]
    marker = f"kickoff {epic_id}"
    kickoff_descs = [
        str(g.get("description") or "") for g in gates if marker in str(g.get("description") or "")
    ]
    problems: list[str] = []
    for root in roots:
        rid = root["handle"]
        if not any(rid in desc for desc in kickoff_descs):
            problems.append(f"root {rid}: no kickoff gate")
    return problems


def _check_child_labels(issues: list[dict], cfg) -> list[str]:
    """Each child carries the provider/org/repo identity triplet, and any CLOSED-dimension label
    it does carry holds an allowed value (reuse registry.closed_dimensions + validate._label_val).
    """
    closed = registry.closed_dimensions(cfg)
    problems: list[str] = []
    for issue in issues:
        cid = issue["handle"]
        labels = issue.get("labels") or []
        for field in ("provider", "org", "repo"):
            if not validate._label_val(labels, f"{field}:"):
                problems.append(f"{cid}: missing identity label '{field}:'")
        for dim, allowed in closed.items():
            val = validate._label_val(labels, f"{dim}:")
            if val and val not in allowed:
                problems.append(
                    f"{cid}: {dim} '{val}' not in closed set {{{', '.join(sorted(allowed))}}}"
                )
    return problems


def verify_epic(epic_id: str, cfg, cwd) -> list[str]:
    """Return convention problems for a FILED molecule ([] ⇒ well-formed). Typer-free, read-only.

    Layers molecule.validate_spec (structural: epic + title, handles, acceptance, deps, acyclic DAG)
    over filed-bead assertions with no other home: the bead is an epic, a bd swarm exists, every
    root has a kickoff gate, kickoff state is set, and every child carries the identity triplet +
    valid closed-dimension labels.
    """
    loaded = _epic_molecule(epic_id, cwd)
    if loaded is None:
        return [f"could not retrieve epic {epic_id} or its children — does it exist in this rig?"]
    epic_data, issues, _origin_reports = loaded
    problems: list[str] = []
    problems += molecule.validate_spec(_spec_from_filed(epic_data, issues), cfg)
    problems += _check_epic_type(epic_data, epic_id)
    problems += _check_swarm(epic_id, cwd)
    problems += _check_kickoff_gates(epic_id, issues, cwd)
    problems += _check_kickoff_state(epic_id, cwd)
    problems += _check_child_labels(issues, cfg)
    return problems


def enforce_epic_conventions(epic_id: str, cfg, cwd, *, action: str) -> None:
    """Gate a state transition on the molecule conventions (reuse `verify_epic`): print the
    validator's SPECIFIC problem list and refuse, so a malformed molecule can't be finalized /
    dispatched behind a cryptic error or a silent main fork. `WS_DEBUG` downgrades the gate to a
    warning so a human can force through. `action` tails the messages (e.g. 'approve', 'dispatch').
    """
    problems = verify_epic(epic_id, cfg, cwd)
    if not problems:
        return
    for problem in problems:
        typer.echo(f"  - {problem}", err=True)
    if os.environ.get("WS_DEBUG"):
        typer.echo(
            f"⚠ WS_DEBUG override: {action} {epic_id} despite "
            f"{len(problems)} molecule convention problem(s)",
            err=True,
        )
        return
    _abort(
        f"{epic_id} fails molecule conventions — {action} refused; "
        f"fix the problems above (or set WS_DEBUG=1 to override)"
    )


# ---- verbs ------------------------------------------------------------------


@app.command("file")
@otel.trace_verb("plan.file")
def file(
    spec: str = typer.Argument(..., metavar="<spec>", help="molecule spec YAML"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview only; create nothing"),
    save: str = typer.Option("", "--save", help="write the normalized spec here for audit"),
    rig: str = _RIG,
):
    """Compile a molecule spec into a beads swarm: validate, then (unless --dry-run) create the
    epic + child issues (deps + labels, identity triplet injected) in dependency order, build the
    swarm, and open the kickoff gate (`bd gate` blocking each root + `kickoff=pending`)."""
    cfg = config.load()
    cwd = _rig_dir(cfg, rig)
    try:
        data = molecule.load_spec(spec)
        molecule.validate_or_raise(data, cfg)
    except (FileNotFoundError, molecule.MoleculeError) as e:
        _abort(str(e))

    epic = data["epic"]
    issues = data["issues"]

    if dry_run:
        _preview(epic, issues, cwd)
        if save:
            _save_spec(data, save)
        return

    actor = resolve_actor("", "", cwd=cwd)
    try:
        result = file_molecule(data, cwd, actor)
    except PlanError as e:
        _abort(str(e))

    adopt_note = (
        f", {result.adopt_count} originating report(s) linked" if result.adopt_count else ""
    )
    typer.echo(
        f"✓ filed {result.epic_id}: {result.issue_count} issue(s), "
        f"{result.root_count} kickoff gate(s), kickoff=pending{adopt_note}"
    )
    if save:
        _save_spec(data, save)


@app.command("adopt")
@otel.trace_verb("plan.adopt")
def adopt_cmd(
    beads: list[str] = _ADOPT_BEADS,
    out: str = typer.Option(
        "", "--out", "-o", help="write the seed frame spec here (default: stdout)"
    ),
    rig: str = _RIG,
):
    """Seed a plan FRAME from one or more PROMOTED intake reports (any channel — report/github/
    import). The report text seeds the epic; the originating report id(s) and any native
    provenance (source_system/external_ref) are recorded on the frame so `ws plan file` links each
    report as child-of the filed epic (epic owns the report — it never blocks the epic) and carries
    provenance onto the epic. The planner then decomposes the frame into issues before filing.

    Only beads handed over by triage `promote` (`intake:promoted`, state.is_promoted) are adoptable.
    """
    cfg = config.load()
    cwd = _rig_dir(cfg, rig)

    loaded: list[dict] = []
    for bead_id in beads:
        data = _bd_json(["show", bead_id], cwd)
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            _abort(f"could not read intake bead {bead_id} in this rig")
        if not state.is_promoted(data.get("labels")):
            _abort(
                f"{bead_id} is not promoted (intake:promoted) — only reports handed over by triage "
                f"`ws work promote {bead_id}` can be adopted"
            )
        loaded.append(data)

    try:
        frame = adopt.frame_from_beads(loaded)
    except adopt.AdoptError as e:
        _abort(str(e))

    if out:
        _save_spec(frame, out)
        typer.echo(
            f"✓ seeded frame from {len(loaded)} report(s) → decompose into issues, then "
            f"`ws plan file {out}`"
        )
    else:
        molecule._yaml.dump(frame, sys.stdout)


@app.command("check")
@otel.trace_verb("plan.check")
def check(
    spec: str = typer.Argument(..., metavar="<spec>", help="molecule spec YAML"),
    rig: str = _RIG,
):
    """Validate a molecule spec without filing it.

    Prints '✓ valid' on success (exit 0), or each validation problem (exit non-zero).
    This is the standalone surface of the same validation that `file` runs inline.
    """
    cfg = config.load()
    try:
        problems = check_spec(spec, cfg)
    except (FileNotFoundError, molecule.MoleculeError) as e:
        _abort(str(e))

    if problems:
        for problem in problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(1)

    typer.echo("✓ valid")


@app.command("verify")
@otel.trace_verb("plan.verify")
def verify(
    epic: str = typer.Argument(..., metavar="<epic>", help="filed epic id to verify"),
    rig: str = _RIG,
):
    """Verify a FILED molecule against the planning-plane conventions — the check gate a planner
    must pass before a molecule is considered done. Read-only: no bead is mutated.

    Prints '✓ verified' on success (exit 0); otherwise lists each specific problem (exit 1).
    Layers molecule.validate_spec (structural) over filed-bead assertions: the bead is an epic,
    a bd swarm exists, each root has a kickoff gate, kickoff state is set, and every child carries
    the identity triplet + valid closed-dimension labels.
    """
    cfg = config.load()
    cwd = _rig_dir(cfg, rig)
    problems = verify_epic(epic, cfg, cwd)
    if problems:
        for problem in problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(1)
    typer.echo(f"✓ verified {epic}: molecule conventions satisfied")


@app.command("approve")
@otel.trace_verb("plan.approve")
def approve(
    epic: str = typer.Argument(..., metavar="<epic>", help="epic id whose kickoff to approve"),
    rig: str = _RIG,
):
    """Resolve the open kickoff gates blocking this epic's root issues and set kickoff=approved.

    Refuses if the epic is not kickoff=pending or has no open kickoff gates.
    After approve the molecule roots become visible in `bd ready`. Pure planning-plane: it does
    NOT create the container branch `wt/bead/epic/<epic>` — the integration plane opens that (via
    worktree.ensure, kind="epic") when the epic is started / its first child is provisioned (see
    work.start / work.assign / work.claim → _maybe_open_molecule).
    """
    cfg = config.load()
    cwd = _rig_dir(cfg, rig)
    actor = resolve_actor("", "", cwd=cwd)

    # Guard: kickoff must be pending
    current = _state_val(epic, "kickoff", cwd)
    if current != "pending":
        _abort(f"epic {epic} kickoff={current or '(unset)'} — approve requires kickoff=pending")

    # Discover open kickoff gates for this epic
    gates = _bd_json(["gate", "list"], cwd)
    if not isinstance(gates, list):
        _abort(f"could not retrieve gate list for {epic}")

    marker = f"kickoff {epic}"
    open_gates = [
        g for g in gates if g.get("status") == "open" and marker in str(g.get("description") or "")
    ]

    if not open_gates:
        _abort(f"no open kickoff gates found for epic {epic} — nothing to approve")

    # Convention gate: don't finalize a malformed molecule — surface the validator's problem list
    # (reuse `verify_epic`) instead of approving a swarm that a coordinator can't cleanly dispatch.
    enforce_epic_conventions(epic, cfg, cwd, action="approve")

    # Resolve each gate
    for gate in open_gates:
        gate_id = str(gate.get("id") or gate.get("key") or "")
        if not gate_id:
            _abort(f"gate missing id field: {gate}")
        _bd(["gate", "resolve", gate_id], cwd, actor=actor)

    # Flip state to approved
    _bd(
        ["set-state", epic, "kickoff=approved", "--reason", "kickoff approved"],
        cwd,
        actor=actor,
    )

    typer.echo(f"✓ approved {epic}: {len(open_gates)} gate(s) resolved, kickoff=approved")


@app.command("show")
@otel.trace_verb("plan.show")
def show(
    ref: str = typer.Argument(..., metavar="<ref>", help="spec file path OR filed epic id"),
    rig: str = _RIG,
):
    """Render a molecule for human review — from a spec file or a filed epic.

    If <ref> is a path to a spec file → loads via molecule.load_spec and renders the
    pre-file view (what `file` would create).  If <ref> is a filed epic id → queries bd
    and renders the post-file view (round-trip verify: what landed == intent).

    Reuses _roots / _topo_order for ordering.  Output header distinguishes the source.
    """
    cfg = config.load()
    cwd = _rig_dir(cfg, rig)

    ref_path = Path(ref).expanduser()
    if ref_path.exists():
        try:
            data = molecule.load_spec(str(ref_path))
        except (FileNotFoundError, molecule.MoleculeError) as e:
            _abort(str(e))
        _render_from_spec(data, ref_path)
    else:
        _render_from_epic(ref, cwd)


@app.command("status")
@otel.trace_verb("plan.status")
def status(
    epic: str | None = typer.Argument(
        None, metavar="[<epic>]", help="epic id (omit for all swarms)"
    ),
    rig: str = _RIG,
):
    """List planning-plane molecules with progress + kickoff state.

    With no argument: lists all swarms (`bd swarm list`) with progress and a kickoff
    column (pending/approved/—). With an <epic> argument: shows that swarm's detail
    (`bd swarm status <epic>`) plus its kickoff state.
    """
    cfg = config.load()
    cwd = _rig_dir(cfg, rig)

    if not epic:
        data = _bd_json(["swarm", "list"], cwd)
        if data is None or not isinstance(data, dict):
            _abort("could not retrieve swarm list")
        swarms = data.get("swarms") or []
        if not swarms:
            typer.echo("no swarms found")
            return
        for sw in swarms:
            eid = sw.get("epic_id", "")
            title = sw.get("epic_title", "")
            completed = sw.get("completed_issues", 0)
            total = sw.get("total_issues", 0)
            kickoff = _state_val(eid, "kickoff", cwd) or "—"
            typer.echo(f"  {eid}  {title}  {completed}/{total}  kickoff={kickoff}")
    else:
        detail = _bd_json(["swarm", "status", epic], cwd)
        if detail is None:
            _abort(f"could not retrieve swarm status for {epic}")
        kickoff = _state_val(epic, "kickoff", cwd) or "—"
        title = detail.get("epic_title", "")
        completed = len(detail.get("completed") or [])
        total = detail.get("total_issues", 0)
        typer.echo(f"swarm: {epic}  {title}")
        typer.echo(f"  kickoff: {kickoff}")
        typer.echo(f"  progress: {completed}/{total} ({detail.get('progress_percent', 0)}%)")
        active = detail.get("active") or []
        ready = detail.get("ready") or []
        blocked = detail.get("blocked") or []
        if active:
            typer.echo(f"  active ({len(active)}): {', '.join(i.get('id', '') for i in active)}")
        if ready:
            typer.echo(f"  ready ({len(ready)}): {', '.join(i.get('id', '') for i in ready)}")
        if blocked:
            typer.echo(f"  blocked ({len(blocked)}): {', '.join(i.get('id', '') for i in blocked)}")
