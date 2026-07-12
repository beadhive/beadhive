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

from . import bd, config, worktree
from .work_logic import flag_rows

# ---- core payload (command + resource share the same producer) ---------------


def show_payload(cfg, entry, bead: str, branch: str) -> dict:
    """Core payload for ``ws work show --json`` and ``beadhive://work/show/{id}``.

    Returns ``{base, max_commits, commits}`` — the base commit SHA (7-char abbreviated),
    the configured commit limit, and the flagged commit rows for ``base..branch`` of the
    named bead.  Computed from the already-pure producers ``worktree.commit_rows`` +
    ``work_logic.flag_rows``; no Typer / no side effects.  Returns an empty commits list
    and an empty base string when the branch or integration base cannot be resolved.
    """
    integration = worktree.integration_base(entry, bead, config.integration_branch(cfg, entry))
    base = worktree.base_of(entry, branch, integration)
    rows = flag_rows(worktree.commit_rows(entry, base, branch)) if base else []
    return {
        "base": base[:7] if base else "",
        "max_commits": config.max_commits(cfg, entry),
        "commits": rows,
    }

# Typer option specs for the read-only render verbs (mirrors the lifecycle verbs' specs in
# work.py; kept local so the verbs live wholly in this module without an import cycle).
_RIG = typer.Option("", "--rig", "-r", help="target rig (default: cwd's rig)")
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
    from . import work  # lazy: bd seam lives in work.py; avoids an import cycle

    state = bd.state(bead, "review", main) or "(none)"
    gate = "open (not approved)" if work._open_gate(bead, main) else "resolved/none"
    typer.echo(f"\n## Review state\n  review={state}  gate={gate}")


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
    rig: str = _RIG,
):
    """Render a bead branch's local history (base..branch) from several perspectives so an agent
    can judge how noisy it is before submit/merge. Read-only; never mutates; always exits 0."""
    cfg = config.load()
    entry, _main, _target, branch = worktree.locate(cfg, rig, bead)
    if json_out:
        typer.echo(json.dumps(show_payload(cfg, entry, bead, branch)))
        return
    integration = worktree.integration_base(entry, bead, config.integration_branch(cfg, entry))
    base = worktree.base_of(entry, branch, integration)
    rows = flag_rows(worktree.commit_rows(entry, base, branch)) if base else []
    if not base:
        typer.echo(f"✗ cannot compare {branch} against {integration} (present locally?)", err=True)
        return
    if not rows:
        typer.echo(f"no commits over {base[:7]}")
        return
    for v in view:
        _render_view(v, rows, base, config.max_commits(cfg, entry), entry, branch)


def review(
    bead: str = _BEAD,
    run_validate: bool = typer.Option(False, "--run", help="run validate_cmd from clean checkout"),
    demo: bool = typer.Option(False, "--demo", help="run demo_cmd from a clean checkout"),
    view: list[str] = _VIEW,
    rig: str = _RIG,
):
    """Assemble a PR-style review packet for an approved branch: intent (epic/bead brief + child
    acceptance + review state), the change (commits/diff/stat against the integration target), and
    optionally validation + feature-demo output run from a pristine checkout. Read-only re: bd/git
    state. Molecule-aware: an epic `<id>` with a `wt/bead/epic/<id>` container branch reviews the
    whole molecule against its integration target; otherwise it reviews the leaf bead branch
    `wt/bead/issue/<id>`."""
    from . import work  # lazy: bd seam (_print_brief / _show) lives in work.py; avoids a cycle

    cfg = config.load()
    entry, main, _target, bead_branch = worktree.locate(cfg, rig, bead)
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
        typer.echo(f"— validate exit {worktree.clean_checkout(entry, branch, cmd)}")
    if demo:
        cmd = config.demo_cmd(cfg, entry)
        if cmd:
            typer.echo(f"\n## Demo ({cmd})")
            typer.echo(f"— demo exit {worktree.clean_checkout(entry, branch, cmd)}")
        else:
            typer.echo("\n## Demo\n  no demo_cmd configured (set work.demo_cmd)")
