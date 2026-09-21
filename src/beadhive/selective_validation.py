"""Consumer-side execution of graph-selected attestation keys."""

from __future__ import annotations

from collections.abc import Callable

import typer

from . import config_work_settings as config
from . import validation_ledger
from .adapters.impact_pants import PantsImpactBackend
from .bootstrap.impact import attest_keys, impact_resolver

Runner = Callable[[str], int]


def warn_impact_fallback(reason: str) -> None:
    """Make fail-closed expansion visible even in long validation logs."""
    typer.echo("", err=True)
    typer.echo("!!! WARNING: IMPACT RESOLUTION FALLBACK !!!", err=True)
    typer.echo(f"    {reason}", err=True)
    typer.echo("    Selective carry-forward is disabled; running every attestation key.", err=True)
    typer.echo("", err=True)


def configured(cfg, entry) -> bool:
    return bool(config.attest_config(cfg, entry).keys)


def all_keys_green(entry, cfg, rev: str) -> bool:
    """Return whether every configured key proves ``rev`` green.

    Optionality controls whether a lifecycle boundary may proceed; it never turns missing or
    red evidence into proof that the aggregate full-gate command passed.
    """
    keys = attest_keys(config.attest_config(cfg, entry))
    if not keys:
        return False
    verdicts = validation_ledger.key_verdicts(entry, rev, keys, cfg=cfg)
    return all(
        verdict.state == validation_ledger.KeyVerdictState.CARRIED
        or (
            verdict.state == validation_ledger.KeyVerdictState.CURRENT
            and validation_ledger.is_qualifying_green(verdict.record or {})
        )
        for verdict in verdicts.values()
    )


def run(
    entry,
    cfg,
    *,
    base_rev: str,
    head_rev: str,
    runner: Runner,
    repo_path: str | None = None,
    full: bool = False,
) -> int:
    """Resolve impact, carry proven greens, run invalidated keys, and aggregate policy.

    Exit 75 is UNKNOWN.  It blocks required keys and is tolerated for optional keys; every
    other non-zero result blocks regardless of policy.
    """
    attest = config.attest_config(cfg, entry)
    keys = attest_keys(attest)
    if not keys:
        return runner(config.validate_cmd(cfg, entry))
    from . import registry

    repo = repo_path or str(registry.hive_dir(entry))
    try:
        pants = PantsImpactBackend(repo)
        backends = {"pants": pants}
    except (OSError, KeyError, ValueError):
        backends = {}
    resolver = impact_resolver(attest, backends=backends)
    if full:
        from .adapters.impact_git import GitTreeDiff
        from .modules.work.application.impact import NativeFullResolver

        resolver = NativeFullResolver(GitTreeDiff())
    receipt = resolver.resolve(repo, base_rev, head_rev, keys)
    if receipt.fallback_reason:
        warn_impact_fallback(receipt.fallback_reason)

    by_name = {key.name: key for key in keys}
    outcomes: dict[str, int | None] = {}
    for name in receipt.unaffected_keys:
        key = by_name[name]
        carried = validation_ledger.carry_key_verdict(entry, key, receipt, cfg=cfg)
        verdict = validation_ledger.key_verdict(entry, head_rev, key, cfg=cfg)
        if carried or verdict.state == validation_ledger.KeyVerdictState.CARRIED:
            record = verdict.record or {}
            typer.echo(
                f"  ✓ {name}: carried from {record.get('source_tree', receipt.base_tree)[:12]} "
                f"via receipt {receipt.digest[:12]}"
            )
            outcomes[name] = 0
        else:
            typer.echo(f"  ? {name}: unknown (no qualifying source verdict)")
            outcomes[name] = None

    for name in receipt.invalidated_keys:
        key = by_name[name]
        rc = runner(key.cmd)
        outcomes[name] = rc
        state = "ran green" if rc == 0 else "unknown" if rc == 75 else f"ran red (exit {rc})"
        typer.echo(f"  {'✓' if rc == 0 else '?' if rc == 75 else '✗'} {name}: {state}")

    blocked = False
    for key in keys:
        outcome = outcomes.get(key.name)
        if outcome == 0:
            continue
        if outcome is None or outcome == 75:
            if key.policy == "required":
                blocked = True
            else:
                typer.echo(f"  · {key.name}: not required (optional unknown)")
        else:
            blocked = True
    return 1 if blocked else 0


__all__ = ["all_keys_green", "configured", "run", "warn_impact_fallback"]
