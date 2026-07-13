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


def _bead_problems(iid, labels, repos, closed):
    """Per-bead label problems for ONE bead: unknown rig prefix, triplet mismatch against the
    registry, and closed-dimension values outside their declared set. Returns a list of problem
    strings (empty == clean). The shared core of both the whole-DB linter (`_issue_checks`) and
    the single-bead intake gate (`bead_violations`)."""
    matches = [e for e in repos if iid.startswith(f"{e['prefix']}-")]
    if not matches:
        return [f"{iid}\tunknown rig prefix (not registered)"]
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
    return [f"{iid}\t{' '.join(errs)}"] if errs else []


def _issues_and_problems(cfg, cwd=None):
    """(issues, problems, db_ok) — the full per-issue check, WITH the raw bd-list records the
    CLI display needs to aggregate identical root causes (bh-9iiz). `_issue_checks` is a thin
    public wrapper over this that drops `issues` to keep its return format unchanged."""
    res = run(["bd", "list", "--limit", "0", "--json"], check=False, capture=True, cwd=cwd)
    if res.returncode != 0:
        return [], [], False
    issues = json.loads(res.stdout or "[]")
    closed = closed_dimensions(cfg)
    repos = cfg.get("managed_repos", [])
    problems = []
    for i in issues:
        problems.extend(_bead_problems(i.get("id", ""), i.get("labels") or [], repos, closed))
    return issues, problems, True


def _issue_checks(cfg, cwd=None):
    """(problems, db_ok). db_ok is False when bd/the DB couldn't be reached — the
    per-issue checks are then skipped (not silently treated as clean)."""
    _issues, problems, db_ok = _issues_and_problems(cfg, cwd)
    return problems, db_ok


def bead_violations(cfg, iid, labels) -> list[str]:
    """Per-bead label problems for a SINGLE bead's own labels — the intake write path (report /
    escalate) validates ONLY the bead it is about to file, NOT the target rig's whole DB. A
    cross-rig reporter has no authority over the target's pre-existing label debt and must never
    be deadlocked by it. Returns a list of problem strings (empty == clean)."""
    cfg = cfg if cfg is not None else config.load()
    repos = cfg.get("managed_repos", [])
    return _bead_problems(iid, labels or [], repos, closed_dimensions(cfg))


def has_violations(cfg=None, cwd=None) -> bool:
    cfg = cfg if cfg is not None else config.load()
    problems, _ = _issue_checks(cfg, cwd)
    return bool(required_violations(cfg) or problems)


_UNREGISTERED_MSG = "unknown rig prefix (not registered)"  # the single-root-cause message


def _bead_prefix(iid: str) -> str:
    """The rig-prefix portion of a bead id: everything before the last `-<suffix>`."""
    return iid.rsplit("-", 1)[0] if "-" in iid else iid


def _agreed_triplet(issues_by_id, iids):
    """`provider/org/repo` the affected beads' OWN labels agree on, or None when they don't
    (or don't say)."""
    vals = {"provider": set(), "org": set(), "repo": set()}
    for iid in iids:
        labels = (issues_by_id.get(iid) or {}).get("labels") or []
        for fld in vals:
            v = _label_val(labels, f"{fld}:")
            if v:
                vals[fld].add(v)
    if all(len(vals[fld]) == 1 for fld in vals):
        return "/".join(next(iter(vals[fld])) for fld in ("provider", "org", "repo"))
    return None


def _render_problems(issues, problems) -> list[str]:
    """Aggregate identical unknown-rig-prefix root causes into ONE line each (with the affected
    count + a fix command); leave genuinely per-issue problems (triplet mismatch, bad-dimension)
    as individual lines. CLI display only — `_issue_checks`'s own return is untouched (bh-9iiz)."""
    issues_by_id = {i.get("id", ""): i for i in issues}
    unregistered: dict[str, list[str]] = {}
    other = []
    for p in problems:
        iid, _, msg = p.partition("\t")
        if msg == _UNREGISTERED_MSG:
            unregistered.setdefault(_bead_prefix(iid), []).append(iid)
        else:
            other.append(p)

    lines = []
    for prefix, iids in sorted(unregistered.items()):
        triplet = _agreed_triplet(issues_by_id, iids) or "<provider>/<org>/<repo>"
        lines.append(
            f"prefix '{prefix}' not registered ({len(iids)} issues affected) — "
            f"fix: {config.BINARY_ALIAS} rig add {triplet} --prefix={prefix}"
        )
    lines.extend(other)
    return lines


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

    issues, problems, db_ok = _issues_and_problems(cfg)
    if problems:
        typer.echo("✗ issue/label problems:")
        for p in _render_problems(issues, problems):
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
