"""Consumer-side execution of graph-selected attestation keys."""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import typer

from . import config_work_settings as config
from . import identity, validation_ledger, validation_records
from .bootstrap.impact import attest_keys, impact_resolver
from .modules.config.contracts import GATE_ALL_KEY_PHASES

Runner = Callable[[str], int]

# A resolver failure is neither a key verdict (75/UNKNOWN) nor a release/decision answer
# (1=refuse, 2=route/half-done, 3=unmeasurable). 76 is sysexits EX_PROTOCOL: the configured
# impact-analysis protocol did not produce an answer strict mode may act on.
UNRESOLVED_IMPACT_EXIT = 76
FULL_GATE_PHASES = ("molecule", "merge-main", "push-main", "postland")
#: ``work.validate.precheck``: an optional cheap command (e.g. a generated-evidence currency
#: check) run before any key, so a tree that cannot pass fails in seconds instead of after a
#: 12-15 minute lane.  Unset (the default) runs nothing extra.
PRECHECK_PHASE = "precheck"
#: Opt-in: stop launching further keys after the first blocking result.  Off by default, which
#: keeps today's behaviour of running and reporting every invalidated key.
FAIL_FAST_ENV = "BH_VALIDATION_FAIL_FAST"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def fail_fast_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``BH_VALIDATION_FAIL_FAST`` opts this process into fail-fast key execution."""
    value = (os.environ if environ is None else environ).get(FAIL_FAST_ENV, "")
    return value.strip().lower() in _TRUTHY


def precheck_cmd(cfg, entry) -> str:
    """The configured ``work.validate.precheck`` command, or ``""`` when none is set."""
    per = config.work_value(cfg, entry, "validate", {}) or {}
    if not isinstance(per, Mapping):
        return ""
    return str(per.get(PRECHECK_PHASE) or "").strip()


def historical_costs(entry, keys: Sequence) -> dict[str, float]:
    """Key name -> median wall seconds of its command in this hive's validation ledger.

    A scheduling hint only: any doubt (no hive, unreadable ledger) answers ``{}``, which leaves
    the configured key order untouched.
    """
    if not entry or not keys:
        return {}
    names_by_hash: dict[str, list[str]] = {}
    for key in keys:
        names_by_hash.setdefault(validation_ledger.cmd_hash(key.cmd), []).append(key.name)
    try:
        from . import registry

        durations = validation_records.command_durations(registry.hive_dir(entry), names_by_hash)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return {}
    return {
        name: seconds
        for command_hash, seconds in durations.items()
        for name in names_by_hash.get(command_hash, ())
    }


def cheapest_first(names: Sequence[str], costs: Mapping[str, float]) -> tuple[str, ...]:
    """Order keys so the cheapest measured ones run first.

    A cheap red key then surfaces before an expensive lane is even started.  Keys with no
    measurement follow the measured ones in their configured order, so a hive with no history
    keeps exactly its configured order.
    """
    position = {name: index for index, name in enumerate(names)}
    return tuple(
        sorted(
            names,
            key=lambda name: (name not in costs, costs.get(name, 0.0), position[name]),
        )
    )


def _blocks(key, rc) -> bool:
    """Whether one key result blocks the aggregate (see the policy loop in :func:`run`)."""
    return rc != 0 and not (rc == 75 and key.policy != "required")


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


_TREE_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def attested_green(entry, cfg, rev: str, phase: str) -> bool:
    """The one aggregate predicate: does the ledger prove every active key green for ``rev``'s tree?

    Attest-keys ADR 3.4. All of these must hold, and any doubt answers ``False``:

    - ``rev`` resolves to a tree (an unresolved tree could only match a literal);
    - a key catalog is configured and no key is disabled (a disabled key is never proof);
    - every key is current-green (qualifying) or validly carried at that exact tree;
    - no key is current-red at that tree;
    - :func:`catalog_ok` (the opt-in drift check) holds;
    - ``work.always_run`` passes, as :func:`validation_ledger.green_verdict` requires.

    Optionality controls whether a lifecycle boundary may proceed; it never turns missing or
    red evidence into proof. Every exception, including an unreadable ledger, is ``False``.
    ``phase`` names the boundary asking; every phase requires every active key, which is the
    ADR's hard limit for ``push-main``/``release``/``postland`` and the safe reading elsewhere.
    For those three phases the profile must also resolve to every active key
    (``P(phase) == active(C)``); :func:`config.gate_profile` pins them to ``all``, so a
    configured subset there can only ever answer ``False`` here.
    """
    try:
        return _attested_green(entry, cfg, rev, phase)
    except Exception as exc:  # fail closed: an unreadable predicate input is never a pass
        typer.echo(
            f"attested_green({phase}) failed closed: {type(exc).__name__}: {exc}"[:300], err=True
        )
        return False


def _attested_green(entry, cfg, rev: str, phase: str) -> bool:
    if not rev or not phase:
        return False
    # The ADR 3.4 `model == keys` conjunct is deliberately NOT here yet: it would refuse every
    # legacy-model `release attest --if-needed` and bh-72ubb's push acceptance. It is deferred
    # to the cutover slice, bh-i3nc3.
    if phase in GATE_ALL_KEY_PHASES and config.gate_profile(cfg, entry, phase) is not None:
        return False  # P(phase) == active(C): a main-reaching profile must be every active key
    tree = validation_ledger.tree_of(entry, rev)
    if not _TREE_ID.fullmatch(tree or ""):
        return False
    keys = attest_keys(config.attest_config(cfg, entry))
    if not keys or any(key.is_disabled() for key in keys):
        return False
    verdicts = validation_ledger.key_verdicts(entry, tree, keys, cfg=cfg)
    if set(verdicts) != {key.name for key in keys}:
        return False
    states = validation_ledger.KeyVerdictState
    for verdict in verdicts.values():
        if verdict.state == states.CURRENT and not validation_ledger.is_qualifying_green(
            verdict.record or {}
        ):
            return False  # current-red (or malformed): a real result no carry can outvote
        if verdict.state not in (states.CURRENT, states.CARRIED):
            return False
    return catalog_ok(entry, cfg, tree) and validation_ledger.always_run_ok(entry, cfg)


def catalog_payload(entry, cfg) -> list[dict]:
    """The live HQ catalog as the drift-check command's stdin: name, cmd, enabled, by name."""
    keys = config.attest_config(cfg, entry).keys
    return sorted(
        ({"name": key.name, "cmd": key.cmd, "enabled": key.enabled} for key in keys),
        key=lambda item: item["name"],
    )


def catalog_digest(payload: Sequence[Mapping]) -> str:
    """Stable digest of the catalog payload; any HQ catalog edit changes it."""
    canonical = json.dumps(list(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _drift_cache_cmd(digest: str, drift_check: str) -> str:
    """The ledger identity of one drift verdict: (tree, catalog digest, drift-check cmd_hash)."""
    return f"catalog-drift:{digest}:{validation_ledger.cmd_hash(drift_check)}"


def _archive_checkout(entry, rev: str, target: Path) -> None:
    """Materialise ``rev``'s tree under ``target`` without touching the hive checkout."""
    from . import registry

    archive = subprocess.run(
        ["git", "-C", str(registry.hive_dir(entry)), "archive", "--format=tar", rev],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")


def catalog_drift(entry, cfg, rev: str, checkout: Path | str | None = None) -> str | None:
    """Why the live HQ catalog cannot be trusted at ``rev``, or ``None`` when it matches.

    Opt-in guard (attest-keys ADR, 3.6). With ``work.attest.drift_check`` unset and the gate
    model not ``keys`` nothing runs and nothing changes. Exit 0 from the opaque command is "no
    drift"; a nonzero exit, a timeout, an unlaunchable command, or an unset command under
    ``model: keys`` is drift. Only a clean verdict is cached, keyed by (tree, catalog digest,
    drift-check cmd_hash), so a red or timed-out check always re-runs.
    """
    attest = config.attest_config(cfg, entry)
    command = attest.drift_check
    if command is None:
        if config.layered(cfg, entry, "work.gate", "model", "legacy") == "keys":
            return "drift check unset: work.attest.drift_check is required under model: keys"
        return None
    payload = catalog_payload(entry, cfg)
    digest = catalog_digest(payload)
    cache_cmd = _drift_cache_cmd(digest, command)
    if validation_ledger.green_verdict(entry, rev, cache_cmd, cfg=cfg) is not None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="bh-drift-") as scratch:
            cwd = checkout
            if cwd is None:
                cwd = Path(scratch)
                _archive_checkout(entry, rev, cwd)
            result = subprocess.run(
                shlex.split(command),
                cwd=cwd,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
                timeout=attest.drift_check_timeout_seconds,
            )
    except subprocess.TimeoutExpired:
        return f"drift check timed out after {attest.drift_check_timeout_seconds:g}s"
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        return f"drift check could not run: {type(exc).__name__}: {exc}"[:300]
    validation_ledger.record(
        entry, rev, cache_cmd, result.returncode, cfg=cfg, phase="catalog-drift"
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return f"drift check exited {result.returncode}: {detail}"[:2000]
    return None


def catalog_ok(entry, cfg, rev: str, checkout: Path | str | None = None) -> bool:
    """True when the live HQ catalog matches the repo at ``rev`` (or no check is configured).

    Fails closed: every drift reason from :func:`catalog_drift` is printed and reads False.
    """
    try:
        reason = catalog_drift(entry, cfg, rev, checkout)
    except Exception as exc:  # fail closed: an unreadable check is drift, never a pass
        reason = f"drift check failed: {type(exc).__name__}: {exc}"[:300]
    if reason is not None:
        typer.echo(f"attest catalog drift: {reason}", err=True)
    return reason is None


def record_full_gate_keys(entry, cfg, rev: str, cmd: str, rc: int) -> None:
    """Expand one successful configured full-gate run into current verdicts for its key catalog.

    The catalog recipes are checked as an exact partition of the full gate.  Recording their
    opaque commands here lets later path receipts carry unchanged keys without rerunning them.
    Red runs and ordinary fast/submission commands never create per-key proof.
    """
    if rc != 0:
        return
    attest = config.attest_config(cfg, entry)
    keys = attest_keys(attest)
    if not keys:
        return
    validation = (entry.get("work") or {}).get("validate") or {}
    full_commands = {
        str(validation[phase]).strip() for phase in FULL_GATE_PHASES if phase in validation
    }
    if cmd.strip() not in full_commands:
        return
    # Under `work.gate.model: keys` the partition this expansion assumes is only trusted while
    # the runtime drift check holds; a drifted catalog (including the evaluator's own drift
    # fallback to the legacy command) records no key proof.
    if config.gate_model(cfg, entry) == "keys" and not catalog_ok(entry, cfg, rev):
        return
    for key in keys:
        if not key.is_disabled():
            validation_ledger.record(entry, rev, key.cmd, 0, cfg=cfg, phase="full-gate-key")


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


def _root_receipt_doubt(root_receipt, root, head_tree: str) -> str:
    """Why a root-to-head receipt cannot carry anything, or ``""`` when it may.

    The receipt must be a real, non-degraded answer about exactly the root tree and exactly the
    head tree; unowned paths and global inputs anywhere in the span invalidate every key.
    """
    if root_receipt is None:
        return "resolver gave no answer"
    if root_receipt.fallback_reason:
        return f"resolver fallback: {root_receipt.fallback_reason}"
    if root_receipt.base_tree != root.tree or root_receipt.head_tree != head_tree:
        return "receipt does not span this root to the head tree"
    if root_receipt.unowned_paths:
        return f"unowned paths in the span: {', '.join(root_receipt.unowned_paths[:3])}"
    if root_receipt.global_inputs_hit:
        return f"global inputs in the span: {', '.join(root_receipt.global_inputs_hit[:3])}"
    return ""


def high_water_carry(
    entry,
    cfg,
    *,
    resolver,
    repo: str,
    head_rev: str,
    receipt,
    keys: Sequence,
) -> tuple[str, ...]:
    """Carry each of ``keys`` from its root run, returning the names now carried at the head.

    Attest-keys ADR, section 3.5.  ``keys`` are unaffected by ``receipt`` (base to head) but have
    no qualifying run on the base to carry from, typically because the base itself only holds a
    carry.  For each key the candidate roots come from
    :func:`validation_ledger.carry_root_candidates`; every distinct root is resolved once,
    root to head directly and shared across keys, and a key carries only when that whole-span
    receipt lists it unaffected.  :func:`validation_ledger.carry_key_verdict` then re-reads the
    root's current green run as the source and keeps its ``source_tree``, ``source_run_id`` and
    ``at``, so no age or TTL is refreshed.

    Every doubt costs a run, never proof: a lookup error drops the key's candidates, a resolver
    error, fallback, unowned path, global input or tree mismatch drops the root, and a key left
    without a carry stays pending.
    """
    head_tree = receipt.head_tree
    if not head_tree or not receipt.base_tree or receipt.is_fallback:
        return ()
    max_roots = config.carry_max_roots(cfg, entry)
    candidates: dict[str, tuple] = {}
    for key in keys:
        try:
            candidates[key.name] = validation_ledger.carry_root_candidates(
                entry,
                receipt.base_tree,
                key,
                max_roots=max_roots,
                exclude=(head_tree,),
                cfg=cfg,
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            candidates[key.name] = ()

    # One resolve per distinct root, asked about every key that lists it.
    wanted: dict[str, list] = {}
    for key in keys:
        for root in candidates[key.name]:
            wanted.setdefault(root.tree, [])
            if key not in wanted[root.tree]:
                wanted[root.tree].append(key)
    receipts: dict[str, object] = {}

    def root_receipt(root):
        if root.tree not in receipts:
            try:
                answer = resolver.resolve(repo, root.rev, head_rev, wanted[root.tree])
            except Exception as exc:  # noqa: BLE001 - a root that cannot be resolved is skipped
                typer.echo(
                    f"  · high-water root {root.tree[:12]} skipped: {type(exc).__name__}: {exc}"[
                        :300
                    ]
                )
                answer = None
            doubt = _root_receipt_doubt(answer, root, head_tree)
            if answer is not None and doubt:
                typer.echo(f"  · high-water root {root.tree[:12]} skipped: {doubt}"[:300])
            receipts[root.tree] = None if doubt else answer
        return receipts[root.tree]

    carried: list[str] = []
    for key in keys:
        for root in candidates[key.name]:
            proof = root_receipt(root)
            if proof is None or not proof.is_unaffected(key.name):
                continue
            if not validation_ledger.carry_key_verdict(entry, key, proof, cfg=cfg):
                continue
            try:
                found = validation_ledger.key_verdict(entry, head_rev, key, cfg=cfg)
            except (KeyError, OSError, ValueError):
                continue
            record = found.record or {}
            if (
                found.state != validation_ledger.KeyVerdictState.CARRIED
                or record.get("source_tree") != root.tree
                or record.get("receipt_digest") != proof.digest
            ):
                continue
            typer.echo(
                f"  ✓ {key.name}: carried from root {root.tree[:12]} "
                f"(run {root.run_id}, high-water past base {receipt.base_tree[:12]}) "
                f"via receipt {proof.digest[:12]}"
            )
            carried.append(key.name)
            break
    return tuple(carried)


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
    phase: str | None = None,
    main_gate: bool = False,
    legacy_cmd: str | None = None,
) -> int:
    """Resolve impact, carry proven greens, run invalidated keys, and aggregate policy.

    Exit 75 is UNKNOWN.  It blocks required keys and is tolerated for optional keys; every
    other non-zero result blocks regardless of policy.

    ``phase`` (with ``main_gate`` for an op onto the integration branch, resolving
    ``<phase>-main`` first exactly as :func:`config.validate_cmd` does) names the lifecycle
    boundary.  It changes nothing under ``work.gate.model: legacy`` (the default).  Under
    ``keys`` (attest-keys ADR 3.3) the runtime catalog drift check runs first and, on drift,
    the phase's legacy command (``legacy_cmd``, else ``validate_cmd(phase)``) runs instead of
    any key and no key proof is recorded; otherwise only the phase profile's keys are
    evaluated and every other active key is reported ``not required by <phase>``.
    """
    # The whole selective run already holds one cfg; the inter-lane verdict/carry/tree reads
    # resolve the workspace root from it rather than re-loading config per git call (bh-931we).
    with identity.config_scope(cfg if isinstance(cfg, Mapping) else None):
        return _run(
            entry,
            cfg,
            base_rev=base_rev,
            head_rev=head_rev,
            runner=runner,
            repo_path=repo_path,
            full=full,
            receipt_override=receipt_override,
            phase=phase,
            main_gate=main_gate,
            legacy_cmd=legacy_cmd,
        )


def _profile_keys(entry, cfg, keys: Sequence, phase: str | None, main_gate: bool):
    """The phase profile's key names under ``model: keys``, or ``None`` for every active key.

    A profile naming any key absent from the catalog is doubt, and doubt runs every key.
    """
    names = config.gate_profile(cfg, entry, phase, main_gate=main_gate)
    if names is None:
        return None
    unknown = sorted(set(names) - {key.name for key in keys})
    if unknown:
        typer.echo(f"  · {phase} profile names unknown key(s) {unknown} — running every active key")
        return None
    return frozenset(names)


def _run(
    entry,
    cfg,
    *,
    base_rev: str,
    head_rev: str,
    runner: Runner,
    repo_path: str | None,
    full: bool,
    receipt_override,
    phase: str | None,
    main_gate: bool,
    legacy_cmd: str | None,
) -> int:
    attest = config.attest_config(cfg, entry)
    keys = attest_keys(attest)
    if not keys:
        return runner(config.validate_cmd(cfg, entry))
    profile = None
    if config.gate_model(cfg, entry) == "keys":
        if not catalog_ok(entry, cfg, head_rev):
            fallback = (
                legacy_cmd
                if legacy_cmd is not None
                else config.validate_cmd(cfg, entry, phase, main_gate=main_gate)
            )
            typer.echo(
                f"  ! {phase or 'validation'}: attest catalog drift — running the legacy "
                f"command {fallback!r}; no key proof is recorded"
            )
            return runner(fallback)
        profile = _profile_keys(entry, cfg, keys, phase, main_gate)
    active_keys = tuple(key for key in keys if not key.is_disabled())
    for key in keys:
        if key.is_disabled():
            expiry = f" (until {key.disabled_until.isoformat()})" if key.disabled_until else ""
            typer.echo(f"  · {key.name}: DISABLED — {key.disabled_reason}{expiry}")
    if profile is not None:
        required = tuple(key for key in active_keys if key.name in profile)
        if required:
            for key in active_keys:
                if key.name not in profile:
                    typer.echo(f"  · {key.name}: not required by {phase}")
            active_keys = required
        else:
            typer.echo(f"  · {phase} profile names no active key — running every active key")
    from . import registry

    repo = repo_path or str(registry.hive_dir(entry))
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

    # Bootstrap collects the configured backend from enabled `build.impact` plugins.
    plugin_kernel = cfg.get("plugin_kernel") if isinstance(cfg, Mapping) else None
    resolver = impact_resolver(attest, repo=repo, plugin_kernel=plugin_kernel)
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

    # (name, ran-for-want-of-a-source-verdict) for every key that must execute.
    pending: list[tuple[str, bool]] = []
    uncarried: list[str] = []
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
        else:
            uncarried.append(name)

    if uncarried and config.carry_high_water(cfg, entry):
        for name in high_water_carry(
            entry,
            cfg,
            resolver=resolver,
            repo=repo,
            head_rev=head_rev,
            receipt=receipt,
            keys=[by_name[name] for name in uncarried],
        ):
            outcomes[name] = 0

    for name in uncarried:
        if name in outcomes:
            continue
        if by_name[name].policy == "required":
            pending.append((name, True))
        else:
            typer.echo(f"  ? {name}: unknown (no qualifying source verdict)")
            outcomes[name] = None

    for name in receipt.invalidated_keys:
        if not reuse_exact_tree(by_name[name]):
            pending.append((name, False))

    fail_fast = fail_fast_enabled()
    stopped_after = ""
    precheck_red = False
    precheck = precheck_cmd(cfg, entry) if pending else ""
    if precheck:
        started = time.perf_counter()
        rc = runner(precheck)
        elapsed = time.perf_counter() - started
        if rc == 0:
            typer.echo(f"  ✓ precheck: ran green ({elapsed:.3f}s)")
        else:
            precheck_red = True
            typer.echo(f"  ✗ precheck: ran red (exit {rc}) ({elapsed:.3f}s)")
            typer.echo(
                f"    `{precheck}` found this tree cannot pass: re-run the generator it names "
                "above, commit the result, and validate again."
            )
            if fail_fast:
                stopped_after = "precheck"

    unaffected_run = dict(pending)
    names = [name for name, _ in pending]
    for name in cheapest_first(names, historical_costs(entry, [by_name[n] for n in names])):
        key = by_name[name]
        if stopped_after:
            typer.echo(f"  · {name}: not run — fail-fast after {stopped_after} ({FAIL_FAST_ENV})")
            outcomes[name] = None
            continue
        rc, elapsed = run_key(key)
        outcomes[name] = rc
        state = "ran green" if rc == 0 else "unknown" if rc == 75 else f"ran red (exit {rc})"
        mark = "✓" if rc == 0 else "?" if rc == 75 else "✗"
        if unaffected_run[name]:
            typer.echo(f"  {mark} {name}: {state} (no qualifying source verdict) [{elapsed:.3f}s]")
        else:
            typer.echo(f"  {mark} {name}: {state} ({elapsed:.3f}s)")
        if fail_fast and _blocks(key, rc):
            stopped_after = name

    blocked = precheck_red
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
    "FAIL_FAST_ENV",
    "FULL_GATE_PHASES",
    "PRECHECK_PHASE",
    "UNRESOLVED_IMPACT_EXIT",
    "attested_green",
    "cheapest_first",
    "configured",
    "error_unresolved_impact",
    "fail_fast_enabled",
    "high_water_carry",
    "historical_costs",
    "precheck_cmd",
    "run",
    "record_full_gate_keys",
    "warn_impact_fallback",
]
