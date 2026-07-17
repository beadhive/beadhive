"""`ws bd …` — a workspace-aware passthrough to beads, with optional hive routing.

Plain: forwards to `bd` in the current dir, intercepting `create` (and `import`) to auto-apply
the provider/org/repo triplet (ports bdc). `-a`/`-r` route across hives (requires git_workspace).
"""

from __future__ import annotations

import json as _json
import sys
import tempfile
from pathlib import Path

import typer

from . import config, guard, route, validate
from .identity import resolve_actor, workspace_identity
from .run import run as _run


def run(args, cwd, actor="", capture=False, text_input=None):
    """Run a `bd` subcommand scoped to the hive via `-C <cwd>` (so the right Beads DB is hit
    regardless of the process cwd / `--hive`). Prepends `--actor <name>` for the audit trail;
    `text_input` feeds stdin (e.g. a JSONL record for `bd import -`). The one shared bd-invocation
    helper the work/plan/triage/report layers all call."""
    cmd = ["bd", "-C", str(cwd)]
    if actor:
        cmd += ["--actor", actor]
    cmd += list(args)
    return _run(cmd, check=False, capture=capture, text_input=text_input)


def err_line(res) -> str:
    """First non-empty output line — bd's `Error: …` headline, never its usage dump."""
    for line in ((res.stdout or "") + (res.stderr or "")).splitlines():
        if line.strip():
            return line.strip()
    return f"exit {res.returncode}"


def show(bead, cwd):
    """The bead's JSON object (bd show may return a single object or a 1-list), or None."""
    data = json(["show", bead], cwd)
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def state(bead, dim, cwd) -> str:
    """Current value of a state dimension via `bd state <bead> <dim>` ('' if unset)."""
    res = run(["state", bead, dim], cwd, capture=True)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def triplet_label_args(cwd) -> list[str]:
    """`-l provider:…,org:…,repo:…` for `cwd`'s managed identity, or [] outside one.

    Typer-free core: the identity-triplet labels `ws bd create` auto-applies, shared with
    the future MCP entrypoint so both build the same label set."""
    ident = workspace_identity(cwd)
    if ident is None:
        return []
    provider, org, repo = ident
    return ["-l", f"provider:{provider},org:{org},repo:{repo}"]


def json(args, cwd):
    """Run ``bd -C <cwd> <args> --json`` and return the parsed dict/list, or None on error.

    Appends ``--json`` itself — callers pass args WITHOUT ``--json``.  Returns None when the
    process exits non-zero or the output is not valid JSON (matches the None-on-failure contract
    the work/triage/plan layers rely on)."""
    res = _run(["bd", "-C", str(cwd), *args, "--json"], check=False, capture=True)
    if res.returncode != 0:
        return None
    try:
        return _json.loads(res.stdout or "null")
    except _json.JSONDecodeError:
        return None


def _is_help(args) -> bool:
    """True when `args` asks for help/usage — the label gate must not block `--help`."""
    return any(a in ("-h", "--help") for a in args)


def create(create_args, cwd) -> tuple[int, str]:
    """Run `bd create` for `cwd`'s hive with its identity triplet appended. Typer-free core.

    Returns `(exit_code, error)`: when the hive has label violations, returns `(1, msg)` and
    runs nothing; otherwise `(bd's exit code, "")`. Callers render `error` to the user.
    `--help`/`-h` always falls through — usage should print even with label violations."""
    if not _is_help(create_args) and validate.has_violations(cwd=cwd):
        return 1, "hive has label violations — fix with 'bh label validate' before creating."
    extra = triplet_label_args(cwd)
    return _run(["bd", "create", *create_args, *extra], check=False, cwd=cwd).returncode, ""


def _create(create_args, cwd):
    """CLI wrapper over `create`: echo the violation error to stderr, return the exit code."""
    code, error = create(create_args, cwd)
    if error:
        typer.echo(f"✗ {error}", err=True)
    return code


def augment_labels(records: list[dict], ident: tuple[str, str, str]) -> list[dict]:
    """Merge the identity triplet into each record's ``labels`` (dedup, order-stable).

    Typer-free core, shared idempotency: appends ``provider:``/``org:``/``repo:`` only when
    absent, so re-importing an already-triplet-tagged record is a no-op on labels."""
    provider, org, repo = ident
    triplet = [f"provider:{provider}", f"org:{org}", f"repo:{repo}"]
    out = []
    for rec in records:
        labels = list(rec.get("labels") or [])
        for tag in triplet:
            if tag not in labels:
                labels.append(tag)
        out.append({**rec, "labels": labels})
    return out


def import_labeled(import_args, cwd) -> tuple[int, str]:
    """Run `bd import` for `cwd`'s hive with its identity triplet merged into every record.

    `bd import` is a raw upsert and, unlike `create`, does NOT inject the triplet — so a backfill
    JSONL would land registry-invalid. This reads the source (a file path, or ``-``/none = stdin),
    augments each record's labels, and imports the augmented copy. Idempotent by ``external_ref``.
    Returns `(exit_code, error)` like `create`; callers render `error`. `--help`/`-h` always
    falls through to plain `bd import --help` — usage should print even with label violations,
    and without touching stdin/the identity triplet."""
    if _is_help(import_args):
        return _run(["bd", "import", *import_args], check=False, cwd=cwd).returncode, ""
    if validate.has_violations(cwd=cwd):
        return 1, "hive has label violations — fix with 'bh label validate' before importing."
    ident = workspace_identity(cwd)
    if ident is None:
        return 1, "not inside a managed hive — cannot resolve the identity triplet for import."
    flags = [a for a in import_args if a.startswith("-") and a != "-"]
    srcs = [a for a in import_args if not a.startswith("-")]
    src = srcs[-1] if srcs else "-"
    try:
        if src == "-":
            text = sys.stdin.read()
        else:
            p = Path(src)
            text = (p if p.is_absolute() else Path(cwd, src)).read_text()
    except OSError as e:
        return 1, f"cannot read import source {src!r}: {e}"
    try:
        records = [_json.loads(ln) for ln in text.splitlines() if ln.strip()]
    except _json.JSONDecodeError as e:
        return 1, f"invalid JSONL in {src!r}: {e}"
    augmented = augment_labels(records, ident)
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
        tf.write("\n".join(_json.dumps(r) for r in augmented) + "\n")
        tmp = tf.name
    try:
        result = _run(["bd", "import", *flags, tmp], check=False, capture=True, cwd=cwd)
    finally:
        Path(tmp).unlink(missing_ok=True)
    combined = (result.stdout or "") + (result.stderr or "")
    # bd errors "nothing to commit" when an import changes nothing — that IS the idempotent no-op
    # a re-run should produce (the upsert created zero duplicates), so treat it as success.
    if result.returncode != 0 and "nothing to commit" in combined:
        typer.echo("nothing to import — already up to date")
        return 0, ""
    if combined.strip():
        typer.echo(combined.rstrip())
    return result.returncode, ""


def _import(import_args, cwd):
    """CLI wrapper over `import_labeled`: echo the error to stderr, return the exit code."""
    code, error = import_labeled(import_args, cwd)
    if error:
        typer.echo(f"✗ {error}", err=True)
    return code


def _run_one(args, cwd):
    if args and args[0] == "create":
        return _create(args[1:], cwd)
    if args and args[0] == "import":
        return _import(args[1:], cwd)
    return _run(["bd", *args], check=False, cwd=cwd).returncode


def passthrough(mode, target, args):
    route.reject_inline_flags(args)
    guard.guard_bd(args, resolve_actor())  # gate `bd github push/sync` (seat + single-item)
    cfg = config.load() if mode != "cwd" else {}
    tgts = route.targets(cfg, mode, target)
    try:
        route.fan_out(tgts, lambda _label, cwd: _run_one(args, cwd))
    finally:
        route.invalidate_targets(cfg, tgts)  # a passthrough may have mutated the hive
