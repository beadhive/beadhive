"""Post-create worktree init rules: injected rule DATA plus the ``CommandRunner`` PORT
(bh-qdezo.6).

Moved off ``worktree_verify.py``'s argv-era ``impl__rules`` / ``impl_run_init`` /
``impl__init_rules_fingerprint`` / ``impl__read_init_rules_fingerprint`` /
``impl_record_init_rules`` / ``impl_warn_init_rules_drift``: rule order, ``if_exists``
evaluation, verify-only filtering, and the failure-summary text are unchanged byte-for-byte.
Root's facade still resolves ``worktrees.init`` + the hive's ``worktree_init`` (config reads
stay root-only — this package imports no config implementation) and calls through here with
the already-merged rule list plus a :class:`~beadhive_worktrees.contracts.init_ports.CommandRunner`
adapter over ``run``/``missing_binary``/``cache_locality``.

The rule STAMP stays exactly what it always was — Git worktree-local config (``git config
--worktree``) — just executed through the same port instead of a direct ``run()`` call, so
package-local tests exercise it with a fake runner instead of a real git subprocess.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from ..contracts.init_ports import CommandRunner

_FINGERPRINT_VERSION = "v1"
INIT_RULES_CONFIG_KEY = "beadhive.initRulesFingerprint"


def rules_fingerprint(rules: Iterable[Mapping]) -> str:
    """Stable identity for the ordered effective seat-init rule set.

    The order is part of the contract (global rules precede hive rules), while mapping key
    order is not. The digest is versioned so a future canonicalization change reports drift
    once instead of silently treating an old stamp as current.
    """
    payload = json.dumps(list(rules), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"{_FINGERPRINT_VERSION}:{digest}"


def run_init_rules(
    rules: Iterable[Mapping],
    path: Path,
    *,
    verify_only: bool = False,
    runner: CommandRunner,
    report: Callable[[str], None],
    warn: Callable[[str], None],
) -> bool:
    """Evaluate ``rules`` in ``path``: run each whose ``if_exists`` glob matches (or has none).

    Best-effort — a failing/absent command warns (via ``warn``) and evaluation continues.
    ``verify_only`` filters to rules flagged ``{verify: true}`` (the ``clean_checkout`` pass).
    Returns ``True`` iff every attempted rule succeeded.
    """
    failed: list[str] = []
    for rule in rules:
        rule = rule or {}
        cmd = rule.get("run")
        if not cmd:
            continue
        if verify_only and not rule.get("verify"):
            continue
        cond = rule.get("if_exists")
        if cond and not any(path.glob(cond)):
            continue
        report(f"  → {cmd}")
        argv = shlex.split(cmd)
        outcome = runner.run(argv, cwd=path)
        for note in outcome.notes:
            report(note)
        for line in outcome.warnings:
            warn(line)
        if outcome.missing:
            warn(f"  ⚠ init: command not found: {cmd}")
            failed.append(cmd)
            continue
        if outcome.returncode != 0:
            warn(f"  ⚠ init: '{cmd}' exited {outcome.returncode}")
            failed.append(cmd)
    if failed:
        warn(
            f"  ⚠ init: {len(failed)} optional provisioning rule(s) failed and were skipped "
            f"(worktree is otherwise ready): {'; '.join(failed)}"
        )
    return not failed


def read_recorded_fingerprint(path: Path, *, runner: CommandRunner) -> str | None:
    """Read this Git worktree incarnation's init-rule stamp without mutating it."""
    outcome = runner.run(
        ["git", "-C", str(path), "config", "--worktree", "--get", INIT_RULES_CONFIG_KEY],
        cwd=path,
    )
    value = outcome.stdout.strip()
    return value if outcome.returncode == 0 and value else None


def record_fingerprint(
    path: Path,
    rules: Iterable[Mapping],
    *,
    runner: CommandRunner,
    warn: Callable[[str], None],
) -> bool:
    """Persist the current rule fingerprint in Git-owned per-worktree config.

    The stamp follows a linked worktree across ``git worktree move`` and disappears with that
    incarnation. A write failure is non-fatal, matching init's best-effort contract; leaving
    the stamp absent makes the next reuse warn instead of falsely claiming provisioning is
    current.
    """
    enabled = runner.run(
        ["git", "-C", str(path), "config", "extensions.worktreeConfig", "true"], cwd=path
    )
    if enabled.returncode != 0:
        warn(f"  ⚠ init: could not record the provisioning rule set for {path}")
        return False
    written = runner.run(
        [
            "git",
            "-C",
            str(path),
            "config",
            "--worktree",
            INIT_RULES_CONFIG_KEY,
            rules_fingerprint(rules),
        ],
        cwd=path,
    )
    if written.returncode != 0:
        warn(f"  ⚠ init: could not record the provisioning rule set for {path}")
        return False
    return True


def warn_drift(
    path: Path,
    rules: Iterable[Mapping],
    *,
    runner: CommandRunner,
    warn: Callable[[str], None],
    reinit_hint: str,
) -> bool:
    """Warn when an existing seat was provisioned under a different rule set.

    Legacy worktrees have no stamp. They warn only when rules are currently configured: an
    unstamped checkout with no provisioning to miss is byte-compatible with the old quiet
    path. Never re-runs operator commands or modifies the checkout.
    """
    rules = list(rules)
    recorded = read_recorded_fingerprint(path, runner=runner)
    current = rules_fingerprint(rules)
    if recorded == current or (recorded is None and not rules):
        return False
    warn(
        "WARNING: worktree init rules changed since this checkout was provisioned; "
        f"re-attaching without re-running them: {path}\n"
        f"  → {reinit_hint}"
    )
    return True


__all__ = [
    "INIT_RULES_CONFIG_KEY",
    "read_recorded_fingerprint",
    "record_fingerprint",
    "rules_fingerprint",
    "run_init_rules",
    "warn_drift",
]
