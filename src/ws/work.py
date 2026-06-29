"""`ws work` — the integration-plane driver.

Takes a single bead assigned → merged through the Agentic Git Flow lifecycle
(brief → claim → check → submit → resume → abandon, plus orchestrator-only assign),
so an agent drives the lifecycle through `ws` instead of improvising raw git. It is a
thin facade: each verb composes `bd` (Beads), `ws` managed worktrees, and per-agent
identity primitives that already exist. Raw git is for the change *inside* the worktree
only — never the lifecycle around it.

Test seam: this module shells out to **`bd` only** (via `_bd`); every git / worktree
operation goes through `worktree` / `identity`. Tests use a real git repo and fake just
`bd` by patching `ws.work.run`.
"""

from __future__ import annotations

import datetime
import json
import re
import shlex
import sys
from pathlib import Path

import typer

from . import config, identity, worktree
from .run import run

app = typer.Typer(no_args_is_help=True, help="Drive a bead assigned→merged (integration plane).")

# Conventional-commit subject — type(scope)!: summary. Used by the submit cleanliness guard.
_CONVENTIONAL = re.compile(
    r"^(feat|fix|refactor|docs|test|chore|perf|ci|build|style|revert)(\([^)]+\))?!?: .+"
)


# ---- bd plumbing (the only subprocess surface here) -------------------------


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


def _show(bead, cwd):
    """The bead's JSON object (bd show may return a single object or a 1-list)."""
    data = _bd_json(["show", bead], cwd)
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def _state(bead, dim, cwd):
    """Current value of a state dimension via `bd state` ('' if unset)."""
    res = _bd(["state", bead, dim], cwd, capture=True)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def _first(data, *keys):
    """First present, truthy value among keys (bd JSON field-name drift insurance)."""
    return next((data[k] for k in keys if data.get(k)), None)


def _open_gate(bead, cwd) -> bool:
    """True iff an open review gate still blocks `bead` — i.e. it isn't approved yet. The gate
    names the bead in its description (matches `bd gate create --blocks <bead>` at submit)."""
    gates = _bd_json(["gate", "list"], cwd)
    if not isinstance(gates, list):
        return False
    return any(
        g.get("status") == "open" and bead in str(g.get("description") or "") for g in gates
    )


# ---- guards & shared steps ---------------------------------------------------


def _guard_open(data, bead):
    if data is None:
        typer.echo(f"✗ no such bead: {bead}", err=True)
        raise typer.Exit(1)
    if str(data.get("status", "")) == "closed":
        typer.echo(f"✗ bead {bead} is closed", err=True)
        raise typer.Exit(1)


def _guard_not_other(data, actor, bead):
    """Refuse if assigned to a *different* actor — `bd --claim` would otherwise steal it."""
    cur = str(data.get("assignee") or "")
    if cur and cur != actor:
        typer.echo(f"✗ bead {bead} assigned to {cur} (not {actor}) — refusing to steal", err=True)
        raise typer.Exit(1)


def _stamp(cfg, entry, target, actor):
    """Stamp agent identity + signing into the worktree, unless supervised (inherit human)."""
    prof = config.work_identity(cfg, entry)
    if prof["mode"] == "supervised":
        return
    identity.stamp(
        target,
        name=actor or prof["name"] or "",
        email=prof["email"] or "",
        signing_key=prof["signing_key"] or "",
        sign=prof["sign"],
    )


def _print_brief(cfg, entry, bead, data):
    if not data:
        typer.echo(f"✗ no such bead: {bead}", err=True)
        raise typer.Exit(1)
    typer.echo(f"# {data.get('id', bead)}  {data.get('title', '')}")
    desc = _first(data, "description")
    if desc:
        typer.echo(f"\n## Requirements / goals\n{desc}")
    acc = _first(data, "acceptance_criteria", "acceptance")
    if acc:
        typer.echo(f"\n## Acceptance\n{acc}")
    design = _first(data, "design")
    if design:
        typer.echo(f"\n## Design\n{design}")
    typer.echo(f"\n## Validate with\n{config.validate_cmd(cfg, entry)}")


def _history_ok(count, subjects, limit):
    """(ok, message) for submit's 'small set of conventional digests' guard."""
    if count < 0:
        return False, "cannot compare against the integration branch (is it present locally?)"
    if count == 0:
        return False, "no commits over the integration branch — nothing to submit"
    if count > limit:
        return False, (
            f"{count} commits over base (> {limit}) — self-refine into a few conventional "
            "digests before submitting"
        )
    bad = [s for s in subjects if not _CONVENTIONAL.match(s)]
    if bad:
        return False, "non-conventional commit subjects:\n  " + "\n  ".join(bad)
    return True, ""


# ---- show / refine pure helpers (no git/bd — unit-tested) --------------------

# fixup!/squash! autosquash markers (git's own --autosquash trigger prefixes).
_MARKER = re.compile(r"^(fixup|squash)! ")


def _type_scope(subject: str) -> str | None:
    """The conventional `type(scope)` prefix of a subject (drops the `!` and everything from
    `:` on), or None if it isn't a conventional subject. Used to spot adjacent same-kind runs."""
    if not _CONVENTIONAL.match(subject):
        return None
    return subject.split(":", 1)[0].rstrip("!")


def flag_rows(rows: list[dict]) -> list[dict]:
    """Annotate each row with noise `flags` (signals, not decisions — no semantic grouping):
      marker  — subject is a fixup!/squash! commit;
      fixup   — short sha of the nearest EARLIER row whose files are a superset of this row's
                (non-empty) files (a likely fold target), else None;
      run     — this row shares a conventional type(scope) with the immediately previous row."""
    out: list[dict] = []
    for i, row in enumerate(rows):
        files = set(row.get("files") or [])
        fixup = None
        if files:
            for j in range(i - 1, -1, -1):
                earlier = set(rows[j].get("files") or [])
                if earlier and files <= earlier:
                    fixup = rows[j]["short"]
                    break
        run = bool(
            i > 0
            and (ts := _type_scope(row["subject"]))
            and _type_scope(rows[i - 1]["subject"]) == ts
        )
        flags = {"marker": bool(_MARKER.match(row["subject"])), "fixup": fixup, "run": run}
        out.append({**row, "flags": flags})
    return out


def _resolve_sha(rows: list[dict], h: str) -> str | None:
    """Map a short/long hash to the full sha of a row in range, or None if it isn't in range."""
    for r in rows:
        if h and (r["sha"] == h or r["short"] == h or r["sha"].startswith(h)):
            return r["sha"]
    return None


def validate_plan(plan: dict, rows: list[dict]) -> tuple[bool, list[str], list[dict]]:
    """(ok, errors, resolved_groups). Each resolved group uses full shas:
    {keep, fold:[...], subject, body, date}. Errors name the offending hashes; on any error the
    caller must refuse BEFORE creating a backup or touching git."""
    errors: list[str] = []
    resolved: list[dict] = []
    seen: dict[str, int] = {}  # full sha -> first group index that owns it
    for gi, g in enumerate(plan.get("groups") or []):
        keep_raw = g.get("keep")
        keep = _resolve_sha(rows, keep_raw) if keep_raw else None
        if not keep:
            errors.append(f"group {gi}: keep {keep_raw!r} is not a commit in range")
        folds: list[str] = []
        for fr in g.get("fold") or []:
            fs = _resolve_sha(rows, fr)
            if not fs:
                errors.append(f"group {gi}: fold {fr!r} is not a commit in range")
            else:
                folds.append(fs)
        if keep and keep in folds:
            errors.append(f"group {gi}: keep {keep_raw!r} also appears in its own fold")
        for sha in dict.fromkeys([keep, *folds]):  # unique within group
            if sha is None:
                continue
            if sha in seen:
                errors.append(f"commit {sha[:8]} appears in more than one group")
            else:
                seen[sha] = gi
        if keep:
            resolved.append(
                {"keep": keep, "fold": folds, "subject": g.get("subject"),
                 "body": g.get("body"), "date": g.get("date") or "keep"}
            )
    return (not errors, errors, resolved)


def plan_from_since(rows: list[dict]) -> dict:
    """`--since` sugar: fold everything after the first commit in `rows` (ref..tip) into it."""
    if not rows:
        return {"groups": []}
    return {"groups": [{"keep": rows[0]["sha"], "fold": [r["sha"] for r in rows[1:]]}]}


def auto_message(keep_row: dict, fold_rows: list[dict]) -> tuple[str, str]:
    """Mode-b digest message: subject = keep's subject; body = `- <folded subject>` bullets
    (fixup!/squash! prefixes stripped, empties dropped)."""
    bullets = [
        f"- {s}" for s in (_MARKER.sub("", r["subject"]).strip() for r in fold_rows) if s
    ]
    return keep_row["subject"], "\n".join(bullets)


def _digest_message(keep_row: dict, fold_rows: list[dict], g: dict) -> tuple[str | None, str]:
    """(message_or_None, date_iso) for a group's amend. message None ⇒ keep the existing message
    (no -m); date_iso '' ⇒ keep the keep's author date."""
    subject = g.get("subject") or keep_row["subject"]
    body = g.get("body")
    if body is None:
        _, body = auto_message(keep_row, fold_rows)
    date = g.get("date") or "keep"
    date_iso = ""
    if date == "last":
        date_iso = max((r["date"] for r in [keep_row, *fold_rows]), default="")
    elif date not in ("", "keep"):
        date_iso = date
    subject_changed = bool(g.get("subject")) and g["subject"] != keep_row["subject"]
    if subject_changed or body:
        return (subject if not body else f"{subject}\n\n{body}"), date_iso
    return None, date_iso


def _amend_line(message: str | None, date_iso: str = "") -> str:
    """An `exec git commit --amend …` rebase-todo line. Multi-line messages are emitted via a
    `printf` command substitution so the exec stays ONE physical todo line (a literal newline
    would split the todo and break the rebase). message None ⇒ --no-edit (date-only amend)."""
    parts = ["git", "commit", "--amend"]
    if message is None:
        parts.append("--no-edit")
    else:
        printf_args = " ".join(shlex.quote(ln) for ln in message.split("\n"))
        parts += ["-m", f"\"$(printf '%s\\n' {printf_args})\""]
    if date_iso:
        parts.append(f"--date={shlex.quote(date_iso)}")
    return "exec " + " ".join(parts)


def build_todo(rows: list[dict], groups: list[dict]) -> list[str]:
    """Rebase todo from resolved groups. Each fold is reordered to sit directly under its keep
    as `fixup`; a message/date override appends an `exec git commit --amend`. Commits in no
    group pass through as `pick`. Contiguous folds = no real reorder = conflict-free."""
    by_sha = {r["sha"]: r for r in rows}
    keep_of = {g["keep"]: g for g in groups}
    fold_set = {fs for g in groups for fs in g["fold"]}
    lines: list[str] = []
    for r in rows:
        sha = r["sha"]
        if sha in fold_set:
            continue  # emitted as a fixup under its keep
        if sha not in keep_of:
            lines.append(f"pick {sha}")
            continue
        g = keep_of[sha]
        lines.append(f"pick {sha}")
        fold_rows = [by_sha[fs] for fs in g["fold"] if fs in by_sha]
        lines += [f"fixup {fr['sha']}" for fr in fold_rows]
        msg, date_iso = _digest_message(by_sha[sha], fold_rows, g)
        if msg is not None or date_iso:
            lines.append(_amend_line(msg, date_iso))
    return lines


# ---- verbs ------------------------------------------------------------------

_RIG = typer.Option("", "--rig", "-r", help="target rig (default: cwd's rig)")
_BEAD = typer.Argument(..., metavar="<id>", help="bead id")
_AS = typer.Option("", "--as", help="crew/<name> identity (default: config/$WS_CREW/git)")
_VIEW = typer.Option(["log"], "--view", help="log|sig|diff|stat (repeatable)")
_JSONOUT = typer.Option(False, "--json", help="machine rows + flags (refine input)")


@app.command("brief")
def brief(bead: str = _BEAD, rig: str = _RIG):
    """Print the bead's requirements/goals and the repo's validation command. Read-only."""
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, rig, bead)
    _print_brief(cfg, entry, bead, _show(bead, main))


@app.command("assign")
def assign(
    bead: str = _BEAD,
    to: str = typer.Option(..., "--to", help="crew/<name> to assign + provision for"),
    rig: str = _RIG,
):
    """Orchestrator-only: stamp the assignee and provision the worktree with that identity.
    Leaves status `open` — the worker's `claim` is the ack that flips it to in_progress."""
    cfg = config.load()
    _entry, main, _target, _branch = worktree.locate(cfg, rig, bead)
    data = _show(bead, main)
    _guard_open(data, bead)
    _guard_not_other(data, to, bead)
    res = _bd(["assign", bead, to], main)
    if res.returncode != 0:
        raise typer.Exit(res.returncode)
    entry, target, _branch = worktree.ensure(cfg, rig, bead)
    _stamp(cfg, entry, target, to)
    typer.echo(f"✓ assigned {bead} → {to}; worktree {target}")


@app.command("claim")
def claim(
    bead: str = _BEAD,
    as_: str = _AS,
    rig: str = _RIG,
):
    """Ack that you're starting: re-attach/provision the worktree with your identity, refuse
    if it's someone else's, then `bd update --claim` as your actor (→ in_progress)."""
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, rig, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    data = _show(bead, main)
    _guard_open(data, bead)
    _guard_not_other(data, actor, bead)
    entry, target, _branch = worktree.ensure(cfg, rig, bead)
    _stamp(cfg, entry, target, actor)
    res = _bd(["update", bead, "--claim"], main, actor=actor)
    if res.returncode != 0:
        raise typer.Exit(res.returncode)
    typer.echo(f"✓ claimed {bead} as {actor}; worktree {target}")
    _print_brief(cfg, entry, bead, data)


@app.command("check")
def check(bead: str = _BEAD, rig: str = _RIG):
    """Run the rig's validation command against the worktree; propagate its exit code."""
    cfg = config.load()
    entry, _main, target, _branch = worktree.locate(cfg, rig, bead)
    if not target.exists():
        typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
        raise typer.Exit(1)
    rc = run(shlex.split(config.validate_cmd(cfg, entry)), cwd=str(target), check=False).returncode
    if rc != 0:
        raise typer.Exit(rc)


@app.command("submit")
def submit(bead: str = _BEAD, rig: str = _RIG):
    """Hand off to async review: verify the branch is clean conventional digests, validate the
    proposed hash from a clean checkout, (publish for out-of-process review,) then open a gate.
    Not 'done' — leaves the worktree intact and returns immediately."""
    cfg = config.load()
    entry, main, target, branch = worktree.locate(cfg, rig, bead)
    if not target.exists():
        typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
        raise typer.Exit(1)

    if not worktree.is_clean(target):
        typer.echo("✗ working tree not clean — commit or discard changes first", err=True)
        raise typer.Exit(1)
    cur = worktree.current_branch(target)
    if cur != branch:
        typer.echo(f"✗ on branch {cur or '(detached)'}, expected {branch}", err=True)
        raise typer.Exit(1)
    base = config.integration_branch(cfg, entry)
    count, subjects = worktree.history(entry, branch, base)
    ok, msg = _history_ok(count, subjects, config.max_commits(cfg, entry))
    if not ok:
        typer.echo(f"✗ {msg}", err=True)
        raise typer.Exit(1)

    # Clean-checkout validation — the result must not depend on dirty local state.
    rc = worktree.clean_checkout(entry, branch, config.validate_cmd(cfg, entry))
    if rc != 0:
        typer.echo(f"✗ clean-checkout validation failed (exit {rc}) — nothing submitted", err=True)
        raise typer.Exit(1)

    sha = worktree.head_sha(target)
    gate = config.review_gate(cfg, entry)
    # Out-of-process reviewers (GitHub CI) can't see a branch we don't push. Push BEFORE
    # set-state so a failed push blocks the gate too (no half-submitted bead).
    if gate.startswith("gh:") and worktree.push_branch(entry, branch) != 0:
        typer.echo("✗ failed to push branch for review — nothing submitted", err=True)
        raise typer.Exit(1)

    # Open the gate FIRST, then flip state — so we never leave a bead review=pending with
    # nothing blocking it (which would let the scheduler re-pick it).
    g = _bd(["gate", "create", "--blocks", bead, "--type", gate, "--reason", f"review {sha}"], main)
    if g.returncode != 0:
        typer.echo("✗ failed to open review gate — nothing submitted", err=True)
        raise typer.Exit(1)
    sres = _bd(["set-state", bead, "review=pending", "--reason", f"submitted {sha}"], main)
    if sres.returncode != 0:
        typer.echo("✗ failed to set review state — nothing submitted", err=True)
        raise typer.Exit(1)
    typer.echo(f"✓ submitted {bead} @ {sha} — opened {gate} review gate (worktree left intact)")


@app.command("merge")
def merge(
    bead: str = _BEAD,
    rig: str = _RIG,
    rm: bool = typer.Option(False, "--rm", help="remove the worktree after a clean merge"),
):
    """Merger-only: serialize integration of an *approved* bead onto the integration branch.
    Holds the rig merge slot, re-verifies a small clean conventional history, merges `--no-ff`
    (history preserved, never squashed at the boundary), closes the bead, releases the slot.
    Refuses unless the review gate is resolved; on conflict it aborts and releases — never drops
    work. (No worker-side ack: this is the merge owner, not the developer.)"""
    cfg = config.load()
    entry, main, _target, branch = worktree.locate(cfg, rig, bead)
    _guard_open(_show(bead, main), bead)

    if _state(bead, "review", main) == "changes-requested":
        typer.echo(f"✗ {bead} has changes-requested — resume & resubmit, don't merge", err=True)
        raise typer.Exit(1)
    if _open_gate(bead, main):
        typer.echo(f"✗ {bead} review gate still open — not approved yet", err=True)
        raise typer.Exit(1)

    base = config.integration_branch(cfg, entry)
    count, subjects = worktree.history(entry, branch, base)
    ok, msg = _history_ok(count, subjects, config.max_commits(cfg, entry))
    if not ok:
        typer.echo(f"✗ {msg} — bounce back for self-refine", err=True)
        raise typer.Exit(1)

    _bd(["merge-slot", "create"], main)  # idempotent: no-op once the rig's slot bead exists
    if _bd(["merge-slot", "acquire"], main).returncode != 0:
        typer.echo("✗ could not acquire merge slot — another merge holds it", err=True)
        raise typer.Exit(1)
    try:
        prof = config.work_identity(cfg, entry)
        agent = prof["mode"] == "agent"
        rc, out = worktree.merge_no_ff(
            entry,
            branch,
            base,
            name=(prof["name"] or "") if agent else "",
            email=(prof["email"] or "") if agent else "",
            signing_key=(prof["signing_key"] or "") if agent else "",
            sign=prof["sign"] if agent else False,
            message=f"merge {bead}",
        )
        if rc != 0:
            typer.echo(f"✗ merge failed — aborted, nothing merged:\n{out}", err=True)
            raise typer.Exit(rc)
        if _bd(["close", bead, "--reason", "merged"], main).returncode != 0:
            typer.echo("⚠ merged but failed to close the bead — close it manually", err=True)
    finally:
        _bd(["merge-slot", "release"], main)

    typer.echo(f"✓ merged {bead} ({branch} --no-ff → {base}) and closed it")
    if rm:
        worktree.remove(rig, bead, force=True)


@app.command("resume")
def resume(
    bead: str = _BEAD,
    as_: str = _AS,
    rig: str = _RIG,
):
    """After review returns changes-requested: re-attach a fresh worktree on the bead branch,
    print the feedback, and re-assert the claim. Address the feedback and `submit` again."""
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, rig, bead)
    state = _state(bead, "review", main)
    if state != "changes-requested":
        typer.echo(f"✗ {bead} not in review:changes-requested (now: {state or 'none'})", err=True)
        raise typer.Exit(1)
    entry, target, _branch = worktree.ensure(cfg, rig, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    _stamp(cfg, entry, target, actor)
    typer.echo("── review feedback ──")
    _bd(["comments", bead], main)
    _bd(["update", bead, "--claim"], main, actor=actor)
    typer.echo(f"✓ resumed {bead} as {actor}; worktree {target}")


@app.command("abandon")
def abandon(
    bead: str = _BEAD,
    rig: str = _RIG,
    rm: bool = typer.Option(False, "--rm", help="also remove the worktree (default: keep it)"),
):
    """Release the claim and record the abandon. Recovery path for stalls."""
    cfg = config.load()
    entry, main, target, _branch = worktree.locate(cfg, rig, bead)
    actor = identity.resolve_actor("", config.work_identity(cfg, entry)["name"] or "")
    # Recovery path: deliberately no refuse-if-other guard (the point is to release a bead a
    # stalled/dead agent left claimed). Surface bd failures instead of always reporting success.
    r1 = _bd(["set-state", bead, "review=abandoned", "--reason", "abandoned"], main, actor=actor)
    r2 = _bd(["update", bead, "--status", "open", "--assignee", ""], main, actor=actor)
    if rm and target.exists():
        worktree.remove(rig, bead, force=True)
    if r1.returncode or r2.returncode:
        typer.echo(f"⚠ abandoned {bead} with bd errors (see above)", err=True)
        raise typer.Exit(1)
    typer.echo(f"✓ abandoned {bead}" + ("; worktree removed" if rm else "; worktree kept"))


# ---- show (read-only history views) -----------------------------------------

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


@app.command("show")
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
    integration = config.integration_branch(cfg, entry)
    base = worktree.base_of(entry, branch, integration)
    rows = flag_rows(worktree.commit_rows(entry, base, branch)) if base else []
    if json_out:
        payload = {"base": base[:7], "max_commits": config.max_commits(cfg, entry), "commits": rows}
        typer.echo(json.dumps(payload))
        return
    if not base:
        typer.echo(f"✗ cannot compare {branch} against {integration} (present locally?)", err=True)
        return
    if not rows:
        typer.echo(f"no commits over {base[:7]}")
        return
    for v in view:
        _render_view(v, rows, base, config.max_commits(cfg, entry), entry, branch)


# ---- refine (squash local checkpoint noise) ---------------------------------


def _load_plan(plan_arg: str) -> dict:
    """Read a squash-plan from a file path or '-' (stdin). Raises on read/JSON errors."""
    text = sys.stdin.read() if plan_arg == "-" else Path(plan_arg).read_text()
    return json.loads(text)


def _simulate(rows: list[dict], groups: list[dict]) -> list[str]:
    """The would-be subject list after applying `groups`: folds dropped, keeps (with override
    subjects) and passthroughs in place. Used by --dry-run (no git writes)."""
    fold_set = {fs for g in groups for fs in g["fold"]}
    keep_of = {g["keep"]: g for g in groups}
    result = []
    for r in rows:
        if r["sha"] in fold_set:
            continue
        g = keep_of.get(r["sha"])
        result.append((g.get("subject") if g else None) or r["subject"])
    return result


def _restore(target, backup) -> None:
    """Abort any in-progress rebase and hard-reset the branch back to its pre-refine tip."""
    worktree.rebase_abort(target)
    worktree.reset_hard(target, backup)


@app.command("refine")
def refine(
    bead: str = _BEAD,
    plan: str = typer.Option("", "--plan", help="squash-plan JSON file or '-' for stdin"),
    autosquash: bool = typer.Option(False, "--autosquash", help="fold fixup!/squash! into targets"),
    since: str = typer.Option("", "--since", help="fold <ref>..tip into a single digest"),
    dry_run: bool = typer.Option(False, "--dry-run", help="print the would-be log; change nothing"),
    rig: str = _RIG,
):
    """Squash local checkpoint noise into conventional digests behind a backup branch and a
    byte-identical gate (the net tree never changes). Retains per-digest author dates. Exactly
    one input mode: --plan | --autosquash | --since."""
    cfg = config.load()
    entry, _main, target, branch = worktree.locate(cfg, rig, bead)
    if sum([bool(plan), autosquash, bool(since)]) != 1:
        typer.echo("✗ pass exactly one of --plan / --autosquash / --since", err=True)
        raise typer.Exit(1)
    if not target.exists():
        typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
        raise typer.Exit(1)
    base = worktree.base_of(entry, branch, config.integration_branch(cfg, entry))
    if not base:
        typer.echo("✗ cannot compute base (is the integration branch present locally?)", err=True)
        raise typer.Exit(1)

    # Build the plan + resolve groups (autosquash lets git build its own todo, so no plan).
    groups: list[dict] = []
    if not autosquash:
        if since:
            plan_dict = plan_from_since(worktree.commit_rows(entry, since, branch))
        else:
            try:
                plan_dict = _load_plan(plan)
            except (OSError, json.JSONDecodeError) as e:
                typer.echo(f"✗ cannot read plan: {e}", err=True)
                raise typer.Exit(1) from None
        if isinstance(plan_dict, dict) and plan_dict.get("base"):
            base = plan_dict["base"]  # explicit base override
        rows = worktree.commit_rows(entry, base, branch)
        ok, errors, groups = validate_plan(plan_dict, rows)
        if not ok:
            for e in errors:
                typer.echo(f"✗ {e}", err=True)
            raise typer.Exit(1)
    else:
        rows = worktree.commit_rows(entry, base, branch)

    # --dry-run: simulate + print; make NO changes (no clean-tree requirement — read-only).
    if dry_run:
        result = (
            [r["subject"] for r in rows if not _MARKER.match(r["subject"])]
            if autosquash
            else _simulate(rows, groups)
        )
        typer.echo(f"would produce {len(result)} commit(s) over {base[:7]}:")
        for s in result:
            typer.echo(f"  {s}")
        return

    # Real refine — now require a clean tree on the expected branch.
    if not worktree.is_clean(target):
        typer.echo("✗ working tree not clean — commit or discard changes first", err=True)
        raise typer.Exit(1)
    cur = worktree.current_branch(target)
    if cur != branch:
        typer.echo(f"✗ on branch {cur or '(detached)'}, expected {branch}", err=True)
        raise typer.Exit(1)

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = worktree.backup_branch(entry, branch, ts)
    typer.echo(f"backup branch: {backup}")

    if autosquash:
        rc, out = worktree.rebase_autosquash(target, base)
    else:
        rc, out = worktree.rebase_squash(target, base, build_todo(rows, groups))

    if rc != 0:
        _restore(target, backup)
        typer.echo(f"✗ refine rebase failed (exit {rc}) — restored from {backup}", err=True)
        if out.strip():
            typer.echo(out.strip(), err=True)
        typer.echo(
            "  keep a keep's folds contiguous, or refine-as-you-go with `git commit --fixup`",
            err=True,
        )
        raise typer.Exit(1)

    # Byte-identical gate — the net change must be untouched (guarantees a pure rewrite).
    if not worktree.same_tree(entry, backup, branch):
        worktree.reset_hard(target, backup)
        typer.echo(f"✗ refine changed the tree — restored from {backup}", err=True)
        raise typer.Exit(1)

    typer.echo(f"✓ refined {bead} ({branch}) — backup left at {backup}:")
    typer.echo(worktree.log_range(entry, base, branch))
    typer.echo(f"restore with: git -C {target} reset --hard {backup}")
