"""Pure helpers for `ws work` — no git, no bd, no typer, no config.

These are the unit-tested building blocks behind `show`/`refine`: the conventional-commit
regex + the squash-plan validation / todo-construction / digest-message machinery. They are
split out of `work.py` so the lifecycle verbs (which own the bd seam) and the contending
feature beads touch a different file. `work.py` re-exports every public name here, so callers
and tests keep importing them as `ws.work.<name>`.
"""

from __future__ import annotations

import re
import shlex

# Conventional-commit subject — type(scope)!: summary. Used by the submit cleanliness guard.
_CONVENTIONAL = re.compile(
    r"^(feat|fix|refactor|docs|test|chore|perf|ci|build|style|revert)(\([^)]+\))?!?: .+"
)

# fixup!/squash! autosquash markers (git's own --autosquash trigger prefixes).
_MARKER = re.compile(r"^(fixup|squash)! ")


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
