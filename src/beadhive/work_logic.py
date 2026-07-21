"""Shared helpers for `ws work` — the typer-free building blocks split out of `work.py`.

Two groups live here: the pure `show`/`refine` machinery (conventional-commit regex + squash-plan
validation / todo-construction / digest-message) that touches no git/bd/typer/config, AND the small
lifecycle guard/seam helpers (`_guard_open` / `_guard_not_other` / `review_gates` /
`open_gate_lines` / `_history_ok` /
`_stamp`) that DO touch bd/typer/config/identity. The guards moved here so `work_group` can reach
them without importing `work` (breaking the module<->module cycle). `work.py` re-exports every name
here, so callers and tests keep importing them as `ws.work.<name>`.
"""

from __future__ import annotations

import re
import shlex

import typer

from . import bd, config, identity

# Conventional-commit subject — type(scope)!: summary. Used by the submit cleanliness guard.
_CONVENTIONAL = re.compile(
    r"^(feat|fix|refactor|docs|test|chore|perf|ci|build|style|revert)(\([^)]+\))?!?: .+"
)

# fixup!/squash! autosquash markers (git's own --autosquash trigger prefixes).
_MARKER = re.compile(r"^(fixup|squash)! ")

# A convention review gate's reason marker: `reason: [bh:]review <sha>` (submit writes `bh:review
# <sha>`; legacy gates wrote the bare `review <sha>`). The `bh:` prefix is optional for back-compat
# and the trailing hex-sha requirement is what separates a real review gate from an ad-hoc human
# gate whose reason merely starts with the word "review" (e.g. "review the rollout plan with ops").
_REVIEW_REASON = re.compile(r"reason: (?:bh:)?review [0-9a-f]{7,40}\b")


def is_review_gate_desc(desc: str) -> bool:
    """True iff `desc` is a convention review-gate description — the ONE selector every verb shares
    (bh-c3il), so a human checkpoint gate reasoned "review …" (no hex sha) is never misclassified
    as a review gate. Matches both the current `bh:review <sha>` marker and the legacy `review
    <sha>` form."""
    return bool(_REVIEW_REASON.search(desc.lower()))


def opt_str(value) -> str:
    """Normalize an optional string flag to a plain str. The lifecycle verbs are Typer commands
    AND called directly (tests / future MCP); an unpassed Typer option arrives as its OptionInfo
    sentinel, not the value, so coerce anything that isn't a str to ''. Via the CLI Typer always
    passes a real str, so this only smooths the direct-call path — keeping a new opt-in flag from
    churning every existing call site."""
    return value if isinstance(value, str) else ""


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
                {
                    "keep": keep,
                    "fold": folds,
                    "subject": g.get("subject"),
                    "body": g.get("body"),
                    "date": g.get("date") or "keep",
                }
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
    bullets = [f"- {s}" for s in (_MARKER.sub("", r["subject"]).strip() for r in fold_rows) if s]
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


# ---- lifecycle guard / seam helpers (shared with work_group; formerly work.py privates) -----


def _bead_gates(bead, cwd, include_resolved=False) -> list[dict]:
    """All bd gates whose description names `bead`. Identity is DESCRIPTION-based (matches
    `bd gate create --blocks <bead>` at submit), so a dep-less gate — e.g. on an epic, which
    can't carry a blocks edge — is still found. Empty list on a bd read failure.
    `--limit 0` defeats bd's default 50-result window, which silently drops older gates
    (bh-pwi2: an open review gate aged out of the window and approve couldn't see it)."""
    args = ["gate", "list", "--limit", "0"] + (["--all"] if include_resolved else [])
    gates = bd.json(args, cwd)
    if not isinstance(gates, list):
        return []
    return [
        g
        for g in gates
        if isinstance(g, dict) and bead.lower() in str(g.get("description") or "").lower()
    ]


def review_gates(bead, cwd) -> tuple[list[dict], list[dict]]:
    """The canonical review-gate selector (bh-c3il): EVERY gate for `bead` whose description
    carries the submit reason (`reason: review <sha>`), split ``(open, resolved)``. Submit
    consults it to reuse/supersede, approve resolves the whole open set, and the flow metrics
    read it — ONE selector, so the verbs can never disagree about which gates count."""
    matches = [
        g
        for g in _bead_gates(bead, cwd, include_resolved=True)
        if is_review_gate_desc(str(g.get("description") or ""))
    ]
    open_ = [g for g in matches if str(g.get("status")) == "open"]
    resolved = [g for g in matches if str(g.get("status")) != "open"]
    return open_, resolved


def _gate_kind(gate) -> str:
    """'review' | 'security' | 'kickoff' | 'other' — classified from the same description
    markers the verbs write: `reason: review` (submit), the `security:` marker (warden,
    via `guard.is_security_gate`), and `kickoff` (molecule kickoff gates)."""
    from . import guard  # lazy: keep this typer-free module's import surface minimal

    desc = str(gate.get("description") or "").lower()
    if is_review_gate_desc(desc):
        return "review"
    if guard.is_security_gate(gate):
        return "security"
    if "kickoff" in desc:
        return "kickoff"
    return "other"


def _gate_reason(gate) -> str:
    """The `reason: …` tail of a gate description (first line, trimmed) — enough for a human
    to identify an unclassified gate in a refusal message."""
    desc = str(gate.get("description") or "")
    idx = desc.lower().find("reason: ")
    snippet = desc[idx + len("reason: ") :] if idx >= 0 else desc
    first = snippet.strip().splitlines()[0] if snippet.strip() else ""
    return first[:60]


def gate_rows(bead, cwd) -> list[dict]:
    """Every gate touching `bead` — open AND resolved — as ``{id, kind, status, reason}`` rows,
    open ones first (stable within each group). Kind comes from the canonical classifier
    (`_gate_kind`, bh-c3il); an unclassified gate surfaces as ``ad-hoc``. This is the at-a-glance
    gate view `work show` renders and the show payload exposes (bh-i371). Empty list on a bd
    read failure."""
    rows = [
        {
            "id": str(g.get("id") or "?"),
            "kind": "ad-hoc" if (kind := _gate_kind(g)) == "other" else kind,
            "status": "open" if str(g.get("status")) == "open" else "resolved",
            "reason": _gate_reason(g),
        }
        for g in _bead_gates(bead, cwd, include_resolved=True)
    ]
    return sorted(rows, key=lambda r: r["status"] != "open")  # stable: open first, order kept


def open_gate_lines(bead, cwd, skip_marker="") -> list[str]:
    """One refusal line per OPEN gate blocking `bead`, classified by kind, so the merger sees
    WHY the merge is blocked and who clears it: review (not approved — `work approve`),
    security (needs a warden), anything else by id + reason snippet. Empty when nothing blocks.
    Deliberately BROAD — ANY open gate counts, not just review: that breadth is what lets the
    warden's `security:*` gate block the merge in parallel with review (do not narrow it).
    `skip_marker` is the one explicit opt-out: a description substring for gates the CALLER
    itself owns and re-drives (the pr landing path's own `pr-merge` gate must not block an
    idempotent re-run of that same path)."""
    lines = []
    for g in _bead_gates(bead, cwd):
        if str(g.get("status")) != "open":
            continue
        if skip_marker and skip_marker in str(g.get("description") or "").lower():
            continue
        gid = str(g.get("id") or "?")
        kind = _gate_kind(g)
        if kind == "review":
            lines.append(
                f"  - review gate {gid}: not approved yet — "
                f"`{config.BINARY_ALIAS} work approve {bead}`"
            )
        elif kind == "security":
            lines.append(
                f"  - security gate {gid}: needs a warden — "
                f"`{config.BINARY_ALIAS} work approve {bead} --as warden/<name>`"
            )
        else:
            lines.append(f"  - {kind} gate {gid}: {_gate_reason(g)}")
    return lines


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


def _guard_holds_claim(data, actor, bead):
    """Refuse unless *actor* currently holds the claim. Stricter than `_guard_not_other`:
    an abandoned (assignee None) or reassigned bead is refused, not silently adopted — so a
    submit from an agent whose claim was released can't open a review gate on an unowned bead."""
    cur = str(data.get("assignee") or "")
    if cur != actor:
        held = f"held by {cur}" if cur else "not currently claimed"
        typer.echo(
            f"✗ bead {bead} is {held} — {actor} no longer holds the claim; "
            "not submitting (re-claim it first)",
            err=True,
        )
        raise typer.Exit(1)


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


def _stamp(cfg, entry, target, actor):
    """Stamp agent identity + signing into the worktree, unless supervised (inherit human)."""
    prof = config.work_identity(cfg, entry, actor)
    if prof["mode"] == "supervised":
        return
    identity.stamp(
        target,
        name=actor or prof["name"] or "",
        email=prof["email"] or "",
        signing_key=prof["signing_key"] or "",
        sign=prof["sign"],
    )
