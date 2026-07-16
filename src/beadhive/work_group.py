"""Work-group (batch) mechanics for `ws work` — claim + merge a set of beads as ONE unit.

A *batch* is several beads sharing a `batch:<group>` label (the data model — label/spec field +
cohesion/size validation — already landed in 8v8.1), handled by ONE agent in ONE shared
`wt/batch/<group>` worktree, then validated and merged ONCE as a single `--no-ff` bubble with the
per-bead commits preserved inside (lossless / bisectable). Split out of `work.py` so the
single-bead lifecycle verbs stay the default path and a different file carries the opt-in batch
logic. The bd seam is `bd.run` / `bd.show` / `bd.state`; the shared guards (`_guard_open` /
`_guard_not_other` / `_open_gate` / `_history_ok` / `_stamp`) live in the typer-free `work_logic`,
so this module never imports `work` at all — it depends only on `bd` / `work_logic` / `worktree`.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import time
from contextlib import contextmanager

import typer

from . import config, identity, otel, worktree

BATCH_PREFIX = "batch/"  # a work-group's shared worktree branch is wt/batch/<group>

# Merge-slot staleness (bh-62ex): a slot orphaned by a killed merge (SIGTERM/SIGKILL mid-run)
# self-heals instead of wedging the pipeline. A same-host holder is judged by pid liveness
# (reclaim iff the process is gone); a cross-host holder — whose pid we can't probe — falls back
# to this generous TTL, set well beyond any real merge (~2 min) so a live merge is never stolen.
_SLOT_TTL_SECONDS = 30 * 60
_SLOT_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGTERM", "SIGINT", "SIGHUP")
    if hasattr(signal, name)
)


def _slot_holder(actor: str) -> str:
    """A structured merge-slot holder token embedding host+pid+acquire-time so a later acquirer can
    tell a live holder from an orphan. The human actor stays the leading field for readability."""
    name = actor or "unknown"
    return f"{name}|host={socket.gethostname()}|pid={os.getpid()}|ts={int(time.time())}"


def _parse_holder(token) -> dict:
    """Parse a `_slot_holder` token into its `{host, pid, ts}` fields; `{}` for a legacy/bare or
    empty holder (which is never reclaimed)."""
    if not isinstance(token, str):
        return {}
    fields: dict[str, str] = {}
    for part in token.split("|")[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            fields[key] = val
    return fields


def _pid_alive(pid: int) -> bool:
    """True iff a process with `pid` exists on this host (POSIX ``kill -0``)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists — just not ours to signal
    except (OverflowError, ValueError, OSError):
        return False
    return True


def _holder_is_stale(token, ttl: int = _SLOT_TTL_SECONDS) -> bool:
    """Whether the current slot holder is an orphan safe to reclaim. Prefer a same-host pid probe
    (reclaim iff the holder process is gone); fall back to a generous TTL for a cross-host holder
    or one with no timestamp. A legacy/bare holder (no fields) is NEVER reclaimed — conservative,
    so a live merge whose token we can't read is never stolen."""
    fields = _parse_holder(token)
    if not fields:
        return False
    host, pid_s, ts_s = fields.get("host"), fields.get("pid"), fields.get("ts")
    if host == socket.gethostname() and pid_s and pid_s.isdigit():
        return not _pid_alive(int(pid_s))
    if ts_s and ts_s.isdigit():
        return (time.time() - int(ts_s)) > ttl
    return False


def _current_holder(bd, main):
    """The slot's current holder token via ``bd merge-slot check --json`` (None if unreadable)."""
    res = bd.run(["merge-slot", "check", "--json"], main, capture=True)
    if res.returncode != 0 or not (res.stdout or "").strip():
        return None
    try:
        return (json.loads(res.stdout) or {}).get("holder")
    except (ValueError, TypeError):
        return None


def _acquire_slot(bd, main, holder) -> bool:
    """Acquire the slot as `holder`; on a held slot, reclaim a demonstrably-orphaned holder (dead
    pid / TTL-exceeded) exactly once and retry. Returns True iff the slot is held on return."""
    if bd.run(["merge-slot", "acquire", "--holder", holder], main).returncode == 0:
        return True
    if _holder_is_stale(_current_holder(bd, main)):
        typer.echo("• merge slot held by an orphaned process — reclaiming", err=True)
        bd.run(["merge-slot", "release"], main)
        return bd.run(["merge-slot", "acquire", "--holder", holder], main).returncode == 0
    return False


def _install_slot_signal_release(release):
    """Install best-effort handlers that release the merge slot if the process is signalled
    mid-hold (SIGTERM from a timeout kill, Ctrl-C, hangup) — the crash-safe half of the release.
    SIGKILL can't be caught; the stale-holder reclaim on the next acquire is the backstop for that.
    Returns the previous handlers to restore. No-op off the main thread, where ``signal.signal``
    would raise."""
    installed: dict[int, object] = {}

    def _handler(signum, _frame):
        release()
        prev = installed.get(signum)
        signal.signal(signum, prev if prev is not None else signal.SIG_DFL)
        os.kill(os.getpid(), signum)  # re-raise with the restored disposition (normal exit code)

    for sig in _SLOT_SIGNALS:
        try:
            installed[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            pass  # not the main thread / unsupported — skip; the reclaim path still covers it
    return installed


def _restore_signal_handlers(prev: dict) -> None:
    for sig, handler in prev.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            pass


@contextmanager
def merge_slot(main, slot_attrs=None):
    """Hold the hive's exclusive merge slot for the block: create (idempotent) -> acquire -> yield
    -> release, crash-safe on every exit path INCLUDING a mid-hold signal. The one merge-slot
    skeleton shared by the bead / molecule / batch land sites. When `slot_attrs` is given, also
    emit the slot wait/hold otel timings (the bead & molecule sites pass them; the batch site does
    not, so its otel is unchanged).

    bh-62ex: a merge killed mid-run (e.g. a foreground-timeout SIGTERM during the ~110s validation)
    used to leak the slot and wedge every retry with '✗ could not acquire merge slot'. Two guards
    now fix that: (1) signal handlers release the slot before the process dies, and (2) the acquire
    reclaims a holder whose owning process is gone (or that blew a generous TTL), so even an
    uncatchable SIGKILL self-heals on the next attempt."""
    from . import bd  # lazy: avoids a load-time import cycle

    bd.run(["merge-slot", "create"], main)  # idempotent: no-op once the hive's slot bead exists
    slot_mark = time.perf_counter()
    if not _acquire_slot(bd, main, _slot_holder(identity.resolve_actor())):
        typer.echo("✗ could not acquire merge slot — another merge holds it", err=True)
        raise typer.Exit(1)
    slot_acquired = time.perf_counter()
    if slot_attrs is not None:
        otel.record_merge_slot_wait(slot_acquired - slot_mark, slot_attrs)

    released = False

    def _release():
        nonlocal released
        if not released:
            released = True
            bd.run(["merge-slot", "release"], main)

    prev_handlers = _install_slot_signal_release(_release)
    try:
        yield
    finally:
        _restore_signal_handlers(prev_handlers)
        if slot_attrs is not None:
            otel.record_merge_slot_hold(time.perf_counter() - slot_acquired, slot_attrs)
        _release()


def members_of(group_arg: str) -> list[str]:
    """Parse `--group` into member bead ids (comma/whitespace separated, de-duped, order kept)."""
    seen: dict[str, None] = {}
    for tok in group_arg.replace(",", " ").split():
        if tok:
            seen.setdefault(tok, None)
    return list(seen)


def batch_label(data) -> str:
    """The `<group>` of a bead's `batch:<group>` label ('' if it carries none)."""
    for lbl in (data or {}).get("labels", []) or []:
        s = str(lbl)
        if s.startswith("batch:"):
            return s[len("batch:") :]
    return ""


def resolve_group(members, datas) -> str:
    """The single batch group name shared by every member (read from their existing
    `batch:<group>` labels). Refuse a member with no batch label, or a mix of groups — those
    aren't a runnable unit (8v8.1 validates this at plan time; we re-guard at the verb)."""
    names = {batch_label(datas[m]) for m in members}
    if "" in names:
        bare = [m for m in members if not batch_label(datas[m])]
        typer.echo(f"✗ not batch members (no batch:<group> label): {', '.join(bare)}", err=True)
        raise typer.Exit(1)
    if len(names) != 1:
        typer.echo(f"✗ members span multiple batch groups: {', '.join(sorted(names))}", err=True)
        raise typer.Exit(1)
    return next(iter(names))


def _members(group_arg):
    members = members_of(group_arg)
    if not members:
        typer.echo("✗ --group needs at least one member id", err=True)
        raise typer.Exit(1)
    return members


def ready_children(epic, main) -> list[str]:
    """Ids of an epic's ready (un-closed) child beads, in listing order — the candidate members of
    a collapsed run. Returns `[]` when the epic has no listable children so callers refuse a
    runnable-empty collapse rather than crashing."""
    from . import bd  # lazy: avoids a circular import at module level

    kids = bd.json(["list", "--parent", epic], main)
    if not isinstance(kids, list):
        return []
    return [str(k["id"]) for k in kids if k.get("id") and str(k.get("status", "")) != "closed"]


def synthesize_batch_labels(members, epic, datas, main, actor) -> None:
    """Collapsed-claim pre-step: stamp a synthetic `batch:<epic>` label onto every member that
    carries no `batch:` label yet, so `resolve_group`'s existing precondition holds for an
    un-batched epic (it is NOT modified — this only makes its precondition true). Additive and
    idempotent: a member already carrying a batch label (planner-authored or a prior stamp) is left
    untouched, and no other label is ever removed."""
    from . import bd  # lazy: avoids a circular import at module level

    label = f"batch:{epic}"
    for m in members:
        if batch_label(datas[m]):
            continue  # read-only w.r.t. existing (planner) batch labels — never overwrite
        if bd.run(["label", "add", m, label], main, actor=actor).returncode != 0:
            typer.echo(f"✗ could not stamp {label} on {m}", err=True)
            raise typer.Exit(1)


def claim_collapsed(cfg, hive, epic, as_):
    """Collapsed claim: run an epic's ready children as ONE grouped session even when the planner
    authored no `batch:` labels. Synthesizes a `batch:<epic>` label on each un-batched ready child
    as a PRE-STEP (making `resolve_group`'s precondition true), then delegates to the existing
    `claim_group` — the same one-shared-`wt/batch/<group>`-worktree path the planner-batch flow
    uses. The stamping is additive/idempotent, so re-running a collapse is safe."""
    from . import bd  # lazy: avoids a circular import at module level

    entry, main, _target, _branch = worktree.locate(cfg, hive, epic)
    members = ready_children(epic, main)
    if not members:
        typer.echo(f"✗ no ready children under {epic} to collapse", err=True)
        raise typer.Exit(1)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    datas = {m: bd.show(m, main) for m in members}
    synthesize_batch_labels(members, epic, datas, main, actor)
    claim_group(cfg, hive, ",".join(members), as_)


def claim_group(cfg, hive, group_arg, as_):
    """Group-aware claim: one shared `wt/batch/<group>` worktree + identity for every member.
    Guards each member first (open, and not another actor's), resolves the shared group name from
    the existing `batch:<group>` labels, provisions/stamps the one batch worktree (forked off the
    members' molecule), then `bd update --claim`s each member as the single actor."""
    from . import bd, work_logic  # lazy: guards/_stamp live in work_logic; avoids the cycle

    members = _members(group_arg)
    entry, main, _target, _branch = worktree.locate(cfg, hive, members[0])
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    datas = {}
    for m in members:
        data = bd.show(m, main)
        work_logic._guard_open(data, m)
        work_logic._guard_not_other(data, actor, m)
        datas[m] = data
    group = resolve_group(members, datas)
    entry, target, branch = worktree.ensure(
        cfg, hive, branch=f"{BATCH_PREFIX}{group}", base_bead=members[0]
    )
    # Guard: the batch worktree MUST be checked out on its own wt/batch/<group>
    # branch. If it resolved onto another seat's dir (e.g. the coordinator's wt/bead/epic/<epic>,
    # which shares the leaf in collapsed mode), commits would silently land on the wrong branch and
    # `merge --group` would find no delta. Hard-fail rather than stamp+claim into the wrong tree.
    on = worktree.current_branch(target)
    if on != branch:
        typer.echo(
            f"✗ refusing to claim batch {group}: worktree {target} is on '{on}', not '{branch}'.\n"
            f"  A batch/collapsed session needs its OWN wt/batch/<group> worktree; this dir\n"
            f"  belongs to another seat. Committing here would land on the wrong branch.",
            err=True,
        )
        raise typer.Exit(1)
    work_logic._stamp(cfg, entry, target, actor)
    for m in members:
        if bd.run(["update", m, "--claim"], main, actor=actor).returncode != 0:
            raise typer.Exit(1)
        otel.count_bead_transition("claimed", {"bh.bead": m, "bh.batch": group})
    typer.echo(
        f"✓ claimed batch {group} ({len(members)} beads: {', '.join(members)}) as {actor}; "
        f"worktree {target}"
    )
    if not worktree.in_bead_worktree(target):
        typer.echo(
            f"\nWARNING: cwd is not the batch worktree — edits here target the wrong tree.\n"
            f'  → cd "{target}"  # the whole group is implemented in this one worktree',
            err=True,
        )


def merge_group(cfg, group_arg, hive, rm):
    """Land a work-group as ONE `--no-ff` bubble. Mirrors the single-bead merge guards per member
    (no changes-requested, no open gate), then — under the hive merge slot, held once — validates
    the shared `wt/batch/<group>` branch from a clean checkout, merges it `--no-ff` into the
    members' molecule base (per-bead commits preserved inside the one bubble), and closes every
    member. The history budget is RELAXED to ~per-bead-commits × members (a cohesive batch is
    several beads' worth of commits on one branch). The slot is released on every path; on conflict
    the merge aborts and nothing is closed — work is never dropped."""
    from . import bd, work_logic  # lazy: guards/_open_gate/_history_ok live in work_logic

    members = _members(group_arg)
    entry, main, _target, _branch = worktree.locate(cfg, hive, members[0])
    datas = {}
    for m in members:
        data = bd.show(m, main)
        work_logic._guard_open(data, m)
        datas[m] = data
    group = resolve_group(members, datas)

    for m in members:
        if bd.state(m, "review", main) == "changes-requested":
            typer.echo(f"✗ {m} has changes-requested — resume & resubmit, don't merge", err=True)
            raise typer.Exit(1)
        if work_logic._open_gate(m, main):
            typer.echo(f"✗ {m} review gate still open — batch not approved yet", err=True)
            raise typer.Exit(1)

    _entry, _main, target, branch = worktree.locate(cfg, hive, branch=f"{BATCH_PREFIX}{group}")
    if not worktree._branch_exists(main, branch):
        typer.echo(f"✗ no batch branch {branch} — was the group claimed?", err=True)
        raise typer.Exit(1)

    base = worktree.integration_base(entry, members[0], config.integration_branch(cfg, entry))
    count, subjects = worktree.history(entry, branch, base)
    if count == 0:
        # Distinguish 'work landed on the wrong branch' from a genuinely empty group so the
        # operator gets an actionable path instead of the generic submit message (ev1l).
        typer.echo(
            f"✗ batch {branch} has no commits over {base} — nothing to merge.\n"
            f"  If the group WAS implemented, its commits likely landed on the wrong branch\n"
            f"  (e.g. a coordinator seat wt/bead/epic/<epic>), not {branch}. Find them with:\n"
            f"      git -C {main} log --oneline {base}..wt/bead/epic/<epic>\n"
            f"  then move them onto {branch} (cherry-pick) and re-run; else abandon the group.",
            err=True,
        )
        raise typer.Exit(1)
    limit = config.max_commits(cfg, entry) * len(members)  # relaxed: per-bead-commits × members
    ok, msg = work_logic._history_ok(count, subjects, limit)
    if not ok:
        typer.echo(f"✗ {msg} — bounce back for self-refine", err=True)
        raise typer.Exit(1)

    started = time.perf_counter()
    with merge_slot(main):
        rc = worktree.clean_checkout(entry, branch, config.validate_cmd(cfg, entry))
        otel.count_validation(rc == 0, {"bh.batch": group, "bh.work.phase": "batch"})
        if rc != 0:
            typer.echo(f"✗ batch validation failed (exit {rc}) — nothing landed", err=True)
            raise typer.Exit(rc)

        prof = config.work_identity(cfg, entry)
        agent = prof["mode"] == "agent"
        mrc, out = worktree.merge_no_ff(
            entry,
            branch,
            base,
            name=(prof["name"] or "") if agent else "",
            email=(prof["email"] or "") if agent else "",
            signing_key=(prof["signing_key"] or "") if agent else "",
            sign=prof["sign"] if agent else False,
            message=f"chore(merge): batch {group}",
        )
        if mrc != 0:
            typer.echo(f"✗ batch merge failed — aborted, nothing landed:\n{out}", err=True)
            raise typer.Exit(mrc)
        for m in members:
            if bd.run(["close", m, "--reason", f"merged in batch {group}"], main).returncode != 0:
                typer.echo(f"⚠ merged but failed to close {m} — close it manually", err=True)

    otel.record_merge_duration(
        time.perf_counter() - started, {"bh.merge.kind": "batch", "bh.batch": group}
    )
    for m in members:
        otel.count_bead_transition("merged", {"bh.bead": m, "bh.batch": group})
    typer.echo(
        f"✓ merged batch {group} ({len(members)} beads: {', '.join(members)}) "
        f"({branch} --no-ff → {base}) and closed all members"
    )
    if rm:
        worktree.remove(hive, branch, force=True)
