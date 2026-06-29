"""Validation engine — the linter pass. Ports labels.sh cmd_validate.

Two check sets: registry-level required-org prefix consistency, and per-issue
identity/phase checks against the current bd DB. Each entrypoint runs these in
advisory mode (report, exit 0) or enforce mode (fail on any violation).
"""

from __future__ import annotations

import json

import typer

from . import config
from .registry import closed_dimensions, required_violations
from .run import run


def _label_val(labels, prefix):
    for label in labels:
        if label.startswith(prefix):
            return label[len(prefix) :]
    return ""


def _issue_checks(cfg, cwd=None):
    """(problems, db_ok). db_ok is False when bd/the DB couldn't be reached — the
    per-issue checks are then skipped (not silently treated as clean)."""
    res = run(["bd", "list", "--limit", "0", "--json"], check=False, capture=True, cwd=cwd)
    if res.returncode != 0:
        return [], False
    issues = json.loads(res.stdout or "[]")
    closed = closed_dimensions(cfg)
    repos = cfg.get("managed_repos", [])
    problems = []
    for i in issues:
        iid = i.get("id", "")
        labels = i.get("labels") or []
        matches = [e for e in repos if iid.startswith(f"{e['prefix']}-")]
        if not matches:
            problems.append(f"{iid}\tunknown rig prefix (not registered)")
            continue
        # longest matching prefix wins (handles bare vs code-prefixed overlap)
        m = max(matches, key=lambda e: len(str(e["prefix"])))
        errs = []
        for fld in ("provider", "org", "repo"):
            val = _label_val(labels, f"{fld}:")
            if val and val != str(m[fld]):
                errs.append(f"{fld}:{val}≠{m[fld]}")
        # closed dimensions: any label value outside the declared set is invalid
        for dim, allowed in closed.items():
            bad = [
                label[len(dim) + 1 :]
                for label in labels
                if label.startswith(f"{dim}:") and label[len(dim) + 1 :] not in allowed
            ]
            if bad:
                errs.append(f"bad-{dim}:{','.join(bad)}")
        if errs:
            problems.append(f"{iid}\t{' '.join(errs)}")
    return problems, True


def has_violations(cfg=None, cwd=None) -> bool:
    cfg = cfg if cfg is not None else config.load()
    problems, _ = _issue_checks(cfg, cwd)
    return bool(required_violations(cfg) or problems)


def validate(mode) -> int:
    """Print findings; return 0 if clean else 1. Raise Exit(1) in enforce mode."""
    cfg = config.load()
    rc = 0

    rv = required_violations(cfg)
    if rv:
        typer.echo("✗ required-org prefix violations:")
        for v in rv:
            typer.echo(f"    {v}")
        rc = 1

    problems, db_ok = _issue_checks(cfg)
    if problems:
        typer.echo("✗ issue/label problems:")
        for p in problems:
            typer.echo(f"    {p}")
        rc = 1
    if not db_ok:
        typer.echo("note: bd DB unavailable — per-issue checks skipped.", err=True)

    if rc == 0:
        ok_msg = (
            "✓ registry valid"
            if not db_ok
            else (
                "✓ valid: prefixes consistent, identity labels match the registry, phases in range."
            )
        )
        typer.echo(ok_msg)
    elif mode == "enforce":
        raise typer.Exit(1)
    return rc
