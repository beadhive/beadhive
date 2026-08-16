"""`bh release` — the release plane: the advisory merge order (bh-k2j8) and the guarded
version-bump flow (bh-ku9n9.7).

`bh release order` renders the strategy-preferred merge sequence the merger consults instead of
FCFS. It is strictly advisory and read-only — it reads the gated-ready set (`bd ready --gated`,
the beads whose review gate cleared) and orders it through the same scorer that sorts
`bh work ready --gated` (`release_order`), so the two never disagree about the sequence. The hard
counterpart — the `release-hold:` gate that blocks a `release:breaking` bead until a releaser
clears it — lives in plan/guard/work, not here.

THE BUMP FLOW — `preflight` / `attest` / `await` / `recover` / `preview` (bh-ku9n9.7, bh-0jndj)
==============================================================================================

This is the attested-green design (`docs/design/attested-green-adr.md`) applied one level up,
to the release itself. It exists because of a measured incident, the 0.11.5 release: a bump was
made, the push failed three times — once on the SSH socket dying mid-gate, twice on a genuinely
red suite — and each failure left a bump commit and a `v0.11.5` tag on local main with nothing
on the remote. Recovery was improvised mid-incident (bh-67utw).

**THE INVERSION, which is the whole point.** bh-67utw's rule is that a failed push is undoable
to a clean slate *if and only if the tag never left*. So the bump is the last safely reversible
moment, and green must be proven BEFORE it — never inside the push, where it is discovered only
after a tag already exists. Pushing main becomes a short check that green was already proven,
which is also what removes the ~371s idle socket that killed the connection (bh-53o8f).

    ┌─ green already proven for HEAD's tree?   `bh release preflight`   REFUSES if not
    │      (the land-time run under `work.validate.push-main` wrote it; nothing new runs here)
    ├─ `cz bump` — writes pyproject.toml + CHANGELOG.md + uv.lock + the tag
    │      ⇒ A NEW TREE, WITH NO ATTESTATION. This is the hole bh-1owpi named and did not
    │        solve; it is why a full gate landed at exactly the moment it hurt most.
    ├─ `bh release attest --background` — fires the gate on THAT new tree, detached, now
    ├─ `bh release await` — blocks on the verdict. GREEN ⇒ push. RED ⇒ refuse, undo is
    │      still safe because nothing has left.
    └─ ONE atomic push of main AND its tag (`git push --atomic`, scripts/push-main.sh) —
           both or neither, so "main landed without its tag" is not a reachable state.
       ══════════ THE TAG IS THE POINT OF NO RETURN ══════════
           everything above is reversible; nothing below is. `bh release recover` decides
           which side of that line you are on by MEASURING the remote with ls-remote.

**TWO READ-ONLY COMPANIONS, and one flag (bh-0jndj).** `attest --if-needed` is prove-or-skip
(`clean_checkout(reuse=True)`), which is what makes "prove this tree if it is not already proven"
something you can ask for deliberately — `just attest` — rather than only ever getting as a side
effect of whatever `bh work merge` last ran. `preview` is the forward-facing counterpart to
`recover`: the same ls-remote measurement, asked BEFORE the door instead of after it, plus the
green lookup and a PyPI check. It REPORTS all three and refuses none of them, because `preflight`
already owns refusing and a second refuser reachable by a different name is the confusion these
verbs exist to remove. Neither establishes anything the flow did not already establish.

**WHAT THIS MODULE DOES NOT OWN.** The undo *rewrite* itself (`git rebase --rebase-merges
--onto`, the backup ref, the only-version-files diff) is bh-67utw; `recover` measures and
decides, then names it. The general-purpose `just push` tag handling and its "unfinished
release" report is bh-zfvbp; this module hands `scripts/push-main.sh` the tag and that script
pushes both atomically.

**THE SAFETY PROPERTY, unchanged from bh-ku9n9.5.** There is no path here where a missing,
stale, red, corrupt, or ambiguous attestation lets a bump or a push proceed as though green were
proven. Every verb's failure direction is refusal, and the phase resolver is literally the same
function the pre-push hook uses (`prepush.push_main_cmd`), so the two can never disagree about
which command a verdict must have been earned under.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import typer

from . import bd, config, registry, validation_ledger, worktree
from . import release_order as ro

# `prepush` is imported per-call, not here: it pulls `guard` + `host_fence` (~14ms measured) and
# `cli.py` imports THIS module at startup, so a top-level import would tax every `bh` invocation
# for four verbs nobody runs in a loop. Same choice cli.py already makes for its own hook verb.

app = typer.Typer(
    no_args_is_help=True, help="Release plane: merge-order views and the guarded bump flow."
)

_HIVE = typer.Option("", "--hive", help="target hive (default: cwd's hive)")
_REV = typer.Argument("HEAD", metavar="REV", help="the revision whose TREE the verdict is about")
_GATE = typer.Option(
    "",
    "--gate",
    metavar="CMD",
    help="the command the caller believes the gate is; `work.validate.push-main` must resolve to "
    "exactly this or every verb here refuses (a phase naming a weaker command is not a verdict "
    "about this gate)",
)

#: The background bump-gate marker, beside the ledger in the hive's git dir. Untracked, local,
#: dies with the clone — it holds no verdict, only enough to tell "still running" from "died
#: without recording one". The VERDICT always comes from the ledger.
BUMP_GATE_FILENAME = "bh-release-bump-gate.json"
BUMP_GATE_LOG = "bh-release-bump-gate.log"


def _impact_tag(bead: dict) -> str:
    """A compact `release:/wave:` tag for a bead's order line ('unclassified' when unlabeled)."""
    impact = ro.release_impact(bead)
    if not impact:
        return "unclassified"
    wave = ro.wave_name(bead)
    return f"{impact}" + (f" (wave:{wave})" if wave else "")


@app.command("order")
def order(hive: str = _HIVE):
    """Show the strategy-preferred merge sequence over the gated-ready set — read-only, advisory.

    Consults the same scorer that sorts `bh work ready --gated` (`release.strategy` /
    `release.fix_churn_budget`), so the merger sees the order it would merge in. Unclassified ready
    beads (no `release:` label) list after the ordered ones. Empty when nothing is gated-ready."""
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    entry = registry.entry_for_dir(cfg, cwd)
    strategy = config.release_strategy(cfg, entry)
    budget = config.release_fix_churn_budget(cfg, entry)

    beads = bd.json(["ready", "--gated", "--limit", "0"], cwd) or []
    by_id = {str(b.get("id") or ""): b for b in beads}
    sequence = ro.merge_sequence(beads, strategy=strategy, fix_churn_budget=budget)

    typer.echo(f"release order — strategy: {strategy}, fix_churn_budget: {budget}")
    if not sequence:
        typer.echo("  (nothing gated-ready to order)")
        return
    for n, bead_id in enumerate(sequence, 1):
        typer.echo(f"  {n}. {bead_id}  [{_impact_tag(by_id.get(bead_id, {}))}]")


# ── the guarded bump flow (bh-ku9n9.7) ────────────────────────────────────────────────────────
#
# EXIT-CODE CONTRACT, one shape for every verb below, because a release decision that cannot
# tell its three answers apart is how the 0.11.5 incident produced a confident wrong sentence
# (bh-dt2d9, scripts/push-main.sh's header):
#
#     0  proven — a measured fact says yes
#     1  refused — a measured fact says no
#     2  the half-done release: main is published, its tag is not (recover only)
#     3  COULD NOT MEASURE — never folded into 1. "I could not look" is its own answer.

REFUSED, HALF_DONE, UNMEASURABLE = 1, 2, 3


def _resolve(hive_id: str, gate_cmd: str):
    """`(entry, cmd, "")` for the target hive's `push-main` phase, or `(None, "", detail)`.

    The one entry point every verb below shares — and it resolves the command through
    `prepush.push_main_cmd`, the SAME resolver the pre-push hook uses, so a bump can never be
    proven against a command a push would not accept.

    The blanket `except` matches `check_push_main`'s and for the same reason: every caller's
    failure direction is REFUSE, so swallowing a surprise into a refusal is strictly safer than
    letting it out — and a traceback is a worse thing to hand an operator mid-release than a
    sentence naming what broke."""
    from . import prepush

    try:
        cfg = config.load()
        entry = registry.resolve_hive(cfg, hive_id) if hive_id else registry.current_hive(cfg)
        if not entry:
            return None, "", f"• no managed hive for {hive_id or 'cwd'}"
        cmd, refusal = prepush.push_main_cmd(cfg, entry, gate_cmd)
        return (None, "", refusal) if refusal else (entry, cmd, "")
    except Exception as exc:  # noqa: BLE001 — ANY failure means "refuse", never "proceed"
        return None, "", f"• could not resolve the gate ({type(exc).__name__}: {exc})"


def _marker_path(entry) -> Path | None:
    """The bump-gate marker file, or None when there's no plain `.git` dir to keep it in — the
    same siting rule (and the same "then callers just fall back") as the ledger's own file."""
    git_dir = registry.hive_dir(entry) / ".git"
    return git_dir / BUMP_GATE_FILENAME if git_dir.is_dir() else None


def _read_marker(entry) -> dict:
    """The marker as a dict; `{}` on absent/corrupt/wrong-shape — a marker that cannot be read
    is a marker that is not there, and "not there" is never permission for anything.

    The `is_file()` guard matters beyond ruling out a directory: a FIFO at this path makes
    `Path.read_text()` block forever rather than raise, which neither exception clause below
    catches — inherited from bh-ku9n9.7, shared by both callers of this function through
    `_marker_for_tree`. Nothing creates a FIFO here today, but "unreadable in bounded time" is
    exactly the "not there" case this function already exists to collapse everything else into,
    and a `just push` that never returns is the worst kind of failure to debug."""
    path = _marker_path(entry)
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _marker_for_tree(entry, rev: str) -> tuple[dict | None, str]:
    """`(marker, tree)` for REV — `marker` is None when there is none, it's unreadable, or it
    names a different tree than REV's own (always returned too, so a caller needing it for a
    message doesn't resolve it twice).

    THE ONE TEST. `await` waits on this and `just push`'s pre-flight refusal (bh-8c2yo) answers
    from it immediately, without waiting — factored out so the two can never disagree about
    what "a bump gate is pending for this tree" means."""
    tree = validation_ledger.tree_of(entry, rev)
    marker = _read_marker(entry)
    return (marker if marker.get("tree") == tree else None), tree


def _still_running(marker: dict) -> bool | None:
    """True/False for "is the background gate process alive?", or **None for "cannot tell"** —
    a marker from another host, or one with no usable pid. Cannot-tell is deliberately its own
    answer: `await` keeps waiting on it rather than declaring a run dead that may be fine."""
    from . import host

    pid = marker.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if marker.get("host") and marker.get("host") != host.host_id():
        return None  # someone else's pid space — this host's os.kill would answer about nothing
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None  # it exists but isn't ours, or the query failed: not evidence of death
    return True


@app.command("preflight")
def preflight(rev: str = _REV, gate: str = _GATE, hive: str = _HIVE):
    """PROVE this tree is already green, then let the bump happen. Exit 0 = proven; refuse other.

    The pre-flight check for `cz bump`, and the reason the whole flow is ordered the way it is
    (bh-67utw): a bump is only safely reversible until its tag leaves, so the expensive proof
    belongs BEFORE it, not inside the push that discovers a red suite after a tag already exists.

    **THIS VERB NEVER ESTABLISHES GREEN — it only reads a verdict.** That is not a limitation,
    it is the design: the land-time run (`work.validate.molecule` / `.merge-main`) already tested
    this exact tree, and pointing `work.validate.push-main` at the same command is what makes
    that run count here for free (`docs/design/attested-green-adr.md`). If you need to establish
    green rather than reuse it, that is `bh release attest`, which is a separate act on purpose.

    Exit 1 on **everything else** — a miss, a stale entry, a red verdict, a malformed record, an
    unconfigured or mismatched `work.validate.push-main`, no hive, an unresolvable rev, any
    exception. There is no flag that turns a refusal into a pass; the way to bump without proof
    is to not call this verb, which is a visible choice rather than a silent one."""
    from . import prepush

    ok, detail = prepush.check_push_main(
        rev, hive_id=hive, gate_cmd=gate, on_miss="THE BUMP IS REFUSED"
    )
    typer.echo(detail, err=not ok)
    if ok:
        return
    typer.echo(
        "\n✗ refusing to bump: nothing proves this tree is green.\n"
        "  A bump is the LAST safely reversible moment (bh-67utw) — once its tag reaches the\n"
        "  remote the release workflow may already have fired and the undo path is closed. So\n"
        "  green is proven here, before the tag exists, not inside the push that would discover\n"
        "  a red suite too late.\n"
        "  Either land through `bh work merge` (its clean-checkout run writes the verdict this\n"
        "  reads) or establish one directly:\n"
        f"      {config.BINARY_ALIAS} release attest {rev}",
        err=True,
    )
    raise typer.Exit(REFUSED)


@app.command("attest")
def attest(
    rev: str = _REV,
    gate: str = _GATE,
    hive: str = _HIVE,
    background: bool = typer.Option(
        False,
        "--background",
        help="fire the gate detached and return immediately; `bh release await` blocks on the "
        "verdict later. This is what the bump uses — see the verb's docstring.",
    ),
    if_needed: bool = typer.Option(
        False,
        "--if-needed",
        help="skip the run when this tree already has a fresh green verdict for the gate command "
        "— prove-or-skip, which is what makes `attest` idempotent and cheap to warm a tree with. "
        "A miss still runs the full gate. Ignored under `--background`, whose whole job is a tree "
        "that by construction has no verdict.",
    ),
):
    """RUN the `push-main` gate against REV's tree from a clean checkout and record the verdict.

    **Why the bump needs this at all.** `cz bump` writes `pyproject.toml`, `CHANGELOG.md` and
    `uv.lock`, so the release commit is a NEW TREE that nothing has ever attested — a guaranteed
    full-gate miss at precisely the moment it costs most. The fix is the same pattern one level
    up: fire the gate on the new tree the instant it exists (`--background`), do the rest of the
    release meanwhile, and have the push WAIT on that verdict instead of establishing green
    inside a hook holding an idle socket open (bh-53o8f).

    The run itself is `worktree.clean_checkout` — the sound ledger writer: a throwaway detached
    worktree at REV, the hive's `verify: true` init rules run against that checkout so the
    environment is established FROM THE TREE, then the command, then the verdict recorded under
    the tree that actually validated. Nothing bespoke; a bump-tree attestation is the same object
    a land-time run produces, which is exactly why `preflight` and the pre-push hook can read it.

    `--background` detaches (`start_new_session`) with output to `<git-dir>/`
    `bh-release-bump-gate.log` and drops a marker naming the tree, command and pid. It carries NO
    verdict —
    it exists only so `await` can tell "still running" from "died without recording one". The
    verdict is always the ledger's.

    `--if-needed` makes the verb IDEMPOTENT — `clean_checkout(reuse=True)`, the same prove-or-skip
    every landing boundary already uses, so a fresh green verdict for this exact (tree, command)
    short-circuits the run and a miss pays the full gate. That is not a second lookup and not a
    weaker proof: a hit under a (tree, cmd_hash) key IS an exact tree match. It exists so "prove
    this tree if it is not already proven" is something you can deliberately ask for (`just
    attest`) instead of only ever getting it as a side effect of a land-time `bh work merge`.
    The default stays OFF, because the verb's designed job — attesting a just-written bump tree —
    has no verdict to reuse and must never look as though it did."""
    entry, cmd, refusal = _resolve(hive, gate)
    if refusal:
        typer.echo(f"{refusal} — cannot attest", err=True)
        raise typer.Exit(REFUSED)
    main = registry.hive_dir(entry)
    # `--verify <rev>^{commit}`, not a bare rev-parse: a bare one ECHOES BACK any 40-hex string
    # unchanged with rc 0, so a typo'd sha would be "resolved", fail its checkout, and record a
    # red verdict under a key naming no content at all. Attesting is a WRITE — it must know the
    # object exists before it claims to have tested it.
    res = subprocess.run(
        ["git", "-C", str(main), "rev-parse", "--verify", "-q", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = (res.stdout or "").strip()
    if res.returncode != 0 or not sha:
        typer.echo(f"✗ cannot resolve {rev!r} to a commit in {main} — nothing to attest", err=True)
        raise typer.Exit(REFUSED)

    if not background:
        rc = worktree.clean_checkout(entry, sha, cmd, reuse=if_needed)
        typer.echo(
            f"{'✓ attested green' if rc == 0 else f'✗ RED (exit {rc}) — recorded, not attested'}"
            f": {sha[:12]} under {cmd!r}",
            err=rc != 0,
        )
        raise typer.Exit(0 if rc == 0 else REFUSED)

    marker = _marker_path(entry)
    if marker is None:
        typer.echo(
            f"✗ {main} has no plain .git dir — nowhere to track a background gate. Run "
            f"`{config.BINARY_ALIAS} release attest` in the foreground instead.",
            err=True,
        )
        raise typer.Exit(REFUSED)
    from . import host

    log = marker.with_name(BUMP_GATE_LOG)
    # `sys.argv[0]` rather than a hardcoded name: the child must be the SAME bh the operator just
    # invoked. In this repo that is often `uv run bh` from source, where the installed binary lags
    # the tree — spawning `bh` by name there would gate with different code than it was asked to.
    argv0 = sys.argv[0] or config.BINARY_ALIAS
    cmdline = [argv0, "release", "attest", sha, "--gate", cmd, "--hive", str(entry["prefix"])]
    with open(log, "wb") as fh, open(os.devnull, "rb") as devnull:
        proc = subprocess.Popen(
            cmdline, cwd=str(main), stdin=devnull, stdout=fh, stderr=fh, start_new_session=True
        )
    marker.write_text(
        json.dumps(
            {
                "tree": validation_ledger.tree_of(entry, sha),
                "cmd": cmd,
                "sha": sha,
                "pid": proc.pid,
                "host": host.host_id(),
                "log": str(log),
                "started": time.time(),
            }
        )
        + "\n"
    )
    typer.echo(
        f"→ gating {sha[:12]} in the background under {cmd!r} (pid {proc.pid})\n"
        f"  log: {log}\n"
        f"  the push must not go out until this lands: `{config.BINARY_ALIAS} release await`"
    )


@app.command("await")
def await_cmd(
    rev: str = _REV,
    gate: str = _GATE,
    hive: str = _HIVE,
    timeout: int = typer.Option(1800, "--timeout", help="seconds to wait before giving up"),
    poll: float = typer.Option(5.0, "--poll", help="seconds between ledger reads"),
    if_pending: bool = typer.Option(
        False,
        "--if-pending",
        help="exit 0 immediately when NO background gate is pending for this tree (for "
        "`just release`'s pre-flight, which must not block an ordinary release with no "
        "bump gate in flight; `just push` itself no longer calls `await` at all — see "
        "`_refuse-if-bump-pending` in the justfile)",
    ),
):
    """BLOCK until the background bump gate records a verdict for REV's tree. Exit 0 iff GREEN.

    This is where "the push waits on a verdict" is actually implemented, and the reason the push
    hook is no longer where green gets established. On a green verdict the pre-push hook's own
    lookup (`bh hive hook push-main`) then hits the same ledger entry — same key, same command,
    one resolver, and fast relative to the full gate it replaces. Not free, though: a hive
    declaring `work.always_run` (bh-ehmd8) pays that set on every hit, not milliseconds — see
    docs/WORK.md's "The always-run set" for the current cost, kept there rather than restated
    here so there is one place to keep it current.

    Three answers, kept apart on purpose:

    * **green** — exit 0, push.
    * **red** — exit 1 immediately, do not wait out the timeout. Nothing has left the machine
      yet, so this is the good failure: bh-67utw's undo is still fully available.
    * **still running** — keep polling. If the background process is gone with no verdict
      recorded, that is exit 1 too and names the log; a gate that died is not a gate that passed.

    `--if-pending` is the only leniency and it is narrow: it skips ONLY when no marker exists for
    this tree at all, i.e. no release is in flight. It cannot turn a pending, red, or crashed
    gate into a pass, and it removes no protection from an ordinary push — that push still meets
    the full pre-push gate, which runs on a miss exactly as it always did."""
    entry, cmd, refusal = _resolve(hive, gate)
    if refusal:
        typer.echo(f"{refusal} — cannot wait on a verdict", err=True)
        raise typer.Exit(REFUSED)
    marker, tree = _marker_for_tree(entry, rev)
    if marker is None:
        if if_pending:
            typer.echo(f"• no bump gate pending for {rev} ({tree[:12]}) — nothing to wait on")
            return
        typer.echo(
            f"✗ no background gate was fired for {rev} ({tree[:12]}).\n"
            f"  A verdict nobody asked for cannot arrive. Fire one:\n"
            f"      {config.BINARY_ALIAS} release attest {rev} --background",
            err=True,
        )
        raise typer.Exit(REFUSED)

    deadline = time.monotonic() + timeout
    while True:
        # `cfg` is NOT threaded here (bh-ku9n9.19, item 2 — a deliberate partial): `_resolve`
        # already loads it, but only inside its own `try`, and does not return it — threading it
        # out would change a 2-caller shared helper's return shape for a `--poll`-throttled loop
        # (default 5s between reads) that pays one extra `config.load()` per poll. Worth doing
        # where a caller already holds `cfg` for free (`clean_checkout`, `check_push_main`,
        # `check`); not worth the ripple here for a config re-read this infrequent.
        hit = validation_ledger.verdict(entry, rev, cmd)
        if hit is not None and hit.get("rc") == 0:
            when = datetime.datetime.fromtimestamp(float(hit["at"])).astimezone()
            typer.echo(
                f"✓ attested green: tree {tree[:12]} passed {cmd!r} at "
                f"{when.isoformat(timespec='seconds')} — safe to push"
            )
            return
        if hit is not None:
            typer.echo(
                f"✗ the bump tree is RED (exit {hit.get('rc')!r} from {cmd!r}) — DO NOT PUSH.\n"
                f"  Nothing has left this machine, so the bump is still fully reversible:\n"
                f"      {config.BINARY_ALIAS} release recover\n"
                f"  gate output: {marker.get('log', '(no log recorded)')}",
                err=True,
            )
            raise typer.Exit(REFUSED)
        alive = _still_running(marker)
        if alive is False:
            typer.echo(
                f"✗ the background gate (pid {marker.get('pid')}) exited WITHOUT recording a "
                f"verdict for {tree[:12]} — it crashed, was killed, or never started.\n"
                f"  That is not a pass. Read {marker.get('log', '(no log recorded)')} and re-run:\n"
                f"      {config.BINARY_ALIAS} release attest {rev}",
                err=True,
            )
            raise typer.Exit(REFUSED)
        if time.monotonic() >= deadline:
            typer.echo(
                f"✗ timed out after {timeout}s waiting for a {cmd!r} verdict on {tree[:12]}.\n"
                f"  A timeout is NOT a pass — the gate may still be running "
                f"({marker.get('log', 'no log')}). Wait longer with --timeout, or re-run it.",
                err=True,
            )
            raise typer.Exit(REFUSED)
        time.sleep(poll)


@app.command("pending")
def pending(rev: str = _REV, gate: str = _GATE, hive: str = _HIVE):
    """Exit 0 iff a bump-gate marker is live for REV's tree, 1 otherwise — no output either way.

    `just push`'s pre-flight (bh-8c2yo) consults this to refuse landing main while `just bump`'s
    background gate marker still names HEAD, rather than waiting the gate out (`await`'s job)
    and then pushing main without ever pushing the tag. Prints nothing on purpose: the caller
    owns the wording of its own refusal.

    Reads the marker through `_marker_for_tree` — the SAME test `await` waits on, not a second
    parser. **FAILS OPEN**: no hive, an unresolvable gate, an absent or unreadable marker, or a
    marker for a different tree are ALL "not pending" (exit 1) — identically to how `await`
    treats every one of those as nothing to wait on. A pending gate's verdict (green, red, or
    still running) does not matter here; only whether one is in flight for THIS tree does."""
    entry, _cmd, refusal = _resolve(hive, gate)
    if refusal:
        raise typer.Exit(REFUSED)
    marker, _tree = _marker_for_tree(entry, rev)
    raise typer.Exit(0 if marker is not None else REFUSED)


def _ls_remote(main: Path, remote: str, pattern: str) -> tuple[int, list[str]]:
    """`(rc, lines)` from a real `git ls-remote` against the REMOTE — never a local
    remote-tracking ref, which a failed push may never have updated and which is precisely how
    the 0.11.5 incident got reported as a success (`scripts/push-main.sh`, bh-53o8f).

    Unpiped, and the rc is returned rather than inferred from empty output: "" is a legitimate
    answer meaning *the ref is not there*, and folding "I could not look" into it is bh-dt2d9."""
    res = subprocess.run(
        ["git", "-C", str(main), "ls-remote", remote, pattern],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode, [ln for ln in (res.stdout or "").splitlines() if ln.strip()]


def recovery_decision(main: Path, tag: str, bump_sha: str, remote: str = "origin", branch="main"):
    """`(exit_code, detail)` for "a bump failed to push — which of bh-67utw's two cases is this?"

    **THE BRANCH TURNS ON ONE MEASURED FACT: did the tag reach the remote.** Measured with
    `ls-remote` against the actual remote, never assumed, never read from a local tracking ref.
    That is the entire load-bearing decision, because the tag is the point of no return: before
    it the bump is a local edit, after it `.github/workflows/release.yml` may already have fired
    and anything downstream may already have consumed the version.

    Four answers, and the fourth is why this returns a code rather than a bool:

    * **3, cannot measure.** `ls-remote` failed, or the remote's main sha is not an object we
      hold. NEITHER case is chosen. A recovery that rewrites history on a guess is the one
      outcome worse than doing nothing.
    * **1, tag IS on the remote — bh-67utw case B.** Leave it alone. A published tag is never
      deleted or moved; a failed release rolls FORWARD with a new version.
    * **2, tag is NOT on the remote but the bump sha IS on remote main.** The half-done release
      bh-zfvbp names: main published, nothing released, undo already closed. The only correct
      move is to finish it by pushing the tag. An atomic push (`--atomic`, both refs or neither)
      is what stops this being reachable in the first place; it is measured for anyway, because
      a state that "cannot happen" is exactly the one nobody checks for.
    * **0, tag absent AND the bump sha is nowhere on the remote — bh-67utw case A.** Nothing
      left; the undo is safe. This function does NOT perform it: the rewrite (backup ref,
      `git rebase --rebase-merges --onto`, and the after-diff proving only version files moved)
      is bh-67utw's, and duplicating it here would give the repo two recipes to disagree."""
    rc, lines = _ls_remote(main, remote, f"refs/tags/{tag}")
    if rc != 0:
        return UNMEASURABLE, (
            f"✗ COULD NOT MEASURE whether {tag} reached {remote} (`git ls-remote` exited {rc}).\n"
            f"  This is NOT 'the tag never left'. The whole recovery branch turns on this one\n"
            f"  fact and it must never be assumed — check by hand before doing anything:\n"
            f"      git ls-remote --tags {remote} {tag}"
        )
    if lines:
        return REFUSED, (
            f"✗ {tag} IS ON {remote} — measured, {lines[0].split()[0][:12]}.\n"
            f"  bh-67utw case B: the tag is published. .github/workflows/release.yml fires on\n"
            f"  `push: tags: v*`, so a release workflow may already have run and anything\n"
            f"  downstream may already have consumed this version. DO NOT delete or move it.\n"
            f"  A failed release rolls FORWARD: fix, and bump again to the next version."
        )

    head_rc, head_lines = _ls_remote(main, remote, f"refs/heads/{branch}")
    if head_rc != 0:
        return UNMEASURABLE, (
            f"✗ {tag} is not on {remote}, but COULD NOT MEASURE {branch} there "
            f"(`git ls-remote` exited {head_rc}).\n"
            f"  Half the fact is not the fact: if the bump commit already landed on {branch} the\n"
            f"  undo is closed regardless of the tag. Check by hand:\n"
            f"      git ls-remote --heads {remote} {branch}"
        )
    remote_head = head_lines[0].split()[0] if head_lines else ""
    landed = False
    if remote_head:
        anc = subprocess.run(
            ["git", "-C", str(main), "merge-base", "--is-ancestor", bump_sha, remote_head],
            capture_output=True,
            text=True,
            check=False,
        )
        if anc.returncode not in (0, 1):
            return UNMEASURABLE, (
                f"✗ {tag} is not on {remote}, but COULD NOT MEASURE whether {bump_sha[:12]} is\n"
                f"  already on {remote}/{branch} ({remote_head[:12]} is not an object this clone\n"
                f"  holds — `git fetch {remote}` first). Undo nothing until you know."
            )
        landed = anc.returncode == 0

    if landed:
        return HALF_DONE, (
            f"✗ HALF-DONE RELEASE: {remote}/{branch} already carries the bump {bump_sha[:12]},\n"
            f"  but {tag} is NOT on {remote} — measured both with ls-remote.\n"
            f"  bh-zfvbp's worst state: main is published so bh-67utw's undo is already closed,\n"
            f"  yet nothing is released — the workflow fires on the TAG, so there is no wheel,\n"
            f"  no tap, no latest. The only correct move is to FINISH it:\n"
            f"      git push {remote} {tag}\n"
            f"  This state should be unreachable — the release pushes main and its tag in one\n"
            f"  `git push --atomic` (both or neither). If you are reading this, something pushed\n"
            f"  {branch} on its own."
        )
    return 0, (
        f"✓ SAFE TO UNDO: {tag} is not on {remote} and {bump_sha[:12]} is on no {remote} ref —\n"
        f"  measured with ls-remote against the actual remote, not a local tracking ref.\n"
        f"  bh-67utw case A: nothing left this machine, so the bump can be rewritten away\n"
        f"  completely. THIS VERB DOES NOT DO IT — the rewrite (backup ref first, then\n"
        f"  `git rebase --rebase-merges --onto <pre-bump> <bump> {branch}` for a buried bump or\n"
        f"  commitizen's tag-delete/reset recipe for a tip one, then diff against the backup to\n"
        f"  prove only the version files moved) is bh-67utw's, and one recipe is better than two."
    )


@app.command("recover")
def recover(
    rev: str = typer.Argument("HEAD", metavar="REV", help="the bump commit"),
    tag: str = typer.Option("", "--tag", help="the bump's tag (default: the tag at REV)"),
    remote: str = typer.Option("origin", "--remote"),
    branch: str = typer.Option("main", "--branch"),
    hive: str = _HIVE,
):
    """A bump failed to push — MEASURE the remote and say which of bh-67utw's two cases this is.

    Read-only: it looks, it decides, it names the next command. It never rewrites history and
    never touches a ref, because the two cases need opposite treatment and picking between them
    on an assumption is what turned the 0.11.5 recovery into an improvisation.

    Exit 0 = the tag never left, the undo is safe (case A). 1 = the tag is published, roll
    forward instead (case B). 2 = main landed without its tag, finish the release. 3 = the
    remote could not be read, so NOTHING is concluded."""
    try:
        main = registry.hive_dir_for(config.load(), hive)
    except Exception as exc:  # noqa: BLE001 — cannot locate the clone ⇒ cannot MEASURE anything
        typer.echo(
            f"✗ COULD NOT MEASURE: no clone to read the remote from "
            f"({type(exc).__name__}: {exc}). Nothing is concluded about {rev}.",
            err=True,
        )
        raise typer.Exit(UNMEASURABLE) from exc
    # `--verify …^{commit}` for the same reason as `attest`: a bare rev-parse hands back any
    # 40-hex string, and a recovery that "resolved" a typo would measure the remote for a commit
    # that does not exist and conclude, correctly and uselessly, that it never left.
    res = subprocess.run(
        ["git", "-C", str(main), "rev-parse", "--verify", "-q", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    bump_sha = (res.stdout or "").strip()
    if res.returncode != 0 or not bump_sha:
        typer.echo(f"✗ cannot resolve {rev!r} in {main}", err=True)
        raise typer.Exit(UNMEASURABLE)
    if not tag:
        at = subprocess.run(
            ["git", "-C", str(main), "tag", "--points-at", bump_sha],
            capture_output=True,
            text=True,
            check=False,
        )
        tags = [t.strip() for t in (at.stdout or "").splitlines() if t.strip()]
        if len(tags) != 1:
            typer.echo(
                f"✗ {'no' if not tags else 'more than one'} tag at {bump_sha[:12]}"
                f"{' (' + ', '.join(tags) + ')' if tags else ''} — name the bump's tag with"
                f" --tag. Guessing which tag a recovery is about is not acceptable here.",
                err=True,
            )
            raise typer.Exit(UNMEASURABLE)
        tag = tags[0]

    code, detail = recovery_decision(main, tag, bump_sha, remote=remote, branch=branch)
    typer.echo(detail, err=code != 0)
    raise typer.Exit(code)


def _project_pin(main: Path) -> tuple[str, str]:
    """`(name, version)` from the clone's `[project]` table, or `("", "")` when unreadable.

    The same single source of truth `scripts/release-pin.sh` reads, and for its reason: a second
    place to spell a version is a second place for it to be wrong. Unreadable is not an error
    here — it degrades the two checks that need it to "could not check", which is this verb's
    whole failure direction."""
    import tomllib

    try:
        project = tomllib.loads((main / "pyproject.toml").read_text()).get("project", {})
        return str(project.get("name") or ""), str(project.get("version") or "")
    except Exception:  # noqa: BLE001 — no project metadata ⇒ "could not check", never a refusal
        return "", ""


def _published_artifact(project: str, version: str, timeout: float = 5.0):
    """Is `project`'s `version` ALREADY published on PyPI? `True` / `False` / **`None` = could
    not check** — plus the sentence to print, either way.

    The only check in `preview` that needs the network, so it is the only one that can be wrong
    because a wifi blinked. **`None` swallows everything that is not a definitive 404**: a
    timeout, DNS, a proxy, a 5xx, an SSL error. A preview that turned a network blip into "the
    path is blocked" would be a gate wearing a report's name, and an operator learns to ignore
    exactly that. 404 is the one negative answer PyPI actually asserts, so it is the only one
    read as "not published".

    Imported lazily like `prepush` above: `cli.py` imports this module at startup and `urllib`
    is not worth taxing every `bh` invocation for one read-only verb."""
    import urllib.error
    import urllib.request

    url = f"https://pypi.org/pypi/{project}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout):  # noqa: S310 — literal https URL
            return True, (
                f"✗ {project} {version} IS ALREADY ON PyPI — {url}\n"
                f"       that version is spent: PyPI never re-accepts a filename, so a release "
                f"of it cannot succeed.\n"
                f"       Roll FORWARD to the next version rather than trying to replace it."
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, f"✓ {project} {version} is not on PyPI — nothing to collide with"
        return None, (
            f"• COULD NOT CHECK PyPI for {project} {version} (HTTP {exc.code}) — not an answer, "
            f"and not a refusal"
        )
    except Exception as exc:  # noqa: BLE001 — every network failure is "could not check"
        return None, (
            f"• COULD NOT CHECK PyPI for {project} {version} ({type(exc).__name__}: {exc}) — "
            f"not an answer, and not a refusal"
        )


@app.command("preview")
def preview(
    rev: str = _REV,
    gate: str = _GATE,
    hive: str = _HIVE,
    tag: str = typer.Option("", "--tag", help="the tag a release would push (default: v<version>)"),
    remote: str = typer.Option("origin", "--remote"),
):
    """Is the release path clear? READ-ONLY — three checks REPORTED, and NOTHING is refused.

    The counterpart to `preflight`, and deliberately not a second copy of it. `preflight` EXITS 1
    on an unattested tree because it exists to GATE the bump; a preview that did the same would
    hide the other two answers behind the first bad one, which is the opposite of what you want
    before a one-way door. So every line here is measured and printed, and **the exit code is 0
    even when the path is not clear** — read the lines, not the status.

    * **green** — free, the same lookup `preflight` and the pre-push hook use. A miss reports
      "not attested" and names `just attest`, instead of refusing.
    * **the tag is not already on the remote** — `git ls-remote` against the ACTUAL remote,
      through the same `_ls_remote` `recovery_decision` measures with, so both keep the same
      three-way answer: on the remote / not there / COULD NOT LOOK. The third is never folded
      into the second (bh-dt2d9) — "the tag never left" is the fact bh-67utw's whole undo rule
      turns on, and assuming it is the one thing that must never happen.
    * **no conflicting published artifact** — the only check that needs the network, and it
      degrades to "could not check" on anything but a definitive 404.

    Establishes no verdict, pushes no ref, writes nothing. Exit 3 only when there is no clone to
    read at all, because then not one of the three was measured."""
    try:
        main = registry.hive_dir_for(config.load(), hive)
    except Exception as exc:  # noqa: BLE001 — no clone ⇒ nothing was measured at all
        typer.echo(
            f"✗ COULD NOT MEASURE: no clone to preview from ({type(exc).__name__}: {exc}).",
            err=True,
        )
        raise typer.Exit(UNMEASURABLE) from exc

    name, version = _project_pin(main)
    tag = tag or (f"v{version}" if version else "")

    typer.echo(
        f"release preview — {rev} → {remote}{f', tag {tag}' if tag else ''}\n"
        f"  READ-ONLY: nothing below establishes a verdict or pushes a ref, and nothing refuses."
    )

    from . import prepush

    ok, detail = prepush.check_push_main(
        rev, hive_id=hive, gate_cmd=gate, on_miss="REPORTED HERE, not enforced"
    )
    typer.echo(f"  green    {detail}")
    if not ok:
        typer.echo("           → not attested — run `just attest` (`just bump` would refuse)")

    if not tag:
        typer.echo("  tag      • no tag to check — no [project] version readable from pyproject")
    else:
        rc, lines = _ls_remote(main, remote, f"refs/tags/{tag}")
        if rc != 0:
            typer.echo(
                f"  tag      • COULD NOT MEASURE whether {tag} is on {remote} (`git ls-remote` "
                f"exited {rc})\n"
                f"           this is NOT 'the tag never left' — check by hand: "
                f"git ls-remote --tags {remote} {tag}"
            )
        elif lines:
            typer.echo(
                f"  tag      ✗ {tag} IS ALREADY ON {remote} — measured, "
                f"{lines[0].split()[0][:12]}\n"
                f"           a published tag is never moved or deleted; roll FORWARD to the next "
                f"version ({config.BINARY_ALIAS} release recover)"
            )
        else:
            typer.echo(
                f"  tag      ✓ {tag} is not on {remote} — measured, the release is still "
                f"fully reversible"
            )

    if not (name and version):
        typer.echo("  artifact • could not check — no [project] name/version readable")
    else:
        _published, why = _published_artifact(name, version)
        typer.echo(f"  artifact {why}")
