"""Verification worktrees and post-create initialization.

Implementations live here; :mod:`beadhive.worktree` remains the stable facade. Internal
collaborators deliberately resolve through that facade so existing monkeypatch seams keep
working while callers migrate at their own pace.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import secrets
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import typer

from . import (
    config,
    converge,
    host,
    otel,
    registry,
    test_report,
    triage_store,
    validation_admission,
    validation_ledger,
    validation_records,
)

VERIFY_LEAF_PREFIX = "verify-"
VERIFY_MARKER = ".bh-verify.json"  # legacy in-checkout marker; read-only since bh-odqgy
VERIFY_ACTIVE_PATH = Path("bh") / "validation" / "active"
_VERIFY_RAND_BYTES = 3
_VERIFY_CREATE_ATTEMPTS = 8
_VERIFY_GRACE_SECONDS = 5 * 60
_VERIFY_TTL_SECONDS = 24 * 60 * 60
_COLOR_FORCE_ENV_KEYS = ("FORCE_COLOR", "CLICOLOR_FORCE")
_BARE_CHECKOUT_HINT = (
    "  ↳ the command ran in a bare clean checkout: only worktree init rules flagged "
    "`verify: true` were applied. If this failure doesn't reproduce in your dev worktree, "
    "the checkout likely isn't provisioned — flag the dependency-sync rules (e.g. 'uv sync') "
    "with verify: true under worktrees.init / worktree_init, or make the repo self-provisioning "
    "(uv [dependency-groups] + tool.uv.default-groups make a bare 'uv run' self-sufficient; "
    "extras are NEVER synced by default). See docs/WORKTREES.md (verify-environment contract)."
)


def _facade():
    from . import worktree

    return worktree


def _call_facade(name, *args, **kwargs):
    return getattr(_facade(), name)(*args, **kwargs)


def _rules(*args, **kwargs):
    return _call_facade("_rules", *args, **kwargs)


def run_init(*args, **kwargs):
    return _call_facade("run_init", *args, **kwargs)


def _pid_alive(*args, **kwargs):
    return _call_facade("_pid_alive", *args, **kwargs)


def _pid_start(*args, **kwargs):
    return _call_facade("_pid_start", *args, **kwargs)


def _pid_starts(*args, **kwargs):
    return _call_facade("_pid_starts", *args, **kwargs)


def _verify_marker_root(*args, **kwargs):
    return _call_facade("_verify_marker_root", *args, **kwargs)


def _verify_marker_path(*args, **kwargs):
    return _call_facade("_verify_marker_path", *args, **kwargs)


def _write_verify_marker(*args, **kwargs):
    return _call_facade("_write_verify_marker", *args, **kwargs)


def _read_verify_marker(*args, **kwargs):
    return _call_facade("_read_verify_marker", *args, **kwargs)


def _remove_verify_marker(*args, **kwargs):
    return _call_facade("_remove_verify_marker", *args, **kwargs)


def _verify_dir_is_orphan(*args, **kwargs):
    return _call_facade("_verify_dir_is_orphan", *args, **kwargs)


def _verify_dir_candidates(*args, **kwargs):
    return _call_facade("_verify_dir_candidates", *args, **kwargs)


def _live_marker_pids(*args, **kwargs):
    return _call_facade("_live_marker_pids", *args, **kwargs)


def sweep_verify_dirs(*args, **kwargs):
    return _call_facade("sweep_verify_dirs", *args, **kwargs)


def _branch_sha(*args, **kwargs):
    return _call_facade("_branch_sha", *args, **kwargs)


def _create_verify_dir(*args, **kwargs):
    return _call_facade("_create_verify_dir", *args, **kwargs)


def _color_neutral_env(*args, **kwargs):
    return _call_facade("_color_neutral_env", *args, **kwargs)


def _reuse_verdict_hit(*args, **kwargs):
    return _call_facade("_reuse_verdict_hit", *args, **kwargs)


def _prepare_verify_worktree(*args, **kwargs):
    return _call_facade("_prepare_verify_worktree", *args, **kwargs)


def clean_checkout(*args, **kwargs):
    return _call_facade("clean_checkout", *args, **kwargs)


def _run_git(*args, **kwargs):
    return _call_facade("_run_git", *args, **kwargs)


def missing_binary(*args, **kwargs):
    return _call_facade("missing_binary", *args, **kwargs)


def run(*args, **kwargs):
    return _call_facade("run", *args, **kwargs)


def wt_dir(*args, **kwargs):
    return _call_facade("wt_dir", *args, **kwargs)


def impl__rules(cfg, entry):
    """Global worktrees.init then the hive's worktree_init (both lists of
    {run, if_exists?, verify?}). Explicit config only — a declared toolchain (bh-d0kb)
    is knowledge-only metadata and never contributes rules here."""
    out = list(config.worktrees_cfg(cfg).get("init", []) or [])
    out += list(entry.get("worktree_init", []) or [])
    return out


def impl_run_init(cfg, entry, path: Path, verify_only: bool = False):
    """Evaluate init rules in `path`: run each whose if_exists glob matches (or has none).
    Best-effort — a failing/absent command warns and we keep going.

    `verify_only` filters to rules flagged `{verify: true}` — the opt-in subset a
    `clean_checkout` verify dir needs to be provisioned enough to validate (dependency
    sync like `uv sync`, trust stamps like `mise trust`). Heavy/side-effectful seat
    provisioning (e.g. `just setup`) stays unflagged so it never runs per validation.
    Flagged rules run on EVERY validation (per-invocation verify dirs), so keep them
    idempotent and cache-friendly.

    Ponytail note (bh-3oq2.2, subprocess-in-loop biomarker): this loop's one `run()` spawn per
    rule is NOT a batchable N+1 — each rule is a distinct, config-declared external command
    (`uv sync`, `mise trust`, `just setup`, ...) that must run in its own process, in the
    operator's declared order, with its own independent failure handling (best-effort: one
    rule's nonzero exit warns but never skips the rest). There's no single call that could
    replace N different commands without changing what actually runs, so this is left as-is
    rather than forced into an artificial batch.

    bh-rcroq: a per-rule ``⚠`` is easy to miss in a wall of dependency-resolution output, so
    failures are also collected and re-surfaced as a one-line summary after the loop — still
    non-fatal (best-effort optional convenience, never blocks worktree creation), just no
    longer silent-in-practice."""
    failed: list[str] = []
    for rule in _rules(cfg, entry):
        rule = rule or {}
        cmd = rule.get("run")
        if not cmd:
            continue
        if verify_only and not rule.get("verify"):
            continue
        cond = rule.get("if_exists")
        if cond and not any(path.glob(cond)):
            continue
        typer.echo(f"  → {cmd}")
        try:
            res = run(shlex.split(cmd), cwd=str(path), check=False)
        except FileNotFoundError:
            typer.echo(f"  ⚠ init: command not found: {cmd}", err=True)
            failed.append(cmd)
            continue
        if res.returncode != 0:
            typer.echo(f"  ⚠ init: '{cmd}' exited {res.returncode}", err=True)
            failed.append(cmd)
    if failed:
        typer.echo(
            f"  ⚠ init: {len(failed)} optional provisioning rule(s) failed and were skipped "
            f"(worktree is otherwise ready): {'; '.join(failed)}",
            err=True,
        )


def impl__pid_alive(pid: int) -> bool:
    """True iff a process with `pid` exists on this host (POSIX ``kill -0``; mirrors
    work_group._pid_alive — kept local so worktree never imports work_group)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists — just not ours to signal
    except (OverflowError, ValueError, OSError):
        return False
    return True


def impl__pid_start(pid: int) -> str:
    """Best-effort start-time token for `pid` ('' when unprobeable). ``ps -o lstart=`` is portable
    across macOS + Linux; a mismatched token means the pid was recycled by a NEW process, defeating
    pid-reuse false-liveness. Deliberately calls subprocess directly (not the module `run` seam):
    this is a pure local probe, and tests faking `worktree.run` must not intercept it."""
    try:
        res = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def impl__pid_starts(pids) -> dict:
    """Start-time tokens for MULTIPLE pids in ONE `ps` spawn — the subprocess-in-loop N+1 fix
    (bh-3oq2.2) for `sweep_verify_dirs`: calling `_pid_start` once per candidate verify- dir would
    spawn one `ps` per dir on every `clean_checkout`'s hot, request-reachable path. Missing/
    unprobeable pids are simply absent from the returned map; an empty `pids` short-circuits with
    no subprocess spawn at all."""
    if not pids:
        return {}
    try:
        res = subprocess.run(
            ["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in sorted(set(pids)))],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return {}
    if res.returncode != 0:
        return {}
    out: dict = {}
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, rest = line.partition(" ")
        try:
            out[int(pid_str)] = rest.strip()
        except ValueError:
            continue
    return out


def impl__verify_marker_root(main: Path) -> Path | None:
    """The canonical git-private directory for active validation markers.

    ``--git-common-dir`` is required here rather than ``main / ".git"``: all linked worktrees
    of a hive must share one liveness store. Resolution is best-effort so a Git/probe failure
    degrades to the existing markerless grace/TTL behavior instead of blocking validation.
    """
    res = _run_git(
        ["git", "-C", str(main), "rev-parse", "--git-common-dir"],
        check=False,
        capture=True,
    )
    out = (getattr(res, "stdout", "") or "").strip()
    if res.returncode != 0 or not out:
        return None
    common = Path(out)
    if not common.is_absolute():
        common = Path(main) / common
    return common / VERIFY_ACTIVE_PATH


def impl__verify_marker_path(marker_root: Path, d: Path) -> Path:
    """Marker path keyed by the verify worktree's unique leaf identity."""
    return Path(marker_root) / f"{d.name}.json"


def impl__write_verify_marker(
    tmp: Path, branch: str, cmd: str, marker_root: Path | None = None
) -> Path | None:
    """Atomically write a verify worktree's liveness marker outside the checkout.

    Host+pid+pid-start identify the creator so the sweep can tell a live run from an orphan.
    The unique worktree leaf keys the file under ``<git-common-dir>/bh/validation/active``;
    path+identity in the document prevent a stale or misplaced file from being trusted for a
    different checkout. Best-effort — an unwritable store keeps the existing grace/TTL fallback.
    """
    if marker_root is None:
        return None
    pid = os.getpid()
    marker = {
        "host": host.host_id(),
        "pid": pid,
        "pid_start": _pid_start(pid),
        "created_at": int(time.time()),
        "branch": branch,
        "command": cmd,
        "worktree": str(tmp.absolute()),
        "identity": tmp.name,
    }
    target = impl__verify_marker_path(marker_root, tmp)
    pending = target.with_name(f".{target.name}.tmp-{pid}-{secrets.token_hex(_VERIFY_RAND_BYTES)}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps(marker, indent=2) + "\n")
        os.replace(pending, target)
    except OSError:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return target


def _read_marker_file(path: Path):
    """Parsed JSON from a regular marker file, or ``None`` without blocking on special files."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def impl__read_verify_marker(d: Path, marker_root: Path | None = None):
    """A verify dir's external marker, with legacy in-checkout fallback.

    New markers live under ``marker_root``. During the compatibility window, a pre-upgrade
    orphan may carry only ``.bh-verify.json`` inside its checkout, so that old location remains
    read-only fallback. ``None`` means absent/unreadable and preserves grace/TTL classification.

    The `is_file()` guard is the point of factoring it out (bh-0tmvk, same class as bh-0jgdz's
    `release._read_marker`): a FIFO at this path makes `read_text()` block FOREVER rather than
    raise, and the `except` never runs. `sweep_verify_dirs` is NOT only a background reaper — it
    runs at the top of every `clean_checkout`, so a non-regular file here wedges `bh work
    submit` and the pre-push hook exactly like the ledger case does, silently and with no exit
    code. None (never `{}`) marks the unreadable case so callers keep distinguishing "no marker"
    from a marker that parsed to something falsy."""
    if marker_root is not None:
        marker = _read_marker_file(impl__verify_marker_path(marker_root, d))
        if marker is not None:
            expected_path = str(d.absolute())
            if marker.get("worktree") == expected_path and marker.get("identity") == d.name:
                return marker
            # A marker keyed to this leaf but naming another checkout is stale/misplaced. Do not
            # let it claim this directory; a genuine legacy marker may still identify the owner.
        # Current pointers are keyed by run id and carry no duplicated lifecycle/ownership.
        # Reconstruct the old reader shape from the authoritative running manifest so the
        # existing orphan classifier remains compatible during this migration.
        try:
            pointers = list(Path(marker_root).glob("run-*.json"))
        except OSError:
            pointers = []
        for pointer in pointers:
            active = _read_marker_file(pointer)
            run_id = active.get("run_id") if isinstance(active, dict) else None
            if not isinstance(run_id, str):
                continue
            manifest = _read_marker_file(
                Path(marker_root).parent / "runs" / run_id / "manifest.json"
            )
            if not isinstance(manifest, dict) or manifest.get("lifecycle") != "running":
                continue
            if manifest.get("worktree") != str(d.resolve()):
                continue
            owner = manifest.get("owner") or {}
            return {
                "host": owner.get("host"),
                "pid": owner.get("pid"),
                "pid_start": owner.get("start_token"),
                "created_at": manifest.get("started_at"),
                "branch": manifest.get("branch"),
                "command": manifest.get("command"),
                "worktree": manifest.get("worktree"),
                "identity": d.name,
                "run_id": run_id,
            }
    return _read_marker_file(d / VERIFY_MARKER)


def impl__remove_verify_marker(d: Path, marker_root: Path | None = None) -> None:
    """Best-effort removal of exactly ``d``'s external active marker."""
    if marker_root is None:
        return
    try:
        impl__verify_marker_path(marker_root, d).unlink(missing_ok=True)
    except OSError:
        pass


def impl__verify_dir_is_orphan(
    d: Path,
    now: float,
    grace: int,
    ttl: int,
    pid_starts: dict | None = None,
    marker_root: Path | None = None,
) -> bool:
    """Whether a sibling verify- dir is a demonstrably-dead leftover safe to reap. Conservative by
    construction: a live same-host pid (with matching start-time) is NEVER reaped; a cross-host or
    unreadable dir falls back to the grace/TTL windows only.

    `pid_starts`, when given, is a pre-fetched ``{pid: start_time_token}`` map (see `_pid_starts`)
    — `sweep_verify_dirs` batches ALL of its candidates' probes into one map up front instead of
    spawning `ps` once per dir here. Falls back to a direct single-pid `_pid_start` call when no
    map is supplied, so a caller probing one dir in isolation is unaffected."""
    try:
        age = now - d.stat().st_mtime
    except OSError:
        return False  # vanished mid-sweep (its owner cleaned up) — nothing to do
    if age > ttl:
        return True  # hard backstop: reboots, shared FS, anything the pid probe can't see
    marker = _read_verify_marker(d, marker_root=marker_root)
    if marker is None:
        return age > grace  # marker missing/unreadable: reap only past the grace window
    marker_host, pid = marker.get("host"), marker.get("pid")
    # `marker_host` compares against the stable `host_id()` UUID (bh-ytbb.4), not
    # `socket.gethostname()` — see `_write_verify_marker`.
    if marker_host != host.host_id() or not isinstance(pid, int):
        return False  # cross-host / malformed marker: only the TTL backstop applies
    if not _pid_alive(pid):
        return True  # creator is gone
    recorded = marker.get("pid_start") or ""
    current = pid_starts.get(pid, "") if pid_starts is not None else _pid_start(pid)
    return bool(recorded and current and recorded != current)  # pid recycled — creator is gone


def impl__verify_dir_candidates(parent: Path) -> list:
    """Sibling verify-* dirs directly under `parent`, in stable (sorted) order — the scan
    `sweep_verify_dirs` classifies, split out so pids can be batch-fetched before classifying."""
    return [
        d for d in sorted(parent.iterdir()) if d.name.startswith(VERIFY_LEAF_PREFIX) and d.is_dir()
    ]


def impl__live_marker_pids(dirs, marker_root: Path | None = None) -> set:
    """Same-host, live, int pids drawn from `dirs`' markers — exactly the set
    `_verify_dir_is_orphan` would otherwise call `_pid_start` for, one at a time. Feeds
    `sweep_verify_dirs`' single batched `_pid_starts` call."""
    pids: set = set()
    this_host = host.host_id()
    for d in dirs:
        marker = _read_verify_marker(d, marker_root=marker_root)
        if marker is None:
            continue
        pid = marker.get("pid")
        if marker.get("host") == this_host and isinstance(pid, int) and _pid_alive(pid):
            pids.add(pid)
    return pids


def impl_sweep_verify_dirs(entry, grace=_VERIFY_GRACE_SECONDS, ttl=_VERIFY_TTL_SECONDS) -> int:
    """Reap orphaned ephemeral verify-* clean-checkout dirs for `entry`'s hive — ALL siblings,
    independent of creator. Called at the top of every clean_checkout (self-healing) and from
    `prune`. Live dirs (marker pid alive with matching start-time) are always spared. Returns the
    number of dirs reaped.

    Batches every candidate's pid-start probe into ONE `ps` spawn up front (`_pid_starts`) rather
    than one per dir (bh-3oq2.2 subprocess-in-loop fix) — this runs on `clean_checkout`'s hot,
    request-reachable path, so an unbounded per-dir spawn count matters."""
    main = registry.hive_dir(entry)
    marker_root = _verify_marker_root(main)
    parent = wt_dir(entry, VERIFY_LEAF_PREFIX).parent
    if not parent.is_dir():
        return 0
    dirs = _verify_dir_candidates(parent)
    pid_starts = _pid_starts(_live_marker_pids(dirs, marker_root=marker_root))
    reaped = 0
    now = time.time()
    for d in dirs:
        marker = _read_verify_marker(d, marker_root=marker_root)
        if not _verify_dir_is_orphan(d, now, grace, ttl, pid_starts, marker_root=marker_root):
            continue
        _run_git(["git", "-C", str(main), "worktree", "remove", "--force", str(d)], check=False)
        if d.exists():  # not (or no longer) a registered worktree — plain filesystem leftover
            shutil.rmtree(d, ignore_errors=True)
        _remove_verify_marker(d, marker_root=marker_root)
        run_id = marker.get("run_id") if isinstance(marker, dict) else None
        if isinstance(run_id, str):
            validation_records.abandon_run(main, run_id, reason="owner_dead")
        reaped += 1
    return reaped


def impl__branch_sha(entry, branch) -> str:
    """Full sha of `branch` in the hive main clone — the verdict-ledger key. '' on any error
    (a faked/failed rev-parse just disables ledger lookup + record for this invocation)."""
    main = registry.hive_dir(entry)
    res = _run_git(["git", "-C", str(main), "rev-parse", branch], check=False, capture=True)
    out = getattr(res, "stdout", "") or ""
    return out.strip() if res.returncode == 0 else ""


def impl__create_verify_dir(entry, leaf_base: str) -> Path | None:
    """Atomically create a unique per-invocation verify dir (<leaf_base>-<rand6>): mkdir is the
    atomic claim, retry-on-exists gives mkdtemp semantics — uniqueness never depends on pid."""
    for _ in range(_VERIFY_CREATE_ATTEMPTS):
        tmp = wt_dir(entry, f"{leaf_base}-{secrets.token_hex(_VERIFY_RAND_BYTES)}")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.mkdir()
        except FileExistsError:
            continue
        return tmp
    return None


def impl__color_neutral_env(base: dict[str, str]) -> dict[str, str]:
    """A copy of `base` with `FORCE_COLOR` / `CLICOLOR_FORCE` dropped and `NO_COLOR=1` forced on,
    so a validation subprocess renders plain-text output regardless of the operator's terminal
    color env. Everything else in `base` is preserved untouched."""
    env = {k: v for k, v in base.items() if k not in _COLOR_FORCE_ENV_KEYS}
    env["NO_COLOR"] = "1"
    return env


def impl__reuse_verdict_hit(
    entry, sha: str, cmd: str, cfg=None, *, bead=None, phase="reuse", branch=None, worktree=None
) -> bool:
    """True (after echoing the reused-verdict notice and counting telemetry) iff a fresh GREEN
    ledger verdict exists for (entry, TREE of `sha`, cmd) — `clean_checkout`'s `reuse=True`
    short-circuit. `sha` is a rev the ledger resolves to its tree, which is the real key
    (bh-ku9n9.3); the notice still names the commit, and the tree the verdict was earned at, so
    an operator can see when a hit came from a *different* commit at identical content.

    A hit is "skip the expensive command", never "skip everything": `green_verdict` runs the
    hive's declared `work.always_run` set first and withholds the hit if it fails (bh-ehmd8).
    That decision lives there — the one seam every reuse boundary reads — not here.

    `cfg` — `clean_checkout`'s own, already resolved — is forwarded to the ledger's TTL lookup
    rather than re-read from disk (bh-ku9n9.19, item 2)."""
    hit = validation_ledger.green_verdict(entry, sha, cmd, cfg=cfg)
    if hit is None:
        return False
    # A hit is a new decision, never an execution. Preserve the long-standing green_verdict
    # monkeypatch seam above, then attach provenance here at the clean-checkout boundary.
    main = registry.hive_dir(entry)
    tree = validation_ledger.tree_of(entry, sha)
    original = validation_records.completed_run(
        main, tree=tree, command_hash=validation_ledger.cmd_hash(cmd)
    )
    if original is not None:
        # The flat 0.15.1 index may still say green after a later `work check` observed red/none:
        # check intentionally never wrote red cache rows.  Authoritative run history wins.  A
        # non-green newest execution therefore refuses reuse instead of returning success without
        # a use record (the second-review green→red/none→reuse gap).
        if original.get("verdict") != "green":
            return False
        validation_records.record_use(
            main,
            run_id=original["run_id"],
            bead=bead,
            phase=phase,
            branch=branch,
            worktree=worktree,
            sha=sha,
            tree=tree,
            command_hash=validation_ledger.cmd_hash(cmd),
            reused=True,
        )
    when = datetime.datetime.fromtimestamp(hit["at"]).astimezone().isoformat(timespec="seconds")
    typer.echo(
        f"✓ validation verdict reused (sha {sha[:7]}, tree {str(hit.get('tree', ''))[:7]}, "
        f"recorded {when})"
    )
    otel.count_validation_reuse({"bh.hive": str(entry.get("prefix", ""))})
    return True


def impl__prepare_verify_worktree(main: Path, entry, branch: str, cmd: str):
    """Reap stale siblings, then create+mark a fresh detached verify-<leaf>-<rand6> worktree for
    `branch`. Returns `(path, 0)` on success, or `(None, exit_code)` — after echoing the failure —
    when the dir or the `git worktree add` can't be created."""
    sweep_verify_dirs(entry)
    marker_root = _verify_marker_root(main)
    leaf_base = registry.sanitize(f"{VERIFY_LEAF_PREFIX}{branch.rsplit('/', 1)[-1]}")
    tmp = _create_verify_dir(entry, leaf_base)
    if tmp is None:
        typer.echo(f"✗ could not create a unique {leaf_base}-* verify dir", err=True)
        return None, 1
    add_res = _run_git(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(tmp), branch], check=False
    )
    if add_res.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)  # our own claim; never adopted by git
        return None, add_res.returncode
    _write_verify_marker(tmp, branch, cmd, marker_root=marker_root)
    return tmp, 0


def _impl_clean_checkout_unadmitted(
    entry, branch, cmd, cfg=None, reuse=False, *, bead=None, phase="validation"
) -> int:
    """Validate `branch` from a throwaway detached worktree, so the result never depends on
    dirty local state. Each invocation gets its OWN verify-<leaf>-<rand6> dir (bh-nikb): two
    processes validating the same branch can no longer destroy each other's in-flight checkout —
    there is no entry-time pre-clean, and the finally-cleanup removes only this invocation's dir.
    Orphans from killed runs are reaped by the marker-based sweep at entry. The validation command
    runs with a telemetry-neutral env (`otel.telemetry_neutral_env`) so its result is independent
    of the operator's otel config and never exports telemetry, composed with a color-neutral env
    (`_color_neutral_env`) so it's also independent of the operator's terminal color settings
    (`FORCE_COLOR` / `CLICOLOR_FORCE` scrubbed, `NO_COLOR=1` forced — bh-76gx: an inherited
    FORCE_COLOR was leaking ANSI escapes into `--help` output, false-REDing a plain-substring
    assert). Returns the validation command's exit code (or git's, if checkout fails).

    Before the command runs, init rules flagged `{verify: true}` are applied (bh-7k1p) so a
    validate_cmd that assumes a provisioned environment (`uv run …`) doesn't false-fail in the
    bare checkout; all other init rules — and observaloop provisioning — stay excluded. `cfg`
    defaults to `config.load()` (no config → no rules) so the many indirect callers need not
    thread it. On a nonzero exit from the command, a one-shot bare-checkout hint is emitted to
    stderr here — the single central place — rather than at every caller's failure render.

    Verdict ledger (bh-dfx0, re-keyed by bh-ku9n9.3): every run records its (TREE, cmd) verdict
    in the hive-local validation ledger — the commit sha rides along as metadata only. With
    `reuse=True` a fresh GREEN verdict for the exact key short-circuits the whole checkout
    (rc 0); a red / stale / cmd-changed / different-tree verdict always revalidates. Keying on
    the tree is what lets a `--no-ff` merge onto an unmoved main reuse the branch tip's verdict
    (byte-identical tree, new commit) while a merge onto a MOVED main misses and runs.

    `reuse=True` is now the landing boundaries' setting too (bh-ku9n9.17, ADR Decision 4): merge,
    postland, finish and batch land consult the ledger, because with a (tree, cmd_hash) key a hit
    IS an exact tree match and nothing weaker can produce one. `reuse` still defaults to False,
    so an unflagged caller — the `review --run` demo, the union-merge conflict tier — is fresh by
    construction; `review --run` itself only reuses under an explicit `--no-fresh`."""
    main = registry.hive_dir(entry)
    if cfg is None:
        try:
            cfg = config.load()
        except FileNotFoundError:
            cfg = {}
    # Validate artifact placement before _prepare_verify_worktree mutates either
    # Git's worktree registry or the compatibility liveness marker.  Apart from
    # avoiding a needless checkout on a setup error, this makes the refusal
    # genuinely side-effect free.
    artifact_root_config = config.work_value(cfg, entry, "validation_artifact_root", "")
    if os.environ.get("BH_VALIDATION_ARTIFACT_ROOT") or artifact_root_config:
        try:
            validation_records.artifact_root(main, artifact_root_config)
        except ValueError as exc:
            typer.echo(f"✗ {exc}", err=True)
            return 2
    sha = _branch_sha(entry, branch)
    if reuse and _reuse_verdict_hit(
        entry, sha, cmd, cfg=cfg, bead=bead, phase=phase, branch=branch
    ):
        return 0
    tmp, rc = _prepare_verify_worktree(main, entry, branch, cmd)
    if tmp is None:
        tree = validation_ledger.tree_of(entry, sha)
        failed = validation_records.begin_run(
            main,
            bead=bead,
            phase=phase,
            branch=branch,
            worktree=None,
            sha=sha,
            tree=tree,
            command_hash=validation_ledger.cmd_hash(cmd),
            command=cmd,
            owner_start=_pid_start(os.getpid()),
        )
        if failed is not None:
            validation_records.finish_run(
                main, failed["run_id"], exit_code=rc, reason="checkout_failure"
            )
            validation_records.record_use(
                main,
                run_id=failed["run_id"],
                bead=bead,
                phase=phase,
                branch=branch,
                worktree=None,
                sha=sha,
                tree=tree,
                command_hash=validation_ledger.cmd_hash(cmd),
                reused=False,
            )
        return rc

    # A successful `git worktree add` has registered `tmp` and may have written a
    # compatibility marker.  Start the cleanup guard immediately: even metadata
    # inspection and manifest allocation are fallible, and none may strand that
    # checkout if they raise before the validation body starts.
    def cleanup_verify_checkout() -> None:
        _run_git(["git", "-C", str(main), "worktree", "remove", "--force", str(tmp)], check=False)
        _remove_verify_marker(tmp, marker_root=_verify_marker_root(main))

    run_record = None
    try:
        # Record against the tree that ACTUALLY validated: the verify checkout's own HEAD. The
        # branch can move between the ledger lookup above and the worktree add (TOCTOU), and a
        # verdict recorded under the stale pre-resolved sha would vouch for content it never saw.
        head = _run_git(["git", "-C", str(tmp), "rev-parse", "HEAD"], check=False, capture=True)
        head_out = getattr(head, "stdout", "") or ""  # tolerate faked run() results without stdout
        validated_sha = head_out.strip() if head.returncode == 0 and head_out.strip() else sha
        tree = validation_ledger.tree_of(entry, validated_sha)
        run_record = validation_records.begin_run(
            main,
            bead=bead,
            phase=phase,
            branch=branch,
            worktree=tmp,
            sha=validated_sha,
            tree=tree,
            command_hash=validation_ledger.cmd_hash(cmd),
            command=cmd,
            owner_start=_pid_start(os.getpid()),
            artifact_root_config=artifact_root_config,
        )
        # From this point the run manifest is authoritative.  Remove the 0.15.1 worktree-keyed
        # compatibility marker; the run-id active pointer is sufficient for liveness/reaping.
        if run_record is not None:
            _remove_verify_marker(tmp, marker_root=_verify_marker_root(main))
        # Synchronous-contract heartbeat (bh-i0p1.4): only printed once we're actually about to pay
        # the real cost (the reuse short-circuit above already returned for a cheap hit), so this
        # never fires on the fast path. `just check`-shaped commands can run 5-15 minutes on a large
        # suite — long enough that a caller watching for output, not wall-clock, can mistake a quiet
        # stretch for a hang and reach for backgrounding/polling instead of just waiting on the
        # call. This line is the inline, always-seen counterpart to the guidance in
        # docs/WORKTREES.md: stay
        # synchronous and wait for THIS call to return rather than parking a watcher on it.
        typer.echo(
            f"  → validating {branch} @ {validated_sha[:7]} from a clean checkout — this can take "
            "several minutes; invoke synchronously and wait for it to return rather than "
            "backgrounding or polling"
        )
    except BaseException:
        cleanup_verify_checkout()
        raise
    try:
        try:
            run_init(cfg, entry, tmp, verify_only=True)
        except BaseException:
            if run_record is not None:
                validation_records.finish_run(main, run_record["run_id"], reason="setup_failure")
                validation_records.record_use(
                    main,
                    run_id=run_record["run_id"],
                    bead=bead,
                    phase=phase,
                    branch=branch,
                    worktree=tmp,
                    sha=validated_sha,
                    tree=tree,
                    command_hash=validation_ledger.cmd_hash(cmd),
                    reused=False,
                )
            raise
        # BH_TEST_REPORT_DIR (bh-ku9n9.20): a fresh, empty drop zone exported into every
        # validation subprocess, with no opt-in and no bh config. bh never invokes a runner — it
        # names a directory and reads what appears. `rc` below stays the sole verdict; an
        # ingested report is detail on the ledger entry and can never upgrade it.
        # The gate log is teed live (bh-ku9n9.6): the output still streams, and now also
        # survives the run that produced it, so a red 6-minute gate can be read instead of
        # re-run. `triage_store` keeps it only when the write rule fires — red, or retried.
        artifacts = (run_record or {}).get("artifacts") or {}
        if artifacts:
            reports_dir = Path(artifacts["reports"])
            gate_log = Path(artifacts["gate_log"])
            if not reports_dir.is_dir() or not gate_log.parent.is_dir():
                raise RuntimeError("validation artifact directory could not be allocated")
            drop_context = test_report.drop_zone(reports_dir)
            log_context = contextlib.nullcontext(gate_log)
        else:
            # Best-effort control-state allocation has always been allowed to miss;
            # preserve the validation command's execution semantics in that case.
            drop_context = test_report.drop_zone()
            log_context = triage_store.gate_log()
        with drop_context as drop, log_context as log:
            protocol_path = validation_records.protocol_path(
                drop, config.work_value(cfg, entry, "validation_protocol", "none")
            )
            child_env = test_report.export(_color_neutral_env(otel.telemetry_neutral_env()), drop)
            if protocol_path is not None:
                child_env[validation_records.PROTOCOL_RESULT_ENV] = str(protocol_path)
            try:
                res = run(
                    shlex.split(cmd),
                    cwd=str(tmp),
                    check=False,
                    env=child_env,
                    tee=log,
                )
            except BaseException:
                if run_record is not None:
                    validation_records.finish_run(main, run_record["run_id"], reason="interrupted")
                    validation_records.record_use(
                        main,
                        run_id=run_record["run_id"],
                        bead=bead,
                        phase=phase,
                        branch=branch,
                        worktree=tmp,
                        sha=validated_sha,
                        tree=tree,
                        command_hash=validation_ledger.cmd_hash(cmd),
                        reused=False,
                    )
                raise
            rc = res.returncode
            report = test_report.ingest(drop, rc)
            protocol = validation_records.read_protocol(protocol_path)
            if run_record is not None:
                validation_records.attach_summary(
                    main,
                    run_record["run_id"],
                    {"counts": test_report.counts(report), "tree": tree},
                )
            # Inside the `with`: the drop zone is gone the moment it closes, so the raw runner
            # output has to be copied into the durable per-tree store before then.
            triage_store.store(
                entry,
                validated_sha,
                cmd,
                rc,
                report,
                drop,
                log,
                run_id=run_record["run_id"] if run_record else None,
            )
        missing = missing_binary(res)
        if run_record is not None:
            validation_records.finish_run(
                main,
                run_record["run_id"],
                exit_code=rc,
                signal_number=-rc if rc < 0 else None,
                reason=(
                    "missing_binary" if missing else "interrupted" if rc < 0 else "command_exit"
                ),
                protocol=protocol,
            )
        validation_ledger.record(  # best-effort compatibility index + execution use
            entry,
            validated_sha,
            cmd,
            rc,
            report=report,
            cfg=cfg,
            run_id=run_record["run_id"] if run_record else None,
            phase=phase,
            bead=bead,
            branch=branch,
            worktree=tmp,
        )
        # A clean checkout running the phase WHOLE is the confirming run — the only kind of run
        # that may attest (bh-ku9n9.8). It never converges and never consults
        # `work.validate_subset`; all it does here is read the tree's retry history back and say
        # so when part of this green took a retry to get there, rather than absorbing the flake.
        converge.warn_flakes(entry, validated_sha, rc)
        if missing:
            # This seam runs WITHOUT capture, so a missing binary would otherwise exit 127 having
            # printed NOTHING — no stdout, no stderr — and _BARE_CHECKOUT_HINT would then point
            # the operator at the checkout, which is not the problem. Silence plus a misdirection
            # is a worse operator experience than the crash this replaced (bh-7m2h9).
            typer.echo(
                f"✗ validation could not RUN: `{missing}` is not on PATH. This is not a test "
                f"failure and says nothing about {validated_sha[:7]} — install it or fix PATH, "
                f"then re-run.",
                err=True,
            )
            return rc
        if rc != 0:
            typer.echo(_BARE_CHECKOUT_HINT, err=True)
        return rc
    finally:
        if run_record is not None:
            current = validation_records.read_run(main, run_record["run_id"])
            if current is not None and current.get("lifecycle") == "running":
                validation_records.abandon_run(main, run_record["run_id"], reason="interrupted")
        cleanup_verify_checkout()


def impl_clean_checkout(
    entry, branch, cmd, cfg=None, reuse=False, *, bead=None, phase="validation"
) -> int:
    """Canonical clean-checkout gate, bounded by host-wide validation admission."""
    if cfg is None:
        try:
            cfg = config.load()
        except FileNotFoundError:
            cfg = {}
    if not reuse:
        with validation_admission.host_slot(cfg, entry):
            return _impl_clean_checkout_unadmitted(
                entry, branch, cmd, cfg=cfg, reuse=False, bead=bead, phase=phase
            )

    sha = _branch_sha(entry, branch)
    main = registry.hive_dir(entry)
    tree = validation_ledger.tree_of(entry, sha)
    command_hash = validation_ledger.cmd_hash(cmd)
    prior = validation_records.latest_run(main, tree=tree, command_hash=command_hash)
    prior_id = prior.get("run_id") if prior else None
    # Lock ordering is intentionally identity -> host. Same-key followers wait without taking
    # scarce compute capacity, then re-read authoritative state after the leader finishes.
    with validation_admission.identity_lock(main, tree, command_hash):
        if _reuse_verdict_hit(
            entry, sha, cmd, cfg=cfg, bead=bead, phase=phase, branch=branch
        ):
            return 0
        latest = validation_records.latest_run(main, tree=tree, command_hash=command_hash)
        if (
            latest is not None
            and latest.get("run_id") != prior_id
            and latest.get("lifecycle") == "completed"
            and latest.get("verdict") == "red"
        ):
            validation_records.record_use(
                main,
                run_id=latest["run_id"],
                bead=bead,
                phase=phase,
                branch=branch,
                worktree=None,
                sha=sha,
                tree=tree,
                command_hash=command_hash,
                reused=True,
            )
            return int(latest.get("exit_code") or 1)
        with validation_admission.host_slot(cfg, entry):
            return _impl_clean_checkout_unadmitted(
                entry, branch, cmd, cfg=cfg, reuse=False, bead=bead, phase=phase
            )
