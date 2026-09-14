"""prepush.py — the legacy/compatibility pre-push fence hook (bh-ytbb.12).

`bh work`'s write verbs are gated by `guard.guard_primary` (bh-ytbb.9): only the host holding
a hive's host lease may `assign`/`claim`/`submit`/`merge`. A raw `bd` invocation never goes
through that gate and can write or publish state outside bh's control.

This module preserves a hook contract for Git transports that permit hooks. Shipped bd does
not: trace2 proves it invokes real Git with ``core.hooksPath=/dev/null`` in both embedded and
shared-server modes. Therefore this hook is not a boundary a direct `bd dolt push` must pass,
and nothing here is represented as enforcement authority.

**Local-only, always** (the acceptance bar): no network call, no HQ round trip — only the
local `refs/bh/lease/<prefix>` ref already cached in this host's HQ clone, the same read
`guard_primary` performs on every `bh work` write verb.

**Bypassable and diagnostic only.** `git push --no-verify` skips it, and current bd suppresses
it unconditionally. The managed boundary is the remote CAS reservation plus postflight around
``Engine.push_state``; it is sequenced rather than atomic, and raw bd bypasses it too.

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
correcting it is part of what `bh-areg.6` was filed for. Neither this location nor the hidden
transport is effective under current bd's explicit hooks-path override.

That bare repo does not exist until bd creates it, lazily, on the first `bd dolt push` for
the hive — so a freshly-initialized hive that has never pushed bead data has nothing there
yet to hook. `install_for_hive` re-installs idempotently after it appears, but this changes only
diagnostic compatibility; safety never depends on its presence or execution.

**A SECOND, UNRELATED PRE-PUSH BEHAVIOUR LIVES HERE TOO** (bh-ku9n9.5):
:func:`check_push_main`, the attested-green lookup the main-merge gate consults before it
spends ~371s re-running `just check-all` on a tree something already proved. It shares nothing
with the fence above except the git lifecycle point — and, deliberately, the shape: a
`(ok, detail)` predicate with the whole hook contract in a verb (`bh hive hook push-main`), so
neither hook file owns logic that can drift from bh's own notion of the gate.

The two are opposite in polarity, which is the thing to keep straight when editing either:
the fence REFUSES a push (ok=False blocks), while the push-main lookup only ever REMOVES WORK
(ok=True skips the gate; ok=False means "run it, exactly as before this existed"). So the
fence must fail OPEN on "nothing to fence", and the lookup must fail CLOSED on everything —
miss, stale entry, invalid record, unconfigured phase, or any exception at all.

**Installed independent of the furnish axis** (`hive.py`'s declared-footprint convention,
bh-ytbb.12's spec-review note): a git hook is never tracked in a repo's git history — it lives
under `.git/` (or a bare repo dir) by construction, invisible to `git status`/`git add` either
way — so `furnish: none` does not prevent an explicit `bh hive hook install`. Installation is
opt-in; this is compatibility tooling, not remote write authority.
"""

from __future__ import annotations

import datetime
import shlex
from pathlib import Path

from . import config, guard, host_fence, registry, validation_ledger
from .run import run

HOOK_FILENAME = "pre-push"

# The named phase the main-push gate resolves through (bh-ku9n9.5). A plain value in the
# free-form `work.validate` map `config.validate_cmd` already reads — no schema change.
PUSH_MAIN_PHASE = "push-main"

# Stamped into every hook bh installs: lets a re-run tell "ours, safe to refresh" apart from
# an operator's own pre-existing pre-push hook, which is left untouched (non-destructive,
# mirroring hive.py's agent-extras installers — compatibility tooling must never clobber a
# repo's own hook).
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
        "# `git push --no-verify`, and current bd forces core.hooksPath=/dev/null. This hook\n"
        "# is compatibility/diagnostic tooling, not enforcement. Managed bh publication uses\n"
        "# a sequenced remote fence CAS plus postflight; raw bd remains outside that boundary.\n"
        "# That is why this hook is opt-in (`bh hive hook install`).\n"
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
    only when an operator asks for it via `bh hive hook install`. This legacy hook is a
    diagnostic fast-fail only: shipped bd suppresses it. Managed reserve-before-bd plus exact
    postflight verification is the current boundary: bh CASes the remote epoch ref, invokes bd,
    then checks the same reservation. That sequence has a non-atomic CAS→push window, and raw
    OS-level bd bypasses it entirely. Defaulting the hook OFF therefore removes no managed
    authority while keeping bh out of the business of installing hook files behind your back
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
        "  This compatibility hook observed a refs/dolt/data push from a host that is not\n"
        "  primary and refuses it. Current bd normally forces core.hooksPath=/dev/null, so do\n"
        "  not treat this hook as authority.\n"
        "  Re-adopt this hive on THIS host before pushing, or coordinate with the current\n"
        "  primary named above.\n"
        f"  Managed bh publication reserves {host_fence.EPOCH_REF} by remote CAS before bd\n"
        "  and verifies it afterward. Raw bd and --no-verify can bypass this hook; publish\n"
        "  through `bh hive sync remotes --push` (ADR Amendment 1 §2)."
    )
    return False, detail


def push_main_cmd(cfg, entry, gate_cmd: str = "") -> tuple[str, str]:
    """`(cmd, "")` — the command `work.validate.push-main` names for `entry` — or `("", detail)`
    when there is no usable one. **The single resolver for the `push-main` phase**, shared by the
    pre-push lookup below and by the release flow (`release.py`, bh-ku9n9.7), so a bump and a push
    can never disagree about which command a verdict has to have been earned under.

    Two refusals, both of which must read as a hard miss rather than a lenient default:

    * **Unconfigured.** `config.validate_cmd` falls back to `work.validate_cmd` (`just check` by
      default) for an unset phase, and honoring a verdict earned by the *fast* gate as though it
      were the *full* one is exactly the ambiguity this epic exists to refuse. So an absent key
      resolves to nothing at all rather than to the fallback.
    * **Mismatched.** `gate_cmd` is what the caller says it will run on a miss. When given, the
      resolved phase must equal it exactly — a `push-main` naming a *different* (and possibly
      weaker) command than the caller runs is a verdict about some other gate."""
    per = config.work_value(cfg, entry, "validate", {}) or {}
    if PUSH_MAIN_PHASE not in per:
        target = str(entry.get("prefix") or "?")
        requested = gate_cmd.strip()
        remedy = shlex.join(
            [
                config.BINARY_ALIAS,
                "config",
                "set",
                f"work.validate.{PUSH_MAIN_PHASE}",
                requested,
                "--scope",
                "fleet",
            ]
        )
        detail = (
            f"• no `work.validate.{PUSH_MAIN_PHASE}` configured for hive "
            f"{target} — nothing to look a verdict up under.\n"
            f"  Target hive: {target}. Requested gate command: {requested!r}.\n"
            "  Validation policy is fleet-owned so every host proves releases under the same "
            "command; host scope is not valid here."
        )
        if requested:
            detail += f"\n  Configure it explicitly: `{remedy}`"
        else:
            detail += "\n  Supply the gate command so an exact fleet-scoped remedy can be shown."
        return "", detail
    cmd = config.validate_cmd(cfg, entry, phase=PUSH_MAIN_PHASE)
    if gate_cmd and gate_cmd.strip() != cmd.strip():
        return "", (
            f"• work.validate.{PUSH_MAIN_PHASE} is {cmd!r} but this gate runs {gate_cmd!r} — a "
            f"verdict earned under a different command says nothing about this one. Point the "
            f"phase at the gate's own command.\n"
            f"  Target hive: {entry.get('prefix', '?')}. Configured command: {cmd!r}. "
            f"Requested command: {gate_cmd.strip()!r}."
        )
    return cmd, ""


def check_push_main(
    rev: str, hive_id: str = "", gate_cmd: str = "", on_miss: str = "gate runs"
) -> tuple[bool, str]:
    """`(ok, detail)` for "has the tree at `rev` already been proved green by the `push-main`
    gate?" — the main-push gate's lookup (bh-ku9n9.5, `docs/design/attested-green-adr.md`).

    `ok=True` means, and only ever means: the ledger holds a FRESH GREEN verdict for the EXACT
    tree this push would land, earned under the exact command this gate would otherwise run.
    The caller may then skip that command. **Every other outcome is `ok=False`, which means run
    the full gate inline exactly as before this function existed** — a miss, a stale entry, a
    red verdict, an invalid record, an unconfigured phase, no hive, no clone, a corrupt config,
    an exception of any kind. There is no path through here where a missing, unreadable, or
    ambiguous attestation produces `True`; the worst case is current behaviour.

    **And a hive's declared `work.always_run` set is one more way to get `False`** (bh-ehmd8):
    `green_verdict` runs it before it hands back a hit, because a tree hash vouches for file
    content and this gate's own release pin reads git *metadata*. A failing set means `ok=False`
    and the full gate runs — the same safe side as every other outcome above. The rule lives in
    `green_verdict`, so this gate and every landing boundary get it from one place.

    That asymmetry is why the `except Exception` below is correct rather than lazy: the failure
    mode of this lookup is "we ran the 371s gate we would have run anyway", so swallowing a
    surprise and falling through is strictly safer than propagating it (which, from a git hook,
    would BLOCK a push that today succeeds).

    **The phase, and why it is required rather than defaulted.** The command is resolved through
    `config.validate_cmd(..., phase="push-main")` — so the outermost gate participates in
    `work.validate.<phase>` like every other validation point, keyed on the same
    `(tree, cmd_hash)` the land-time `molecule` / `merge-main` runs write. `push-main` must be
    EXPLICITLY configured: unset, `validate_cmd` falls back to `work.validate_cmd` (`just check`
    by default), and honoring a verdict earned by the *fast* gate would let a push skip the
    *full* one. Unconfigured therefore looks up nothing at all and the gate runs — the tier
    contract's "a hive that supports nothing gets exactly today's behaviour".

    **`gate_cmd`** is what the caller says it will run on a miss. When given, the resolved phase
    must equal it exactly or the lookup refuses: a `push-main` key naming a *different* (and
    possibly weaker) command than the hook actually runs is precisely the "ambiguous
    attestation" case, and it must read as a loud miss rather than a quiet pass.

    **`on_miss`** names only what the CALLER does with a `False` — "gate runs" for the hook,
    "the bump is refused" for the release pre-flight (bh-ku9n9.7). It changes wording, never the
    verdict: every caller's `False` is the safe side of its own decision, which is the reason one
    predicate can serve both.

    WHAT THIS DOES NOT SOLVE: only the HIT path is fast. A miss still runs ~371s inside the
    push, holding the connection git opened before the hook started, so the SSH keepalive from
    bh-53o8f (`scripts/push-main.sh` / `just push`) is still required — see that script's
    header. This makes the expensive path rarer; it does not make it safe to run bare."""
    try:
        cfg = config.load()
        entry = registry.resolve_hive(cfg, hive_id) if hive_id else registry.current_hive(cfg)
        if not entry:
            return False, f"• no managed hive for this push ({hive_id or 'cwd'}) — {on_miss}"
        cmd, refusal = push_main_cmd(cfg, entry, gate_cmd)
        if refusal:
            return False, f"{refusal} ({on_miss})"
        hit = validation_ledger.green_verdict(entry, rev, cmd, cfg=cfg)
        # Re-assert the centralized typed predicate at this outermost boundary. This deliberately
        # checks more than legacy `rc == 0`: typed red/none and contradictory manifests refuse.
        if not hit or not validation_ledger.is_qualifying_green(hit):
            return False, f"• no fresh green {PUSH_MAIN_PHASE} verdict for {rev[:12]} — {on_miss}"
        # Formatting stays INSIDE the try anyway (bh-ku9n9.19): `_is_fresh` now rejects a future
        # `at` outright, so nothing that reaches here should be unformattable — but this whole
        # function's contract is "an exception here means a miss, never a pass, never a raise
        # out of a hook", and there is no reason to make that depend on the ledger's invariant
        # holding.
        when = datetime.datetime.fromtimestamp(hit["at"]).astimezone().isoformat(timespec="seconds")
        return True, (
            f"✓ attested green: tree {str(hit.get('tree', ''))[:12]} already passed {cmd!r} at "
            f"{when} — skipping the full gate for {rev[:12]}"
        )
    except Exception as exc:  # noqa: BLE001 — ANY failure means "run the gate", never "pass"
        return False, f"• verdict lookup failed ({type(exc).__name__}: {exc}) — {on_miss}"
