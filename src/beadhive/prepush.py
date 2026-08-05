"""prepush.py — the pre-push fence hook (bh-ytbb.12): defence in depth against direct `bd`.

`bh work`'s write verbs are gated by `guard.guard_primary` (bh-ytbb.9): only the host holding
a hive's host lease may `assign`/`claim`/`submit`/`merge`. A raw `bd` invocation never goes
through that gate at all — it writes straight to the local Dolt replica, which is bounded
(bd repo sync cannot corrupt the remote; the real fence is at push, `host_fence.py`,
bh-ytbb.7) but still lets an operator build an hour of work on a doomed local state before
discovering their host was never primary.

This module closes that gap EARLY, at the one place a direct `bd dolt push` cannot avoid:
git's own `pre-push` hook. It reuses `guard.primary_state`'s cached-lease read (the exact
predicate `guard_primary` already uses) rather than inventing a second notion of "primary" —
so the hook and `bh work`'s own gate can never disagree.

**Local-only, always** (the acceptance bar): no network call, no HQ round trip — only the
local `refs/bh/lease/<prefix>` ref already cached in this host's HQ clone, the same read
`guard_primary` performs on every `bh work` write verb.

**Bypassable, on purpose, and documented as such.** `git push --no-verify` skips this hook
entirely. That is fine: the hook is a convenience — an early, legible refusal — not the
enforcement. The real backstop is the atomic `--force-with-lease` push fence beside the
hive's own data (`refs/bh/epoch`, `host_fence.py`, docs/design/multi-host-model-adr.md
Amendment 1 §2): a stale-epoch push is rejected there regardless of `--no-verify`.

**Two install locations, one hive — and only ONE of them ever fires for a data push.**
`bd dolt push`'s `git push` is issued from a HIDDEN bare repo nested under the database
directory, `<db>/.dolt/git-remote-cache/<hash>/repo.git` (`host_fence.transport_lookup`), in
**every** storage mode — bh's own push and a raw one both go through that same bd-internal
transport. Measured, not assumed: `bh-ukit.2` instrumented a real `bd dolt push` with a logging
hook in each candidate location, embedded and shared-server, and the hive checkout's hook never
fired in either. What differs by mode is only where that repo hangs off:

  1. **embedded** — under the hive, at `.beads/embeddeddolt/<db>/.dolt/git-remote-cache/…`;
  2. **bd's shared server** — outside the hive entirely, under the server's own data dir
     (`~/.beads/shared-server/dolt/<db>/…`), where the push runs inside the *server* process.

The hive's own hooks dir is still installed into — it is cheap, and it covers the storage shape
whose `refs/dolt/data` really does live in the wrapping repo (`safety.DoltRefInfo`'s
docstring) — but it is not what fences a data push under either mode above. An earlier version
of this docstring called that location "a non-embedded/server Dolt setup"; that was wrong, and
correcting it is part of what `bh-areg.6` was filed for.

That bare repo does not exist until bd creates it, lazily, on the first `bd dolt push` for
the hive — so a freshly-initialized hive that has never pushed bead data has nothing there
yet to hook. This is provably harmless: the multi-host model is never "in force"
(`guard.primary_state` returns `None`) until an `adopt` has happened, and an adopt can only
follow at least one host already having pushed bead data (a second host bootstraps FROM that
push — `onboard._origin_has_dolt_data`). By the time two hosts are contesting primacy, the
first push — and so the transport repo, and so this hook — already exists. `install_for_hive`
re-installs idempotently, so re-running `bh hive init` (or the second host's own onboard,
which bootstraps first) picks up any transport repo that has since appeared.

**Installed independent of the furnish axis** (`hive.py`'s declared-footprint convention,
bh-ytbb.12's spec-review note): a git hook is never tracked in a repo's git history — it lives
under `.git/` (or a bare repo dir) by construction, invisible to `git status`/`git add` either
way — so `furnish: none` has nothing to opt out of here. Every hive gets the hook, tracked or
not; this is a safety mechanism, not a convenience the ownership-gated furnish declaration was
ever meant to gate.
"""

from __future__ import annotations

from pathlib import Path

from . import config, guard, host_fence
from .run import run

HOOK_FILENAME = "pre-push"

# Stamped into every hook bh installs: lets a re-run tell "ours, safe to refresh" apart from
# an operator's own pre-existing pre-push hook, which is left untouched (non-destructive,
# mirroring hive.py's agent-extras installers — a safety mechanism must never clobber a
# repo's own tooling).
_MARKER = "# bh:prepush-fence (bh-ytbb.12) -- do not hand-edit; `bh hive init` regenerates this"

# Distinct from guard.PRIMARY_REFUSAL_MARKER / STALE_CLAIM_REFUSAL_MARKER (same convention):
# an operator staring at a blocked push needs to tell which of the guard's several refusals
# they hit, and grep/tests need a stable substring to key off.
PREPUSH_FENCE_REFUSAL_MARKER = "prepush fence: this host is not primary for"


def _git(args: list[str], cwd: Path):
    return run(["git", *args], cwd=str(cwd), check=False, capture=True)


def hook_script(hive: str) -> str:
    """The hook's shell body: a LOGIC-FREE shim that delegates to `bh hive hook pre-push`.

    Every decision — git's stdin protocol, the ``refs/dolt/data`` filter, the primary check,
    the exit codes — lives in that verb (bh-smcj,
    ``docs/design/hooks-as-functionality-adr.md``). This function used to build all of it as a
    shell string, which meant a second dispatcher had to transcribe the ref filter into a copy
    free to drift, and a drifted copy fails silently in both directions (fence every ordinary
    push, or never fence at all). Nothing here to drift now.

    ``hive`` is a hive **id** (prefix / triplet), not a path. A git hook cannot discover its
    owning hive from cwd — for the embedded-engine transport repo (module docstring) cwd is a
    bare repo nested under ``.beads/``, not the hive — so something must be baked in. An id
    survives the hive being moved or re-cloned; the absolute path this used to embed did not.
    The verb resolves id -> path at RUN time via ``registry.hive_dir_for``.

    ``${BH_EXEC:-bh}`` matches ``lefthook.yml``: the released binary by default, overridable to
    ``uv run bh`` in the repo that authors bh, where the installed binary can lag the tree."""
    hive_sh = str(hive).replace("'", "'\\''")  # single-quote-safe for POSIX sh
    return (
        "#!/bin/sh\n"
        f"{_MARKER}\n"
        "# Refuses a refs/dolt/data push when this host's cached multi-host lease shows it is\n"
        "# NOT primary. A LOCAL-ONLY read, never a network round trip. Bypass with\n"
        "# `git push --no-verify`: that is fine, this hook is a FAST-FAIL CONVENIENCE, not the\n"
        "# enforcement. The atomic --force-with-lease push fence beside the hive's own data\n"
        "# (refs/bh/epoch, docs/design/multi-host-model-adr.md Amendment 1 §2) is the real\n"
        "# backstop and rejects a stale-epoch push regardless of --no-verify. That is why this\n"
        "# hook is opt-in (`bh hive hook install`) and no longer furnished automatically.\n"
        "\n"
        f"exec ${{BH_EXEC:-{config.BINARY_ALIAS}}} hive hook pre-push '{hive_sh}'\n"
    )


def _is_ours(text: str) -> bool:
    return _MARKER in text


def _hooks_dir(repo_path: Path) -> Path | None:
    """The hooks directory `git` itself would use for the repo at `repo_path` (respects
    `core.hooksPath` and gitdir-file indirection — `git rev-parse --git-path hooks`, not a
    hardcoded `.git/hooks`). `None` when `repo_path` isn't a git repo (nothing to install
    into, e.g. a transport repo bd hasn't created yet)."""
    res = _git(["rev-parse", "--git-path", "hooks"], repo_path)
    if getattr(res, "returncode", 1) != 0:
        return None
    rel = (res.stdout or "").strip()
    if not rel:
        return None
    return repo_path / rel  # Path.joinpath: an absolute `rel` (custom hooksPath) wins outright


def _write_hook(hooks_dir: Path, hive: str) -> str:
    """Install/refresh the fence shim in `hooks_dir` for hive id `hive`. Non-destructive: a
    foreign (unmarked) `pre-push` already there is left alone entirely. Returns a one-word
    status: "installed" | "refreshed" | "unchanged" | "skipped (custom hook present)"."""
    target = hooks_dir / HOOK_FILENAME
    content = hook_script(hive)
    if target.exists():
        try:
            existing = target.read_text()
        except OSError:
            return "skipped (unreadable existing hook)"
        if not _is_ours(existing):
            return "skipped (custom hook present)"
        if existing == content:
            return "unchanged"
        target.write_text(content)
        target.chmod(target.stat().st_mode | 0o111)
        return "refreshed"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    target.chmod(0o755)
    return "installed"


def install_for_hive(hive_dir: Path, hive: str) -> list[str]:
    """Install the fence shim for `hive_dir` — see the module docstring for the two locations.
    Returns one `"<hooks dir>: <status>"` line per location actually touched (a not-yet-created
    transport repo contributes nothing, silently — see module docstring).

    **OPT-IN as of bh-smcj.** This is no longer furnished by `bh hive init`/onboard; it runs
    only when an operator asks for it via `bh hive hook install`. The hook is a fast-fail
    convenience in front of the real `--force-with-lease` epoch fence, not the enforcement, so
    defaulting it OFF costs an early refusal and nothing else — while keeping bh out of the
    business of installing hook files behind your back
    (`docs/design/hooks-as-functionality-adr.md`).

    `hive_dir` is resolved to an ABSOLUTE path first: the transport-repo copy is discovered
    relative to it, and onboard-style callers can pass `Path(".")`. `hive` is the hive **id**
    baked into the shim — see :func:`hook_script` for why an id rather than a path."""
    hive_dir = Path(hive_dir).resolve()
    statuses: list[str] = []
    main_hooks = _hooks_dir(hive_dir)
    if main_hooks is not None:
        statuses.append(f"{main_hooks}: {_write_hook(main_hooks, hive)}")
    lookup = host_fence.transport_lookup(hive_dir)
    for repo in lookup.repos:
        repo_hooks = _hooks_dir(repo)
        if repo_hooks is not None:
            statuses.append(f"{repo_hooks}: {_write_hook(repo_hooks, hive)}")
    if not lookup.ok:
        # The one empty-lookup state that is NOT benign: there IS a transport repo, it is just
        # on another machine, so this host can never hook it. Reported rather than swallowed —
        # a silently un-hooked fence is the failure bh-areg.6 exists to end.
        statuses.append(f"(no hook installed): {lookup.detail}")
    return statuses


def check_fence(hive_dir: Path, *, cfg=None) -> tuple[bool, str]:
    """`(ok, detail)` for a `refs/dolt/data` push attempted from/for `hive_dir`.

    `ok=True` → allow: either the multi-host model isn't in force for this hive (nothing
    adopted — single-host default) or this host currently holds the cached lease. `ok=False`
    → refuse; `detail` is the operator-facing message (empty on allow).

    The SAME predicate `guard_primary` uses for `bh work`'s write verbs
    (`guard.primary_state`'s cached-lease read: local-only, no HQ round trip) — applied here
    to a git push itself is about to make, which is exactly the gap `bh work`'s own gate
    cannot close (a direct `bd dolt push` never goes through it)."""
    cfg = cfg if cfg is not None else config.load()
    state = guard.primary_state(cfg=cfg, hive_dir=hive_dir)
    if state is None:
        return True, ""  # multi-host not in force here -- nothing to gate
    prefix, this_host, lease = state
    if lease.held_by(this_host):
        return True, ""
    held = "nobody currently holds it" if lease.is_tombstone else f"held by {lease.describe()}"
    detail = (
        f"✗ {PREPUSH_FENCE_REFUSAL_MARKER} {prefix} — {held}.\n"
        "  A direct `bd dolt push` bypasses bh's own write guard (bh-ytbb.9) -- this hook is\n"
        "  catching it here instead: writing bead data from a host that isn't primary risks\n"
        "  building on a doomed local state (a re-adopted primary's push fence will refuse\n"
        "  this data anyway).\n"
        "  Re-adopt this hive on THIS host before pushing, or coordinate with the current\n"
        "  primary named above.\n"
        f"  `git push --no-verify` bypasses ONLY this hook, not real enforcement: the atomic\n"
        f"  --force-with-lease push fence beside the hive's own data ({host_fence.EPOCH_REF},\n"
        "  docs/design/multi-host-model-adr.md Amendment 1 §2) is the actual backstop, and a\n"
        "  stale-epoch push is rejected there regardless of --no-verify."
    )
    return False, detail
