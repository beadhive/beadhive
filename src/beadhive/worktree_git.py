"""Git history, rewrite, and landing operations for managed worktrees.

The public compatibility surface stays in :mod:`beadhive.worktree`; implementations live
here and resolve patchable collaborators through that facade.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

import typer

from . import config, ghpr, registry
from .run import retry_on_index_lock

UPSTREAM_REMOTE = "upstream"
BEAD_KINDS = ("epic", "issue")
_BEAD_PREFIX = "wt/bead/"
_ROW_RS = "\x1e"
_ROW_FS = "\x1f"
_ROW_FMT = _ROW_RS + _ROW_FS.join(["%H", "%h", "%P", "%an", "%ae", "%ad", "%G?", "%GS", "%s"])


def _facade():
    from . import worktree

    return worktree


def _call_facade(name, *args, **kwargs):
    return getattr(_facade(), name)(*args, **kwargs)


def _run_git(*args, **kwargs):
    return _call_facade("_run_git", *args, **kwargs)


def history(*args, **kwargs):
    return _call_facade("history", *args, **kwargs)


def signature_status(*args, **kwargs):
    return _call_facade("signature_status", *args, **kwargs)


def commit_messages(*args, **kwargs):
    return _call_facade("commit_messages", *args, **kwargs)


def commit_shas(*args, **kwargs):
    return _call_facade("commit_shas", *args, **kwargs)


def push_branch(*args, **kwargs):
    return _call_facade("push_branch", *args, **kwargs)


def is_clean(*args, **kwargs):
    return _call_facade("is_clean", *args, **kwargs)


def dirty_paths(*args, **kwargs):
    return _call_facade("dirty_paths", *args, **kwargs)


def current_branch(*args, **kwargs):
    return _call_facade("current_branch", *args, **kwargs)


def head_sha(*args, **kwargs):
    return _call_facade("head_sha", *args, **kwargs)


def head_full_sha(*args, **kwargs):
    return _call_facade("head_full_sha", *args, **kwargs)


def base_of(*args, **kwargs):
    return _call_facade("base_of", *args, **kwargs)


def commit_rows(*args, **kwargs):
    return _call_facade("commit_rows", *args, **kwargs)


def backup_branch(*args, **kwargs):
    return _call_facade("backup_branch", *args, **kwargs)


def _rebase_env(*args, **kwargs):
    return _call_facade("_rebase_env", *args, **kwargs)


def rebase_squash(*args, **kwargs):
    return _call_facade("rebase_squash", *args, **kwargs)


def rebase_autosquash(*args, **kwargs):
    return _call_facade("rebase_autosquash", *args, **kwargs)


def rebase_onto(*args, **kwargs):
    return _call_facade("rebase_onto", *args, **kwargs)


def rebase_abort(*args, **kwargs):
    return _call_facade("rebase_abort", *args, **kwargs)


def reset_hard(*args, **kwargs):
    return _call_facade("reset_hard", *args, **kwargs)


def safe_to_rewrite(*args, **kwargs):
    return _call_facade("safe_to_rewrite", *args, **kwargs)


def same_tree(*args, **kwargs):
    return _call_facade("same_tree", *args, **kwargs)


def is_merged(*args, **kwargs):
    return _call_facade("is_merged", *args, **kwargs)


def on_first_parent_chain(*args, **kwargs):
    return _call_facade("on_first_parent_chain", *args, **kwargs)


def landed_via_merge(*args, **kwargs):
    return _call_facade("landed_via_merge", *args, **kwargs)


def _all_cherry_landed(*args, **kwargs):
    return _call_facade("_all_cherry_landed", *args, **kwargs)


def is_landed(*args, **kwargs):
    return _call_facade("is_landed", *args, **kwargs)


def bead_and_parent(*args, **kwargs):
    return _call_facade("bead_and_parent", *args, **kwargs)


def diff_range(*args, **kwargs):
    return _call_facade("diff_range", *args, **kwargs)


def log_range(*args, **kwargs):
    return _call_facade("log_range", *args, **kwargs)


def _bead_id_from_branch(*args, **kwargs):
    return _call_facade("_bead_id_from_branch", *args, **kwargs)


def _branch_exists(*args, **kwargs):
    return _call_facade("_branch_exists", *args, **kwargs)


def integration_base(*args, **kwargs):
    return _call_facade("integration_base", *args, **kwargs)


def run(*args, **kwargs):
    return _call_facade("run", *args, **kwargs)


def impl__run_git(args, **kw):
    """Run git with ambient GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE scrubbed, so our explicit
    `-C <repo>` always wins (those env vars override -C, and a git hook exports them — without
    this, `ws wt …` invoked inside a hook would operate on the wrong repo).

    Every worktree mutation (worktree add/remove, branch -d, reset --hard, push, rebase) funnels
    through here, so this is also where the ``.git/index.lock`` retry is generalized (bh-i6o7): a
    detached ``git maintenance run --auto`` spawned by an earlier commit can transiently hold the
    index, and a mutation racing it must retry, not fail. ``run`` is passed to the retry so the
    per-module subprocess seam tests fake stays intact."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return retry_on_index_lock(run, args, env=env, **kw)


def impl_history(entry, branch, base):
    """(count, [subjects]) for commits on `branch` not reachable from `base`.
    (-1, []) when the range can't be computed (e.g. base missing)."""
    main = registry.hive_dir(entry)
    rng = f"{base}..{branch}"
    cres = _run_git(["git", "-C", str(main), "rev-list", "--count", rng], check=False, capture=True)
    if cres.returncode != 0:
        return -1, []
    count = int((cres.stdout or "0").strip() or "0")
    lres = _run_git(["git", "-C", str(main), "log", "--format=%s", rng], check=False, capture=True)
    subjects = [s for s in (lres.stdout or "").splitlines() if s.strip()]
    return count, subjects


def impl_signature_status(entry, branch, base) -> list[tuple[str, str, str]]:
    """`(short_sha, status, subject)` for EVERY commit on `branch` not reachable from `base`,
    newest first — the merge gate's input (bh-ijd4).

    `status` is git's own `%G?` verdict, one character per commit, in ONE call rather than a
    `git verify-commit` per commit: `G` good+trusted, `U` good but the key is not in
    `allowed_signers`, `B` bad, `X`/`Y`/`R` expired/expired-key/revoked, `E` uncheckable, `N`
    none. Only `G` means a signature bh can actually stand behind — and note a *correctly
    signed* commit still reports `N` when `gpg.ssh.allowedSignersFile` is unset, or `U` when it
    points at a missing file (measured, git 2.54), which is why the gate's config description
    insists on that file being real. `[]` when the range can't be computed, matching
    :func:`history`'s `-1` sentinel — the caller must treat that as a refusal, not as "clean"."""
    main = registry.hive_dir(entry)
    res = _run_git(
        ["git", "-C", str(main), "log", "--format=%h%x00%G?%x00%s", f"{base}..{branch}"],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return []
    rows = []
    for line in (res.stdout or "").splitlines():
        parts = line.split("\0")
        if len(parts) == 3 and parts[0]:
            rows.append((parts[0], parts[1] or "N", parts[2]))
    return rows


def impl_commit_messages(entry, branch, base) -> list[str]:
    """Full commit messages (`%B` — subject + body) for commits on `branch` not reachable from
    `base`, newest first; [] when the range can't be computed. The subject-only `history()` view
    drops the body, so the submit-time release-hint reconcile (which reads `BREAKING CHANGE:`
    footers) reads messages here. NUL-delimited so multi-line bodies split cleanly."""
    main = registry.hive_dir(entry)
    rng = f"{base}..{branch}"
    res = _run_git(
        ["git", "-C", str(main), "log", "--format=%B%x00", rng], check=False, capture=True
    )
    if res.returncode != 0:
        return []
    return [m.strip() for m in (res.stdout or "").split("\x00") if m.strip()]


def impl_commit_shas(entry, branch, base) -> list[str]:
    """Full 40-char SHAs for every commit on `branch` not reachable from `base`, OLDEST FIRST —
    the order the `git.commits` bead↔commit linkage contract requires
    (docs/design/bead-commit-linkage-contract.md: "append-only, oldest-observed-first"). `[]`
    when the range can't be computed (e.g. base missing), matching `history()`'s failure mode."""
    main = registry.hive_dir(entry)
    rng = f"{base}..{branch}"
    res = _run_git(
        ["git", "-C", str(main), "rev-list", "--reverse", rng], check=False, capture=True
    )
    if res.returncode != 0:
        return []
    return [s for s in (res.stdout or "").splitlines() if s.strip()]


def impl_push_branch(entry, branch, remote="origin") -> int:
    """Push `branch` to `remote` (same name both ends). Returns git's exit code.

    Refuses outright when `remote` is `upstream` — external hives fork-and-PR (`origin` is our
    fork, the only remote we ever own write access to); `upstream` stays pull-only until a
    dedicated PR verb consumes it deliberately."""
    if remote == UPSTREAM_REMOTE:
        typer.echo(
            "✗ refusing to push to 'upstream' — external hives are pull-only; "
            "push to 'origin' (the fork) instead",
            err=True,
        )
        return 1
    main = registry.hive_dir(entry)
    return _run_git(
        ["git", "-C", str(main), "push", remote, f"{branch}:{branch}"], check=False
    ).returncode


def impl_is_clean(target: Path) -> bool:
    """True iff the worktree at `target` has no staged/unstaged/untracked changes."""
    res = _run_git(["git", "-C", str(target), "status", "--porcelain"], check=False, capture=True)
    return res.returncode == 0 and not (res.stdout or "").strip()


def impl_dirty_paths(target: Path) -> list[str]:
    """The ``git status --porcelain`` lines for `target` — what :func:`is_clean` said no to.

    Exists so a refusal can NAME the offending files instead of guessing at them (bh-bj219).
    The merge gate used to advise adding ``.beads/`` to .gitignore; on a repo whose churn was
    ``.beads.gate.lock`` at the REPO ROOT that advice was both wrong (no ``.beads/`` rule
    covers it) and actively harmful (it would also have ignored the tracked
    ``.beads/config.yaml``)."""
    res = _run_git(["git", "-C", str(target), "status", "--porcelain"], check=False, capture=True)
    if res.returncode != 0:
        return []
    return [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]


def impl_current_branch(target: Path) -> str:
    """The checked-out branch name in `target` ('' if detached / on error)."""
    res = _run_git(
        ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"], check=False, capture=True
    )
    name = (res.stdout or "").strip() if res.returncode == 0 else ""
    return "" if name == "HEAD" else name


def impl_head_sha(target: Path) -> str:
    """Short HEAD sha in `target` ('' on error)."""
    res = _run_git(
        ["git", "-C", str(target), "rev-parse", "--short", "HEAD"], check=False, capture=True
    )
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def impl_head_full_sha(target: Path) -> str:
    """Full HEAD sha in `target` ('' on error) — the validation-ledger key format (bh-i0p1.4).
    Distinct from `head_sha` above (short, display-only): the ledger's `(sha, cmd_hash)` key is
    always the FULL sha (see `clean_checkout`'s `_branch_sha`), so a caller recording against the
    ledger must use this, not the short form, or the key silently never matches."""
    res = _run_git(["git", "-C", str(target), "rev-parse", "HEAD"], check=False, capture=True)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def impl_base_of(entry, branch, integration) -> str:
    """The fork point `git merge-base <integration> <branch>` — base..branch is the bead's
    local history. '' if it can't be computed (e.g. integration branch missing locally)."""
    main = registry.hive_dir(entry)
    res = _run_git(
        ["git", "-C", str(main), "merge-base", integration, branch], check=False, capture=True
    )
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def impl_commit_rows(entry, base, branch) -> list[dict]:
    """Oldest→newest commits in base..branch. Each row: {sha, short, parents, author, email,
    date (author date, iso-strict), subject, files, sig (G/U/B/N), signer}. [] on error."""
    main = registry.hive_dir(entry)
    res = _run_git(
        [
            "git",
            "-C",
            str(main),
            "log",
            f"{base}..{branch}",
            "--reverse",
            "--date=iso-strict",
            "--name-only",
            f"--format={_ROW_FMT}",
        ],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return []
    rows = []
    for chunk in (res.stdout or "").split(_ROW_RS):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.split("\n")
        f = lines[0].split(_ROW_FS)
        if len(f) < 9:
            continue
        sha, short, parents, an, ae, ad, sig, signer, subj = f[:9]
        rows.append(
            {
                "sha": sha,
                "short": short,
                "parents": parents.split(),
                "author": an,
                "email": ae,
                "date": ad,
                "subject": subj,
                "files": [ln for ln in lines[1:] if ln.strip()],
                "sig": sig,
                "signer": signer,
            }
        )
    return rows


def impl_backup_branch(entry, branch, ts: str, label: str = "refine") -> str:
    """Create the safety branch `<branch>.<label>-<ts>` at `branch`'s tip; return its name.
    Caller supplies `ts` (ws runtime may stamp time freely). `label` distinguishes the operation
    (refine vs. premerge rebase) so concurrent safety refs never collide."""
    main = registry.hive_dir(entry)
    name = f"{branch}.{label}-{ts}"
    res = _run_git(["git", "-C", str(main), "branch", name, branch], check=False, capture=True)
    if res.returncode != 0:
        typer.echo(f"✗ could not create backup branch {name}: {res.stderr or res.stdout}", err=True)
        raise typer.Exit(1)
    return name


def impl__rebase_env(**extra) -> dict:
    """git env with the dir-pointing GIT_* scrubbed (so `-C` wins) plus our editor overrides —
    `_run_git` can't be reused here because it scrubs ALL GIT_* incl. the ones we must set."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra)
    return env


def impl_rebase_squash(target_wt, base, todo_lines) -> tuple[int, str]:
    """Run `git rebase -i <base>` in the WORKTREE (the branch is checked out there) with a
    non-interactive sequence editor that overwrites git's todo with `todo_lines`. GIT_EDITOR is
    pinned to a no-op too (fixup/exec need no editor) so nothing can block. (rc, combined out)."""
    with tempfile.NamedTemporaryFile("w", suffix=".gittodo", delete=False) as f:
        f.write("\n".join(todo_lines) + "\n")
        todo_path = f.name
    env = _rebase_env(GIT_SEQUENCE_EDITOR=f"cp {shlex.quote(todo_path)}", GIT_EDITOR="true")
    try:
        res = run(
            ["git", "-C", str(target_wt), "rebase", "-i", base],
            env=env,
            check=False,
            capture=True,
        )
    finally:
        os.unlink(todo_path)
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def impl_rebase_autosquash(target_wt, base) -> tuple[int, str]:
    """`git rebase -i --autosquash <base>` with no-op editors: git auto-builds the todo placing
    each `fixup!`/`squash!` after its target, and `true` accepts it unedited. (rc, combined)."""
    env = _rebase_env(GIT_SEQUENCE_EDITOR="true", GIT_EDITOR="true")
    res = run(
        ["git", "-C", str(target_wt), "rebase", "-i", "--autosquash", base],
        env=env,
        check=False,
        capture=True,
    )
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def impl_rebase_onto(target_wt, base) -> tuple[int, str]:
    """Plain `git rebase <base>` in the worktree (the branch is checked out there) — replay the
    branch's commits onto a newer base. Used by `try_merge_rebase`'s conflict recovery; a clean
    replay needs no editor, and on conflict git stops non-zero so the caller can abort. (rc, out)"""
    # rerere off for the same reason as merge_no_ff: don't let a cached resolution mask a real
    # replay conflict. Cherry-pick de-duplication (the actual replay win) is independent of rerere.
    res = _run_git(
        ["git", "-C", str(target_wt), "-c", "rerere.enabled=false", "rebase", str(base)],
        check=False,
        capture=True,
    )
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def impl_rebase_abort(target_wt) -> None:
    """Best-effort `git rebase --abort` (no-op if no rebase is in progress)."""
    _run_git(["git", "-C", str(target_wt), "rebase", "--abort"], check=False, capture=True)


def impl_reset_hard(target_wt, ref) -> int:
    """`git reset --hard <ref>` in the worktree. Returns git's exit code."""
    return _run_git(
        ["git", "-C", str(target_wt), "reset", "--hard", ref], check=False, capture=True
    ).returncode


def impl_safe_to_rewrite(clone, branch) -> bool:
    """True iff `branch` may be `reset --hard` without rewriting shared/published history: any
    branch with no configured upstream (not pushed). A private container integration branch
    (`wt/bead/epic/<id>`, any tier) is local/unpushed → safe, so an intermediate tier land rolls
    back losslessly. A pushed integration branch (e.g. `main` tracking `origin/main`) is NOT safe —
    a red landing there must be fixed forward, not rewritten."""
    return (
        _run_git(
            ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"],
            check=False,
            capture=True,
        ).returncode
        != 0
    )


def impl_same_tree(entry, a, b) -> bool:
    """True iff refs `a` and `b` have byte-identical trees — the refine safety gate."""
    main = registry.hive_dir(entry)
    return _run_git(["git", "-C", str(main), "diff", "--quiet", a, b], check=False).returncode == 0


def impl_is_merged(entry, branch: str, base: str) -> bool:
    """True iff every commit on `branch` is already reachable from `base`.

    Uses ``git merge-base --is-ancestor branch base`` which exits 0 when ``branch`` is an
    ancestor of ``base`` (i.e. all its commits are included in ``base``).  This is the
    merge-ancestry primitive that the worktree SAFE classifier depends on — the only call that
    performs a real git ancestry check rather than inferring merged-ness from bead status.
    """
    main = registry.hive_dir(entry)
    return (
        _run_git(
            ["git", "-C", str(main), "merge-base", "--is-ancestor", branch, base],
            check=False,
        ).returncode
        == 0
    )


def impl_on_first_parent_chain(entry, branch: str, base: str) -> bool:
    """True iff ``branch``'s tip sits on ``base``'s own first-parent line.

    This is what separates a branch that was NEVER IMPLEMENTED from one that ALREADY LANDED, and
    :func:`is_merged` cannot tell them apart alone: a freshly-claimed branch points AT the base
    tip, and a commit is trivially its own ancestor, so ``merge-base --is-ancestor`` answers
    "merged" for work that does not exist (bh-lvqs).

    A ``--no-ff`` land — the only kind bh performs — puts the bead's commits on a side branch
    reachable from the base only through a merge commit's SECOND parent, so a landed tip is NOT on
    the first-parent chain; a fork point, by construction, is. Were a branch ever fast-forwarded
    in, its commits would sit on the chain and this returns True, so the caller treats it as
    not-landed and takes the ordinary refusal — a false negative that bounces rather than silently
    closing a bead, which is the safe direction to be wrong in.
    """
    main = registry.hive_dir(entry)
    # EVERY FAILURE PATH RETURNS True, and the direction is deliberate. This function is consumed
    # negated (`landed_via_merge` = is_merged AND NOT this), so True means "treat as NOT landed"
    # and the caller takes the ordinary refusal. A git call we could not read must never be the
    # reason a bead gets closed as already-merged: refusing a merge is recoverable in one command,
    # silently closing unlanded work is not.
    tip = _run_git(["git", "-C", str(main), "rev-parse", branch], check=False, capture=True)
    if tip.returncode != 0:
        return True
    sha = (tip.stdout or "").strip()
    if not sha:
        return True
    chain = _run_git(
        ["git", "-C", str(main), "rev-list", "--first-parent", base], check=False, capture=True
    )
    if chain.returncode != 0:
        return True
    return sha in (chain.stdout or "").split()


def impl_landed_via_merge(entry, branch: str, base: str) -> bool:
    """True when ``branch``'s commits reached ``base`` BY BEING MERGED into it.

    THE DISTINCTION THIS DRAWS IS THE WHOLE OF bh-lvqs. "Zero commits over base" has two causes the
    merge verbs used to collapse into one message: work never implemented, and work that ALREADY
    LANDED. They demand opposite responses — bounce for rework versus reconcile the bookkeeping —
    and merge was giving the first answer to the second case, telling an operator to "bounce back
    for self-refine" about code already on the integration branch. Acting on that means re-doing
    landed work.

    Both halves are required. :func:`is_merged` alone says True for a never-implemented branch,
    because a freshly-claimed branch points AT the base tip and a commit is its own ancestor —
    closing those as landed would silently mark unwritten work done, which is worse than the bug
    being fixed. :func:`on_first_parent_chain` supplies the other half.

    Lives here, beside the two ancestry primitives it composes, so both the bead path (``work``)
    and the batch path (``work_group``) can reach it — ``work_group`` cannot import ``work``.
    """
    return is_merged(entry, branch, base) and not on_first_parent_chain(entry, branch, base)


def impl__all_cherry_landed(entry, branch: str, parent: str) -> bool:
    """True iff every unique commit on ``branch`` (not in ``parent``) is already present
    in ``parent`` by patch-id equivalence.

    Uses ``git cherry <parent> <branch>``: commits marked ``-`` are already in parent
    (patch-id equivalent from a rebase or cherry-pick); commits marked ``+`` are not.
    Returns ``True`` when all unique commits are ``-`` or there are no unique commits.
    Returns ``False`` on git failure (conservative — prefer UNMERGED over a false positive).

    Limitation: pure squash-merges (N commits collapsed to one) cannot be detected here
    because the squashed commit will not patch-id-match the individual originals.  Use the
    merge-event check (``is_landed``) for squash-landed branches.
    """
    main = registry.hive_dir(entry)
    res = _run_git(
        ["git", "-C", str(main), "cherry", parent, branch],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return False
    lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
    # Empty output → branch adds no unique commits (already covered).
    # All "-" lines → every commit already in parent by patch-id.
    return all(ln.startswith("- ") for ln in lines) if lines else True


def impl_is_landed(entry, branch: str, parent: str, close_reason: str = "") -> bool:
    """True iff a closed-but-non-ancestor branch has its content effectively in ``parent``.

    Second-stage check for the closed+non-ancestor set (today's UNMERGED rows).  Runs
    ONLY after the fast-path ``is_merged`` ancestor check has returned ``False``, so the
    git work here is bounded to the cases that actually need it.

    Three checks in priority order:

    1. **Merge-event** (fast, authoritative, squash-proof): if ``close_reason`` is
       ``"merged"`` or ``"molecule landed"``, the AGF lifecycle confirms the work landed
       and the branch is safe to reclaim — regardless of SHA identity.

    2. **Patch-id / cherry equivalence** (fallback for branches without a merge event):
       ``git cherry <parent> <branch>`` marks commits already in parent with ``-``.  If
       every unique commit is so marked, the branch was rebase/cherry-pick landed.  Not
       reliable for pure squash-merges (which have no patch-id match).

    3. **GitHub PR-merged** (squash-proof, network, last — bh-v0wu): a PR-governed land
       (``work.landing: pr``, or any hand-opened PR) squash-merged ON GitHub leaves neither
       a bh close_reason nor patch-id-matching commits — the seat would read UNMERGED
       forever.  Ask gh whether a MERGED PR has this branch as head (``gh pr list --state
       merged --head``).  Best-effort and fail-closed: GitHub-backed hives only, ``False``
       when gh is absent or the probe errors.

    Returns ``False`` on git failure (conservative: prefer UNMERGED over a false positive).
    """
    if close_reason in ("merged", "molecule landed"):
        return True
    if _all_cherry_landed(entry, branch, parent):
        return True
    return ghpr.merged_pr_for(entry, branch) is not None


def impl_bead_and_parent(
    entry, path: str, integration: str, branch: str = ""
) -> tuple[str | None, str]:
    """Map a managed worktree path to ``(bead_id | None, parent_branch)``.

    The bead id is parsed from the real ``wt/bead/<type>/<id>`` branch ref (the ``branch``
    argument from ``managed()``'s row) via :func:`_bead_id_from_branch`, which strips the
    ``wt/bead/<type>/`` prefix.  This is the primary path: the actual ref preserves dots and other
    characters that the sanitized directory leaf loses (e.g. wt/bead/issue/
    vs. the dashed leaf -1).

    When ``branch`` is not supplied (legacy callers), the function falls back to reconstructing
    the branch from the directory leaf, probing each ``wt/bead/<type>/<leaf>`` namespace.

    The parent branch is resolved via :func:`integration_base`: the nearest started container
    ancestor (a parent epic/workstream branch ``wt/bead/epic/<parent>``) up the id chain, else
    ``integration``.
    """
    if branch:
        # Primary path: parse the bead id from the real branch ref supplied by managed().
        # This preserves dots that the sanitized directory leaf converts to dashes.
        bead_id: str | None = _bead_id_from_branch(branch)
    else:
        # Fallback for callers that do not supply the branch ref (legacy / no-op path).
        rel = Path(path).relative_to(config.worktrees_root())
        leaf = rel.parts[-1] if len(rel.parts) >= 4 else ""
        main = registry.hive_dir(entry)
        bead_id = None
        if leaf:
            for t in BEAD_KINDS:
                if _branch_exists(main, f"{_BEAD_PREFIX}{t}/{leaf}"):
                    bead_id = leaf
                    break

    parent = integration_base(entry, bead_id, integration) if bead_id else integration
    return bead_id, parent


def impl_diff_range(entry, base, branch) -> int:
    """Stream `git diff base..branch` to stdout (the net change). Returns git's exit code."""
    main = registry.hive_dir(entry)
    return _run_git(["git", "-C", str(main), "diff", f"{base}..{branch}"], check=False).returncode


def impl_log_range(entry, base, branch) -> str:
    """`git log --oneline base..branch` (oldest→newest) — the post-refine digest summary."""
    main = registry.hive_dir(entry)
    res = _run_git(
        [
            "git",
            "-C",
            str(main),
            "log",
            "--reverse",
            "--format=%h %ad %s",
            "--date=short",
            f"{base}..{branch}",
        ],
        check=False,
        capture=True,
    )
    return (res.stdout or "") if res.returncode == 0 else ""
