"""Consumer-side execution of graph-selected attestation keys."""

from __future__ import annotations

import fnmatch
import json
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence

import typer

from . import config_work_settings as config
from . import validation_ledger
from .adapters.impact_pants import PantsImpactBackend
from .bootstrap.impact import attest_keys, impact_resolver

Runner = Callable[[str], int]

# A resolver failure is neither a key verdict (75/UNKNOWN) nor a release/decision answer
# (1=refuse, 2=route/half-done, 3=unmeasurable). 76 is sysexits EX_PROTOCOL: the configured
# impact-analysis protocol did not produce an answer strict mode may act on.
UNRESOLVED_IMPACT_EXIT = 76


def warn_impact_fallback(reason: str) -> None:
    """Make fail-closed expansion visible even in long validation logs."""
    typer.echo("", err=True)
    typer.echo("!!! WARNING: IMPACT RESOLUTION FALLBACK !!!", err=True)
    typer.echo(f"    {reason}", err=True)
    typer.echo("    Selective carry-forward is disabled; running every attestation key.", err=True)
    typer.echo("", err=True)


def error_unresolved_impact(reason: str) -> None:
    """Report strict-mode refusal without claiming that the all-key fallback will run."""
    typer.echo("", err=True)
    typer.echo("!!! ERROR: IMPACT RESOLUTION UNRESOLVED (STRICT MODE) !!!", err=True)
    typer.echo(f"    {reason}", err=True)
    typer.echo("    No attestation key ran; strict mode refuses the all-key fallback.", err=True)
    typer.echo("", err=True)


def configured(cfg, entry) -> bool:
    return bool(config.attest_config(cfg, entry).keys)


def all_keys_green(entry, cfg, rev: str) -> bool:
    """Return whether every configured key proves ``rev`` green.

    Optionality controls whether a lifecycle boundary may proceed; it never turns missing or
    red evidence into proof that the aggregate full-gate command passed.
    """
    keys = attest_keys(config.attest_config(cfg, entry))
    if not keys or any(key.is_disabled() for key in keys):
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


#: Three-way triage verdicts, matching the vocabulary this codebase already uses for an
#: honest non-answer (ComplexityResult's UNKNOWN, and exit 75 here). UNKNOWN is a
#: first-class answer, not an error: "I cannot tell" differs from "no", and a
#: fall-through filter must be able to say so rather than guess.
TRIVIAL_YES = "yes"
TRIVIAL_NO = "no"
TRIVIAL_UNKNOWN = "unknown"


def path_verdict(attest, changed_paths: Sequence[str]) -> str:
    """Does the configured glob policy call this change trivial?

    UNKNOWN when the policy is unconfigured or there is nothing to judge -- distinct from NO,
    so an unconfigured hive is never mistaken for one that considered the change and
    rejected it.
    """
    policy = getattr(attest, "trivial", None)
    if policy is None or not policy.enabled or not policy.paths or not policy.keys:
        return TRIVIAL_UNKNOWN
    if not changed_paths:
        return TRIVIAL_UNKNOWN
    if all(
        any(fnmatch.fnmatch(path, pattern) for pattern in policy.paths) for path in changed_paths
    ):
        return TRIVIAL_YES
    return TRIVIAL_NO


def trivial_selection(attest, changed_paths: Sequence[str], keys: Sequence, triage=None):
    """``(selected_keys | None, record)`` -- the keys a trivial change runs, and why.

    TWO INDEPENDENT JUDGES MUST AGREE before anything is skipped:

      * the configured path globs, and
      * an optional semantic ``triage(changed_paths) -> yes | no | unknown``.

    Neither can skip alone. A glob list that is too BROAD is vetoed by a triage answering
    ``no``; a list that is too NARROW simply never fires, and the record says so, which is the
    signal that the config drifted. Misconfiguration is the failure mode this guards, so the
    guard cannot itself be a single config value.

    Every uncertain outcome -- UNKNOWN from either judge, a triage that raises, an empty
    selection -- resolves to the normal route. Running a key that was not needed costs minutes;
    skipping one that was needed ships a regression.
    """
    paths = tuple(changed_paths)
    pv = path_verdict(attest, paths)
    record = {
        "path_verdict": pv,
        "triage_verdict": TRIVIAL_UNKNOWN,
        "applied": False,
        "changed_count": len(paths),
    }

    tv = TRIVIAL_UNKNOWN
    if triage is not None and pv in (TRIVIAL_YES, TRIVIAL_NO):
        try:
            tv = triage(paths)
        except Exception as exc:  # noqa: BLE001 - any failure means "cannot tell"
            tv = TRIVIAL_UNKNOWN
            record["triage_error"] = f"{type(exc).__name__}: {exc}"[:200]
        if tv not in (TRIVIAL_YES, TRIVIAL_NO, TRIVIAL_UNKNOWN):
            record["triage_error"] = f"unknown triage verdict {tv!r}"
            tv = TRIVIAL_UNKNOWN
    record["triage_verdict"] = tv

    # The two disagreement cases are the ones worth naming, because each is a config bug.
    if pv == TRIVIAL_YES and tv == TRIVIAL_NO:
        record["disagreement"] = (
            "globs call this trivial but triage does not — globs may be too broad"
        )
        return None, record
    if pv == TRIVIAL_NO and tv == TRIVIAL_YES:
        record["disagreement"] = (
            "triage calls this trivial but globs do not — globs may be too narrow"
        )
        return None, record
    if pv != TRIVIAL_YES:
        return None, record

    policy = attest.trivial
    wanted = set(policy.keys)
    selected = tuple(key for key in keys if key.name in wanted)
    if not selected:
        # Skipping everything is the one outcome this policy must never produce.
        record["error"] = "trivial.keys matched no configured key"
        return None, record
    record["applied"] = True
    record["ran"] = sorted(key.name for key in selected)
    return selected, record


def _changed_paths_for_policy(repo: str, base_rev: str, head_rev: str) -> tuple[str, ...] | None:
    """Changed paths for the policy check, or ``None`` when git cannot answer.

    ``None`` means the policy does not apply, so an unreadable diff runs the full route.
    """
    try:
        from .adapters.impact_git import GitTreeDiff

        diff = GitTreeDiff()
        base_tree = diff.tree_of(repo, base_rev)
        head_tree = diff.tree_of(repo, head_rev)
        return tuple(change.path for change in diff.changed_paths(repo, base_tree, head_tree))
    except (OSError, RuntimeError, ValueError):
        return None


def semantic_selection(attest, repo: str, base_rev: str, head_rev: str, keys: Sequence):
    """Return ``(selected | None, record)`` from a fail-closed semantic adviser.

    This is policy, never impact evidence.  A valid answer must partition every active key;
    otherwise the normal route remains intact.  Refusing an empty selection prevents an adviser
    from becoming a test-free gate even when its probabilities are badly calibrated.
    """
    policy = getattr(attest, "semantic", None)
    record = {"applied": False}
    if policy is None or not policy.enabled:
        return None, record
    try:
        command = [*shlex.split(policy.command), "--base", base_rev, "--head", head_rev]
        result = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=policy.timeout_seconds,
        )
        if result.returncode != 0:
            raise RuntimeError(f"selector exited {result.returncode}")
        payload = json.loads(result.stdout)
        if payload.get("schema") != "jevwrap/select/1" or payload.get("error"):
            raise RuntimeError(str(payload.get("error") or "unsupported selector schema"))
        affected = payload.get("affected")
        unaffected = payload.get("unaffected")
        if not isinstance(affected, list) or not isinstance(unaffected, list):
            raise RuntimeError("selector must return affected and unaffected lists")
        expected = {key.name for key in keys}
        affected_set = set(affected)
        unaffected_set = set(unaffected)
        if affected_set & unaffected_set or affected_set | unaffected_set != expected:
            raise RuntimeError("selector answer does not partition the active attest keys")
        selected = tuple(key for key in keys if key.name in affected_set)
        if not selected:
            raise RuntimeError("selector refused: no attest key selected")
        record.update(applied=True, ran=sorted(affected_set), skipped=sorted(unaffected_set))
        return selected, record
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return None, record


def run(
    entry,
    cfg,
    *,
    base_rev: str,
    head_rev: str,
    runner: Runner,
    repo_path: str | None = None,
    full: bool = False,
    receipt_override=None,
) -> int:
    """Resolve impact, carry proven greens, run invalidated keys, and aggregate policy.

    Exit 75 is UNKNOWN.  It blocks required keys and is tolerated for optional keys; every
    other non-zero result blocks regardless of policy.
    """
    attest = config.attest_config(cfg, entry)
    keys = attest_keys(attest)
    if not keys:
        return runner(config.validate_cmd(cfg, entry))
    active_keys = tuple(key for key in keys if not key.is_disabled())
    for key in keys:
        if key.is_disabled():
            expiry = f" (until {key.disabled_until.isoformat()})" if key.disabled_until else ""
            typer.echo(f"  · {key.name}: DISABLED — {key.disabled_reason}{expiry}")
    from . import registry

    repo = repo_path or str(registry.hive_dir(entry))
    try:
        pants = PantsImpactBackend(repo)
        backends = {"pants": pants}
    except (OSError, KeyError, ValueError):
        backends = {}
    changed_for_policy = _changed_paths_for_policy(repo, base_rev, head_rev)
    semantic_selected, semantic_record = semantic_selection(
        attest, repo, base_rev, head_rev, active_keys
    )
    if semantic_record.get("error"):
        typer.echo(
            "  · semantic key selection unavailable: "
            f"{semantic_record['error']} — running normal route"
        )
    if semantic_selected is not None:
        chosen = {key.name for key in semantic_selected}
        for key in active_keys:
            if key.name not in chosen:
                typer.echo(
                    f"  · {key.name}: SKIPPED — semantic selection policy "
                    f"(no proof carried; this revision is not fully attested)"
                )
        active_keys = semantic_selected
    selected, trivial_record = (
        trivial_selection(attest, changed_for_policy, active_keys)
        if changed_for_policy is not None
        else (None, {"path_verdict": TRIVIAL_UNKNOWN, "applied": False})
    )
    if trivial_record.get("disagreement"):
        typer.echo(f"  · trivial-change triage: {trivial_record['disagreement']}")
    if trivial_record.get("triage_error"):
        typer.echo(f"  · trivial-change triage unavailable: {trivial_record['triage_error']}")
    if selected is not None:
        chosen = {key.name for key in selected}
        for key in active_keys:
            if key.name not in chosen:
                typer.echo(
                    f"  · {key.name}: SKIPPED — trivial-change policy "
                    f"(no proof carried; this revision is not fully attested)"
                )
        active_keys = selected

    resolver = impact_resolver(attest, backends=backends)
    if full:
        from .adapters.impact_git import GitTreeDiff
        from .modules.work.application.impact import NativeFullResolver

        resolver = NativeFullResolver(GitTreeDiff())
    receipt = receipt_override or resolver.resolve(repo, base_rev, head_rev, active_keys)
    if receipt.fallback_reason:
        if attest.impact.on_unresolved == "strict":
            error_unresolved_impact(receipt.fallback_reason)
            return UNRESOLVED_IMPACT_EXIT
        warn_impact_fallback(receipt.fallback_reason)

    by_name = {key.name: key for key in active_keys}
    outcomes: dict[str, int | None] = {}
    validation_started = time.perf_counter()

    def run_key(key) -> tuple[int, float]:
        started = time.perf_counter()
        return runner(key.cmd), time.perf_counter() - started

    def reuse_exact_tree(key) -> bool:
        """Reuse only an already-qualifying verdict for this exact tree and command."""
        try:
            verdict = validation_ledger.key_verdict(entry, head_rev, key, cfg=cfg)
        except (KeyError, OSError, ValueError):
            return False
        if verdict.state == validation_ledger.KeyVerdictState.CARRIED or (
            verdict.state == validation_ledger.KeyVerdictState.CURRENT
            and validation_ledger.is_qualifying_green(verdict.record or {})
        ):
            typer.echo(f"  ✓ {key.name}: exact-tree verdict reused")
            outcomes[key.name] = 0
            return True
        return False

    for name in receipt.unaffected_keys:
        key = by_name[name]
        if reuse_exact_tree(key):
            continue
        carried = validation_ledger.carry_key_verdict(entry, key, receipt, cfg=cfg)
        verdict = validation_ledger.key_verdict(entry, head_rev, key, cfg=cfg)
        if carried or verdict.state == validation_ledger.KeyVerdictState.CARRIED:
            record = verdict.record or {}
            typer.echo(
                f"  ✓ {name}: carried from {record.get('source_tree', receipt.base_tree)[:12]} "
                f"via receipt {receipt.digest[:12]}"
            )
            outcomes[name] = 0
        elif key.policy == "required":
            rc, elapsed = run_key(key)
            outcomes[name] = rc
            state = "ran green" if rc == 0 else "unknown" if rc == 75 else f"ran red (exit {rc})"
            typer.echo(
                f"  {'✓' if rc == 0 else '?' if rc == 75 else '✗'} {name}: {state} "
                f"(no qualifying source verdict) [{elapsed:.3f}s]"
            )
        else:
            typer.echo(f"  ? {name}: unknown (no qualifying source verdict)")
            outcomes[name] = None

    for name in receipt.invalidated_keys:
        key = by_name[name]
        if reuse_exact_tree(key):
            continue
        rc, elapsed = run_key(key)
        outcomes[name] = rc
        state = "ran green" if rc == 0 else "unknown" if rc == 75 else f"ran red (exit {rc})"
        typer.echo(
            f"  {'✓' if rc == 0 else '?' if rc == 75 else '✗'} {name}: {state} ({elapsed:.3f}s)"
        )

    blocked = False
    for key in active_keys:
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
    typer.echo(f"  selective validation total: {time.perf_counter() - validation_started:.3f}s")
    return 1 if blocked else 0


__all__ = [
    "UNRESOLVED_IMPACT_EXIT",
    "all_keys_green",
    "configured",
    "error_unresolved_impact",
    "run",
    "warn_impact_fallback",
]
