"""`ws bd …` — a workspace-aware passthrough to beads, with optional rig routing.

Plain: forwards to `bd` in the current dir, intercepting `create` (and `import`) to auto-apply
the provider/org/repo triplet (ports bdc). `-a`/`-r` route across rigs (requires git_workspace).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import typer

from . import config, route, validate
from .identity import workspace_identity
from .run import run


def triplet_label_args(cwd) -> list[str]:
    """`-l provider:…,org:…,repo:…` for `cwd`'s managed identity, or [] outside one.

    Typer-free core: the identity-triplet labels `ws bd create` auto-applies, shared with
    the future MCP entrypoint so both build the same label set."""
    ident = workspace_identity(cwd)
    if ident is None:
        return []
    provider, org, repo = ident
    return ["-l", f"provider:{provider},org:{org},repo:{repo}"]


def create(create_args, cwd) -> tuple[int, str]:
    """Run `bd create` for `cwd`'s rig with its identity triplet appended. Typer-free core.

    Returns `(exit_code, error)`: when the rig has label violations, returns `(1, msg)` and
    runs nothing; otherwise `(bd's exit code, "")`. Callers render `error` to the user."""
    if validate.has_violations(cwd=cwd):
        return 1, "rig has label violations — fix with 'ws labels validate' before creating."
    extra = triplet_label_args(cwd)
    return run(["bd", "create", *create_args, *extra], check=False, cwd=cwd).returncode, ""


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
    """Run `bd import` for `cwd`'s rig with its identity triplet merged into every record.

    `bd import` is a raw upsert and, unlike `create`, does NOT inject the triplet — so a backfill
    JSONL would land registry-invalid. This reads the source (a file path, or ``-``/none = stdin),
    augments each record's labels, and imports the augmented copy. Idempotent by ``external_ref``.
    Returns `(exit_code, error)` like `create`; callers render `error`."""
    if validate.has_violations(cwd=cwd):
        return 1, "rig has label violations — fix with 'ws labels validate' before importing."
    ident = workspace_identity(cwd)
    if ident is None:
        return 1, "not inside a managed rig — cannot resolve the identity triplet for import."
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
        records = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    except json.JSONDecodeError as e:
        return 1, f"invalid JSONL in {src!r}: {e}"
    augmented = augment_labels(records, ident)
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
        tf.write("\n".join(json.dumps(r) for r in augmented) + "\n")
        tmp = tf.name
    try:
        result = run(["bd", "import", *flags, tmp], check=False, capture=True, cwd=cwd)
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
    return run(["bd", *args], check=False, cwd=cwd).returncode


def passthrough(mode, target, args):
    route.reject_inline_flags(args)
    cfg = config.load() if mode != "cwd" else {}
    tgts = route.targets(cfg, mode, target)
    try:
        route.fan_out(tgts, lambda _label, cwd: _run_one(args, cwd))
    finally:
        route.invalidate_targets(cfg, tgts)  # a passthrough may have mutated the rig
