"""Render helpers + the PR-style review packet for `ws work show` / `ws work review`.

Pure presentation over the rows `worktree.commit_rows` produces (plus the diff stream and the
review-state/molecule-intent text). Split out of `work.py` so the lifecycle verbs (which own the
bd seam) sit in a different file from the rendering. The bd lookups the review packet needs are
reached through the `work` module at call time (`bd.show` / `work._print_brief` etc.), so this
module never imports
`work` at load time — `work.py` re-exports these names and the cycle stays one-directional.
"""

from __future__ import annotations

import json

import typer

from . import bd, config, work_logic, worktree
from .work_logic import flag_rows

# ---- core payload (command + resource share the same producer) ---------------


def show_payload(cfg, entry, bead: str, branch: str, main) -> dict:
    """Core payload for ``ws work show --json`` and ``beadhive://work/show/{id}``.

    Returns ``{base, max_commits, commits, gates}`` — the base commit SHA (7-char
    abbreviated), the configured commit limit, the flagged commit rows for ``base..branch``
    of the named bead, and every gate touching the bead (``work_logic.gate_rows``: id, kind,
    open/resolved status, reason snippet — open first).  Computed from the already-pure
    producers ``worktree.commit_rows`` + ``work_logic.flag_rows``; no Typer / no side
    effects.  Returns an empty commits list and an empty base string when the branch or
    integration base cannot be resolved.
    """
    integration = worktree.integration_base(entry, bead, config.integration_branch(cfg, entry))
    base = worktree.base_of(entry, branch, integration)
    rows = flag_rows(worktree.commit_rows(entry, base, branch)) if base else []
    return {
        "base": base[:7] if base else "",
        "max_commits": config.max_commits(cfg, entry),
        "commits": rows,
        "gates": work_logic.gate_rows(bead, main),
    }

# Typer option specs for the read-only render verbs (mirrors the lifecycle verbs' specs in
# work.py; kept local so the verbs live wholly in this module without an import cycle).
_HIVE = typer.Option("", "--hive", help="target hive (default: cwd's hive)")
_BEAD = typer.Argument(..., metavar="<id>", help="bead id")
_VIEW = typer.Option(["log"], "--view", help="log|sig|diff|stat (repeatable)")
_JSONOUT = typer.Option(False, "--json", help="machine rows + flags (refine input)")

_SIG_GLYPH = {"G": "✔", "U": "~", "B": "✗", "N": "·"}  # mirror tests/harness/render.py


def _row_notes(flags: dict) -> str:
    notes = []
    if flags["marker"]:
        notes.append("marker")
    if flags["fixup"]:
        notes.append(f"fixup→{flags['fixup']}")
    if flags["run"]:
        notes.append("run")
    return ("   " + " ".join(notes)) if notes else ""


def _render_log(rows, base, max_commits):
    flagged = sum(1 for r in rows if any(r["flags"].values()))
    typer.echo(f"{len(rows)} commits (base {base[:7]}), {flagged} flagged, max {max_commits}")
    for r in rows:
        typer.echo(
            f"{r['short']}  {r['date'][:10]}  {r['author']}  "
            f"{r['subject']}  ({len(r['files'])}f){_row_notes(r['flags'])}"
        )


def _render_sig(rows):
    for r in rows:
        glyph = _SIG_GLYPH.get(r["sig"], "?")
        signed = r["sig"] in ("G", "U") and r["signer"]
        who = f"{glyph}{r['signer']}" if signed else f"{glyph}unsigned"
        typer.echo(f"{r['short']}  {r['author']} <{r['email']}>  {who}  {r['subject']}")


def _render_stat(rows):
    from collections import Counter

    c = Counter(f for r in rows for f in r["files"])
    for fname, n in c.most_common():
        typer.echo(f"{n:>3}  {fname}")
    typer.echo(f"— {sum(c.values())} file-touches across {len(rows)} commits")


_GATE_GLYPH = {"open": "○", "resolved": "✓"}


def _render_gates(bead, main):
    """Compact gates section (bh-i371): EVERY gate touching the bead — kind (kickoff / review /
    security / ad-hoc), status, reason snippet, gate id — open ones first, resolved marked ✓ so
    the gate history stays visible. Silent when no gates touch the bead."""
    rows = work_logic.gate_rows(bead, main)
    if not rows:
        return
    n_open = sum(1 for r in rows if r["status"] == "open")
    typer.echo(f"gates: {len(rows)} ({n_open} open)")
    for r in rows:
        glyph = _GATE_GLYPH.get(r["status"], "?")
        typer.echo(f"  {glyph} {r['kind']} gate {r['id']}: {r['reason']}")


def _render_view(v, rows, base, max_commits, entry, branch):
    if v == "log":
        _render_log(rows, base, max_commits)
    elif v == "sig":
        _render_sig(rows)
    elif v == "diff":
        worktree.diff_range(entry, base, branch)
    elif v == "stat":
        _render_stat(rows)
    else:
        typer.echo(f"✗ unknown view: {v} (log|sig|diff|stat)", err=True)


# ---- review (PR-style walkthrough packet for the merger/reviewer) -----------


def _print_review_state(bead, main):
    state = bd.state(bead, "review", main) or "(none)"
    open_review, _resolved = work_logic.review_gates(bead, main)
    gate = "open (not approved)" if open_review else "resolved/none"
    typer.echo(f"\n## Review state\n  review={state}  gate={gate}")
    _render_gates(bead, main)  # every gate touching the bead, not just the review one (bh-i371)


def _review_molecule_intent(cfg, entry, epic, main):
    """Epic brief + each child's acceptance criteria — the intent a molecule land is judged by."""
    from . import work  # lazy: bd seam lives in work.py; avoids an import cycle

    work._print_brief(cfg, entry, epic, bd.show(epic, main))
    # --all so landed (closed) children show too — the reviewer judges the molecule against every
    # child's acceptance, not just the ones still in flight.
    children = bd.json(["list", "--parent", epic, "--all"], main)
    if not isinstance(children, list):
        typer.echo("\n⚠ could not list molecule children", err=True)
        return
    kids = [c for c in children if str(c.get("issue_type", "")) not in ("epic", "gate")]
    typer.echo(f"\n## Molecule children ({len(kids)})")
    for c in kids:
        acc = work._first(c, "acceptance_criteria", "acceptance") or "(no acceptance criteria)"
        typer.echo(f"\n- {c.get('id')} · {c.get('status')} · {c.get('title', '')}\n    {acc}")


# ---- read-only render verbs (registered onto work.app from work.py) ---------


def show(
    bead: str = _BEAD,
    view: list[str] = _VIEW,
    json_out: bool = _JSONOUT,
    hive: str = _HIVE,
):
    """Render a bead branch's local history (base..branch) from several perspectives — plus a
    gates section listing every gate touching the bead — so an agent can judge how noisy it is
    before submit/merge. Read-only; never mutates; always exits 0."""
    cfg = config.load()
    entry, main, _target, branch = worktree.locate(cfg, hive, bead)
    if json_out:
        typer.echo(json.dumps(show_payload(cfg, entry, bead, branch, main)))
        return
    integration = worktree.integration_base(entry, bead, config.integration_branch(cfg, entry))
    base = worktree.base_of(entry, branch, integration)
    rows = flag_rows(worktree.commit_rows(entry, base, branch)) if base else []
    if not base:
        typer.echo(f"✗ cannot compare {branch} against {integration} (present locally?)", err=True)
    elif not rows:
        typer.echo(f"no commits over {base[:7]}")
    else:
        for v in view:
            _render_view(v, rows, base, config.max_commits(cfg, entry), entry, branch)
    _render_gates(bead, main)  # gates exist independent of local history — render either way


def review(
    bead: str = _BEAD,
    run_validate: bool = typer.Option(False, "--run", help="run validate_cmd from clean checkout"),
    demo: bool = typer.Option(False, "--demo", help="run demo_cmd from a clean checkout"),
    fresh: bool = typer.Option(
        True,
        "--fresh/--no-fresh",
        help="run validation fresh (default — reviewers expect a real run); --no-fresh may "
        "reuse a recorded green verdict for this exact sha + command (bh-dfx0)",
    ),
    view: list[str] = _VIEW,
    hive: str = _HIVE,
):
    """Assemble a PR-style review packet for an approved branch: intent (epic/bead brief + child
    acceptance + review state), the change (commits/diff/stat against the integration target), and
    optionally validation + feature-demo output run from a pristine checkout. Read-only re: bd/git
    state. Molecule-aware: an epic `<id>` with a `wt/bead/epic/<id>` container branch reviews the
    whole molecule against its integration target; otherwise it reviews the leaf bead branch
    `wt/bead/issue/<id>`. Validation defaults to a FRESH clean-checkout run; `--no-fresh` is the
    explicit opt-in to reuse a recorded green verdict from the validation ledger (bh-dfx0). The
    demo always runs fresh — its output is the point."""
    from . import work  # lazy: bd seam (_print_brief / _show) lives in work.py; avoids a cycle

    cfg = config.load()
    entry, main, _target, bead_branch = worktree.locate(cfg, hive, bead)
    mol_branch = f"{worktree._BEAD_PREFIX}epic/{bead}"
    integration = worktree.integration_base(entry, bead, config.integration_branch(cfg, entry))
    if worktree._branch_exists(main, mol_branch):
        branch = mol_branch
        _review_molecule_intent(cfg, entry, bead, main)
    elif worktree._branch_exists(main, bead_branch):
        branch = bead_branch
        work._print_brief(cfg, entry, bead, bd.show(bead, main))
    else:
        typer.echo(f"✗ no {mol_branch} or {bead_branch} branch — nothing to review", err=True)
        return
    _print_review_state(bead, main)

    # change packet
    base = worktree.base_of(entry, branch, integration)
    if not base:
        typer.echo(f"\n✗ cannot compare {branch} against {integration} (present?)", err=True)
    else:
        rows = flag_rows(worktree.commit_rows(entry, base, branch))
        if not rows:
            typer.echo(f"\nno commits over {base[:7]}")
        else:
            typer.echo(f"\n## Change ({branch} vs {integration})")
            for v in view:
                _render_view(v, rows, base, config.max_commits(cfg, entry), entry, branch)

    # execution (pristine checkout — never depends on dirty local state)
    if run_validate:
        cmd = config.validate_cmd(cfg, entry)
        typer.echo(f"\n## Validation ({cmd})")
        rc = worktree.clean_checkout(entry, branch, cmd, reuse=not fresh)
        typer.echo(f"— validate exit {rc}")
    if demo:
        cmd = config.demo_cmd(cfg, entry)
        if cmd:
            typer.echo(f"\n## Demo ({cmd})")
            typer.echo(f"— demo exit {worktree.clean_checkout(entry, branch, cmd)}")
        else:
            typer.echo("\n## Demo\n  no demo_cmd configured (set work.demo_cmd)")
