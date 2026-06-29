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
from pathlib import Path

import typer

from . import config, molecule, registry
from .identity import resolve_actor, workspace_identity
from .run import run

app = typer.Typer(no_args_is_help=True, help="Plan a molecule → swarm (planning plane).")

# ---- shared plumbing ---------------------------------------------------------

_RIG = typer.Option("", "--rig", "-r", help="target rig (default: cwd's rig)")

# Molecule integration-branch prefix — matches worktree.MOL_PREFIX (lands from sibling bead
#; defined locally here so approve does not depend on that merge order).
_MOL_PREFIX = "mol/"

# Issue fields that map to a label dimension (`<field>:<value>`), filed alongside the
# auto-injected provider/org/repo identity triplet. Mirrors molecule._DIMENSION_FIELDS.
_DIMENSION_FIELDS = ("model", "harness", "component", "size")

# --- `bd create --graph <json>` spike (bd 1.0.5) -----------------------------
# Tried a single atomic call: `{"nodes": [{key,title,type,priority,description,labels,
# parent_key,parent_id}], "edges": [{from_key,to_key,type}]}`. It DOES create an epic +
# children + parent links + dependency edges + labels in one shot — but it **silently drops
# `acceptance`/`design`** (warns: "unknown field(s)"). Acceptance is the molecule's required
# accuracy field, so --graph would lose it. It also bypasses the triplet-injection wrapper.
# Decision: file per-issue (`bd create` carries --acceptance/--design/--deps/-l), in
# dependency (topological) order so each `--deps` references an already-created real id.


def _bd(args, cwd, actor="", capture=False):
    """Run a `bd` subcommand scoped to the rig via `-C <cwd>` (so the right Beads DB is hit
    regardless of the process cwd / `--rig`). Prepends `--actor <name>` for the audit trail."""
    cmd = ["bd", "-C", str(cwd)]
    if actor:
        cmd += ["--actor", actor]
    cmd += list(args)
    return run(cmd, check=False, capture=capture)


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


def _rig_entry(cfg, rig: str):
    """The managed_repos entry for rig_id, or None when rig is unset (cwd fallback)."""
    if rig:
        return registry.resolve_rig(cfg, rig)
    return None


def _ensure_mol_branch(cfg, entry, epic_id: str, main: Path) -> None:
    """Create mol/<epic> off the integration branch in the rig's main clone, idempotently.

    If the branch already exists, emits a note and returns without error. On creation
    failure, aborts with a descriptive message so the caller never silently loses state.
    """
    branch = _MOL_PREFIX + epic_id
    integration = config.integration_branch(cfg, entry)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # Idempotency check: skip creation if the branch is already present.
    already_exists = (
        run(
            ["git", "-C", str(main), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
            capture=True,
            env=env,
        ).returncode
        == 0
    )
    if already_exists:
        typer.echo(f"  mol branch already exists: {branch} (skipped)")
        return
    res = run(
        ["git", "-C", str(main), "branch", branch, integration],
        check=False,
        capture=True,
        env=env,
    )
    if res.returncode != 0:
        _abort(
            f"could not create {branch} off {integration}: {(res.stderr or '').strip()}"
        )
    typer.echo(f"✓ created mol branch: {branch} → {integration}")


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
    """`bd create … --silent` (id-only output); return the new id or abort on failure."""
    res = _bd(["create", *args, "--silent"], cwd, actor=actor, capture=True)
    new_id = (res.stdout or "").strip().splitlines()[-1].strip() if res.stdout else ""
    if res.returncode != 0 or not new_id:
        _abort(f"bd create failed ({(res.stderr or '').strip() or 'no id returned'})")
    return new_id


def _create_epic(epic: dict, cwd, actor: str) -> str:
    args = [
        str(epic["title"]),
        "--type=epic",
        *_opt("-d", epic.get("description")),
        *_opt("--design", epic.get("design")),
        *_issue_labels(epic, cwd),  # epic has no dimensions ⇒ just the identity triplet
    ]
    return _create_one(args, cwd, actor)


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


def _render_from_epic(epic_id: str, cwd) -> None:
    """Print the molecule from a filed epic: query bd, then render like _render_from_spec."""
    epic_raw = _bd_json(["show", epic_id], cwd)
    if not isinstance(epic_raw, list) or not epic_raw:
        _abort(f"could not retrieve epic {epic_id} — does it exist in this rig?")
    epic_data = epic_raw[0]

    children = _bd_json(["list", "--parent", epic_id], cwd)
    if not isinstance(children, list):
        _abort(f"could not retrieve children of {epic_id}")

    # Build molecule-like dicts (handle = bead id) and wire sibling "blocks" deps.
    child_ids = {c["id"] for c in children if c.get("issue_type") not in ("epic", "gate")}
    issues = []
    for child in children:
        if child.get("issue_type") in ("epic", "gate"):
            continue
        cid = child["id"]
        sibling_deps = [
            d["depends_on_id"]
            for d in (child.get("dependencies") or [])
            if d.get("type") == "blocks" and d.get("depends_on_id") in child_ids
        ]
        issues.append({
            "handle": cid,
            "title": child.get("title") or "",
            "type": child.get("issue_type") or "task",
            "labels": child.get("labels") or [],
            "deps": sibling_deps,
            "acceptance": child.get("acceptance_criteria") or "",
            "status": child.get("status") or "",
        })

    typer.echo(f"from beads (filed): {epic_id}")
    typer.echo(f"epic: {epic_data.get('title') or epic_id}")
    if epic_data.get("description"):
        typer.echo(f"  {epic_data['description']}")
    typer.echo()

    if not issues:
        typer.echo("  (no child issues)")
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


# ---- verbs ------------------------------------------------------------------


@app.command("file")
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
    epic_id = _create_epic(epic, cwd, actor)
    handle_to_id: dict[str, str] = {}
    for issue in _topo_order(issues):
        dep_ids = [handle_to_id[h] for h in (issue.get("deps") or [])]
        handle_to_id[issue["handle"]] = _create_issue(issue, epic_id, dep_ids, cwd, actor)

    if _bd(["swarm", "create", epic_id], cwd, actor=actor).returncode != 0:
        _abort(f"created epic {epic_id} but `bd swarm create` failed — inspect the rig")

    for root in _roots(issues):
        _bd(
            ["gate", "create", "--type=human", "--blocks", handle_to_id[root["handle"]],
             "--reason", f"kickoff {epic_id}"],
            cwd,
            actor=actor,
        )
    _bd(["set-state", epic_id, "kickoff=pending", "--reason", "awaiting kickoff approval"],
        cwd, actor=actor)

    typer.echo(
        f"✓ filed {epic_id}: {len(issues)} issue(s), "
        f"{len(_roots(issues))} kickoff gate(s), kickoff=pending"
    )
    if save:
        _save_spec(data, save)


@app.command("check")
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
        data = molecule.load_spec(spec)
    except (FileNotFoundError, molecule.MoleculeError) as e:
        _abort(str(e))

    problems = molecule.validate_spec(data, cfg)
    if problems:
        for problem in problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(1)

    typer.echo("✓ valid")


@app.command("approve")
def approve(
    epic: str = typer.Argument(..., metavar="<epic>", help="epic id whose kickoff to approve"),
    rig: str = _RIG,
):
    """Resolve the open kickoff gates blocking this epic's root issues and set kickoff=approved.

    Refuses if the epic is not kickoff=pending or has no open kickoff gates.
    After approve the molecule roots become visible in `bd ready`.
    """
    cfg = config.load()
    cwd = _rig_dir(cfg, rig)
    actor = resolve_actor("", "", cwd=cwd)

    # Guard: kickoff must be pending
    current = _state_val(epic, "kickoff", cwd)
    if current != "pending":
        _abort(
            f"epic {epic} kickoff={current or '(unset)'} — approve requires kickoff=pending"
        )

    # Discover open kickoff gates for this epic
    gates = _bd_json(["gate", "list"], cwd)
    if not isinstance(gates, list):
        _abort(f"could not retrieve gate list for {epic}")

    marker = f"kickoff {epic}"
    open_gates = [
        g for g in gates
        if g.get("status") == "open" and marker in str(g.get("description") or "")
    ]

    if not open_gates:
        _abort(f"no open kickoff gates found for epic {epic} — nothing to approve")

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

    # Create the molecule integration branch mol/<epic> off the integration branch.
    entry = _rig_entry(cfg, rig)
    _ensure_mol_branch(cfg, entry, epic, cwd)

    typer.echo(f"✓ approved {epic}: {len(open_gates)} gate(s) resolved, kickoff=approved")


@app.command("show")
def show(
    ref: str = typer.Argument(
        ..., metavar="<ref>", help="spec file path OR filed epic id"
    ),
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
        typer.echo(
            f"  progress: {completed}/{total} ({detail.get('progress_percent', 0)}%)"
        )
        active = detail.get("active") or []
        ready = detail.get("ready") or []
        blocked = detail.get("blocked") or []
        if active:
            typer.echo(f"  active ({len(active)}): {', '.join(i.get('id', '') for i in active)}")
        if ready:
            typer.echo(f"  ready ({len(ready)}): {', '.join(i.get('id', '') for i in ready)}")
        if blocked:
            typer.echo(f"  blocked ({len(blocked)}): {', '.join(i.get('id', '') for i in blocked)}")
