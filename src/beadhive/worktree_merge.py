"""Integration-boundary merge tiers for ws-managed worktrees.

The `--no-ff` merge and its bounded conflict-recovery ladder (rebase-then-retry → union driver)
that the merger drives. Split out of `worktree.py` so the merge path sits in its own file from
the naming / path-resolution / init / ops surface — the contending feature beads touch different
files. `worktree.py` re-exports every public name here, so callers and tests keep importing them
as `ws.worktree.<name>`.

The lower-level git/worktree helpers these tiers compose (``_run_git``, ``is_clean``,
``current_branch``, ``clean_checkout``, ``reset_hard``, ``backup_branch``, ``_session_id``,
``rebase_onto``, ``rebase_abort``) stay in ``worktree`` and are reached through the module
(``worktree.<helper>``) at call time, so there is no import cycle at load.
"""

from __future__ import annotations

import os
from bisect import bisect_left
from pathlib import Path

from beadhive_worktrees import (
    all_union_eligible,
    is_zero_delta_rebase,
    parse_conflict_paths,
    union_attributes_text,
)

from . import registry, worktree


class MergePreconditionError(RuntimeError):
    """The integration workspace cannot start a merge; no content was compared."""


def merge_workspace_issue(main: Path, base: str, branch: str) -> str:
    """Describe workspace changes that would make checkout or merge unsafe.

    Check the index and tracked files separately from operator-owned untracked files. An
    untracked file is safe when checkout of `base` and merge of `branch` would leave it
    alone. The branch's whole tree is too broad: an unchanged file on a stale branch may
    already have been deleted on `base`, and Git does not restore it during the merge.
    Keep this preflight ahead of validation and let Git make the final merge decision under
    the merge slot.
    """
    git = lambda *args: worktree._run_git(  # noqa: E731 - all probes share the same cwd
        ["git", "-C", str(main), *args], check=False, capture=True
    )
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=no")
    if status.returncode != 0:
        return f"cannot inspect tracked changes in {main}: {(status.stderr or '').strip()}"
    if status.stdout:
        paths = [part for part in status.stdout.split("\0") if part]
        return "tracked changes block the merge: " + ", ".join(paths[:10])

    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    ignored = git("ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    base_tree = git("ls-tree", "-r", "--name-only", "-z", base)
    branch_tree = git("ls-tree", "-r", "--name-only", "-z", branch)
    common = git("merge-base", base, branch)
    if any(
        result.returncode != 0 for result in (untracked, ignored, base_tree, branch_tree, common)
    ):
        return f"cannot inspect incoming paths for {branch} onto {base}"
    changed = git("diff", "--name-only", "-z", common.stdout.strip(), branch)
    if changed.returncode != 0:
        return f"cannot inspect incoming paths for {branch} onto {base}"
    local_paths = set((untracked.stdout or "").split("\0")) | set(
        (ignored.stdout or "").split("\0")
    )
    branch_paths = set((branch_tree.stdout or "").split("\0"))
    incoming_paths = set((base_tree.stdout or "").split("\0")) | (
        set((changed.stdout or "").split("\0")) & branch_paths
    )
    local_paths.discard("")
    incoming_paths.discard("")
    sorted_incoming = sorted(incoming_paths)

    def collides(local: str) -> bool:
        if local in incoming_paths:
            return True
        parts = local.split("/")
        if any("/".join(parts[:index]) in incoming_paths for index in range(1, len(parts))):
            return True
        prefix = local + "/"
        index = bisect_left(sorted_incoming, prefix)
        return index < len(sorted_incoming) and sorted_incoming[index].startswith(prefix)

    collisions = sorted(local for local in local_paths if collides(local))
    if collisions:
        return "untracked path collides with the incoming merge: " + ", ".join(collisions[:10])
    return ""


def merge_no_ff(entry, branch, base, *, name="", email="", signing_key="", sign=False, message=""):
    """Integration-boundary merge: bring `branch` onto `base` in the hive's main clone with a
    real merge commit (`--no-ff`) — history preserved, never squashed. Checks out `base` first
    (refusing if the clone is dirty, so we never merge over someone's uncommitted work). Pass
    identity/signing overrides for an agent-mode merger; omit them to inherit the clone's git
    config (supervised). On content conflict the merge is aborted, leaving the clone clean and
    returning (rc, output). A dirty workspace, checkout refusal, or merge failure without
    unmerged paths raises MergePreconditionError; callers must not bounce reviewed work for it.

    Runs in the worktree that has `base` checked out (`clone_for_branch`): the main clone for a
    top-level land onto `main`, or the coordinator seat for a merge onto a container branch (which
    lives there, not the main clone) — see xn3o.6."""
    main = worktree.clone_for_branch(entry, base)
    issue = merge_workspace_issue(main, base, branch)
    if issue:
        raise MergePreconditionError(f"main clone {main} cannot merge: {issue}")
    if worktree.current_branch(main) != base:
        co = worktree._run_git(
            ["git", "-C", str(main), "checkout", base], check=False, capture=True
        )
        if co.returncode != 0:
            raise MergePreconditionError((co.stdout or "") + (co.stderr or ""))
    # Disable rerere: an integration-boundary merge must be deterministic — `_run_git` scrubs
    # GIT_CONFIG_GLOBAL so git falls back to the user's ~/.gitconfig, and a developer's rerere
    # cache could silently replay a stale resolution over a real conflict. We want a clean merge
    # or an explicit conflict (which then drives the rebase-retry), never a ghost resolution.
    cmd = ["git", "-C", str(main), "-c", "rerere.enabled=false"]
    if name:
        cmd += ["-c", f"user.name={name}"]
    if email:
        cmd += ["-c", f"user.email={email}"]
    if signing_key:
        cmd += [
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={os.path.expanduser(signing_key)}",
            "-c",
            f"commit.gpgsign={'true' if sign else 'false'}",
        ]
    cmd += ["merge", "--no-ff", "-m", message or f"chore(merge): {branch}", branch]
    res = worktree._run_git(cmd, check=False, capture=True)
    output = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0:
        unresolved = worktree._run_git(
            ["git", "-C", str(main), "diff", "--name-only", "--diff-filter=U"],
            check=False,
            capture=True,
        )
        worktree._run_git(["git", "-C", str(main), "merge", "--abort"], check=False, capture=True)
        if unresolved.returncode != 0 or not (unresolved.stdout or "").strip():
            raise MergePreconditionError(output)
    return res.returncode, output


def _ref_sha(main: Path, ref: str) -> str:
    """Full sha of `ref` in the hive's main clone ('' if it can't be resolved)."""
    res = worktree._run_git(["git", "-C", str(main), "rev-parse", ref], check=False, capture=True)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def merge_conflict_paths(entry, branch, base) -> tuple[list[str], str]:
    """Run a NON-aborting `--no-ff` merge of `branch` into `base` purely to enumerate the
    conflicted paths (`git diff --name-only --diff-filter=U`) BEFORE aborting it — so a caller
    can decide whether every conflicted path is union-eligible. Always leaves the base clone
    clean on `base` (aborts the probe merge). Returns (conflicted_paths, output)."""
    main = worktree.clone_for_branch(entry, base)
    if worktree.current_branch(main) != base:
        worktree._run_git(["git", "-C", str(main), "checkout", base], check=False, capture=True)
    res = worktree._run_git(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "rerere.enabled=false",
            "merge",
            "--no-ff",
            "--no-commit",
            branch,
        ],
        check=False,
        capture=True,
    )
    ures = worktree._run_git(
        ["git", "-C", str(main), "diff", "--name-only", "--diff-filter=U"],
        check=False,
        capture=True,
    )
    # Parsing is `beadhive_worktrees.parse_conflict_paths` (bh-qdezo.8, moved verbatim); the
    # probe merge and its abort are git-plumbing mechanics that stay here.
    paths = parse_conflict_paths(ures.stdout or "")
    worktree._run_git(["git", "-C", str(main), "merge", "--abort"], check=False, capture=True)
    return paths, (res.stdout or "") + (res.stderr or "")


def _all_union_eligible(paths, union_globs) -> bool:
    """True iff EVERY path matches at least one glob in `union_globs` (fnmatch). An empty
    `paths` is not eligible — there is nothing for the union driver to resolve.

    Thin forwarding wrapper: the algorithm itself is
    ``beadhive_worktrees.all_union_eligible`` (bh-qdezo.8, moved verbatim). Kept as a private
    module-level name since callers/tests still reach it as ``worktree_merge._all_union_eligible``.
    """
    return all_union_eligible(paths, union_globs)


def merge_with_union(entry, branch, base, union_globs, **idkw) -> tuple[int, str]:
    """`merge_no_ff` with git's built-in `union` merge driver activated for `union_globs` via a
    TRANSIENT `.git/info/attributes` in the main clone (`<glob> merge=union` lines). The attribute
    file is always removed — or, if one pre-existed, restored byte-for-byte — in a finally, so we
    never clobber a hand-maintained info/attributes. (rc, output) from the merge."""
    main = registry.hive_dir(entry)
    info = main / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    attrs = info / "attributes"
    had = attrs.exists()
    saved = attrs.read_text() if had else None
    try:
        # Rendering is `beadhive_worktrees.union_attributes_text` (bh-qdezo.8, moved
        # verbatim); writing the transient file and the merge itself stay git-plumbing mechanics.
        attrs.write_text(union_attributes_text(union_globs))
        return merge_no_ff(entry, branch, base, **idkw)
    finally:
        if had:
            attrs.write_text(saved or "")
        else:
            attrs.unlink(missing_ok=True)


def _try_union_tier(
    entry, branch, base, target: Path, backup, union_globs, validate_cmd, idkw
) -> tuple[int, str, str]:
    """The bounded union tier. Precondition: the main clone is clean on `base` and the bead
    branch sits at `backup` (its pre-merge tip). Returns (rc, out, how):
      - ("union") only when EVERY conflicted path is whitelisted, the union-driver merge lands,
        AND mandatory re-validation (clean_checkout with validate_cmd) passes.
      - ("conflict") on any other outcome, having restored BOTH the integration branch (hard
        reset to its pre-union tip) and the bead branch (reset to `backup`) — work is never lost.
    A no-op (returns conflict immediately) when `union_globs` is empty."""
    if not union_globs:
        return 1, "", "conflict"
    main = worktree.clone_for_branch(entry, base)  # reset `base` where it lives (seat or clone)
    paths, dout = merge_conflict_paths(entry, branch, base)
    if not _all_union_eligible(paths, union_globs):
        worktree.reset_hard(target, backup)  # bead branch back to its pre-merge tip; main clean
        return 1, dout, "conflict"

    pre_union = _ref_sha(main, base)  # snapshot integration tip to roll back to on failure
    try:
        rc, out = merge_with_union(entry, branch, base, union_globs, **idkw)
    except MergePreconditionError:
        worktree.reset_hard(target, backup)
        raise
    if rc != 0:
        worktree.reset_hard(main, pre_union)  # union merge itself conflicted — undo partial state
        worktree.reset_hard(target, backup)
        return rc, dout + out, "conflict"

    if validate_cmd:
        vrc = worktree.clean_checkout(entry, base, validate_cmd, phase="union")
        if vrc != 0:
            worktree.reset_hard(main, pre_union)  # never land a union result that fails validation
            worktree.reset_hard(target, backup)
            return vrc, dout + out, "conflict"
    return 0, dout + out, "union"


def try_merge_rebase(
    entry,
    branch,
    base,
    target: Path,
    *,
    name="",
    email="",
    signing_key="",
    sign=False,
    message="",
    union_globs: tuple[str, ...] = (),
    validate_cmd: str = "",
) -> tuple[int, str, str]:
    """Integration merge with a bounded **rebase-then-retry** conflict recovery and an optional
    **bounded union tier**, returning (rc, output, how) where how ∈ {"clean", "rebased", "union"}
    on success.

    Strategy — recover the file-coupled-but-DAG-parallel case where the bead merely needs to
    *replay on a newer base* instead of being hand-serialized:
      1. Try a plain `--no-ff` merge. Clean → done (how="clean"), behaviour unchanged.
      2. On conflict the first merge already aborted (main left clean on `base`). Snapshot the
         bead branch behind a backup ref (like `refine` does), then `git rebase <base>` the bead
         branch in its worktree to replay its commits onto the newer base, and retry the merge.
         A clean retry with at least one replayed commit → done (how="rebased"). If Git drops
         every commit as already applied, restore the reviewed branch and fail closed: there is
         no distinct history left from which a child integration bubble could be created.
      3. **Union tier** (only when `union_globs` is non-empty): the rebase path did not resolve, so
         probe the conflicted paths from a non-aborting `--no-ff` merge. IFF *every* conflicted
         path matches a glob in `union_globs` (fnmatch), retry the merge with git's built-in
         `union` driver applied via a transient `.git/info/attributes` (keeps both sides of an
         append-only file), then run MANDATORY re-validation of the merged integration tip from a
         clean checkout (`validate_cmd`). Only a union merge that lands AND validates returns
         (0, out, "union"). Any path outside the whitelist, a union merge that still conflicts, or
         a failed re-validation hard-resets the integration branch to its pre-union tip, restores
         the bead branch from the backup ref, and falls through to the bounce.
      4. Otherwise it's a *real* conflict: restore the bead branch to its pre-rebase tip from the
         backup ref and surface a non-zero failure so the merger bounces it for rework. Work is
         never dropped — neither the integration branch nor the bead branch is left mutated.

    What replay actually fixes: a 3-way `--no-ff` merge resolves conflicts against the *old*
    merge-base, so a sibling's already-landed change (e.g. two coupled beads that both added the
    same import / boilerplate line, or a bead forked off a stale base) collides spuriously.
    Rebasing replays the bead's commits one-by-one onto the current tip — git may drop individual
    already-applied patches while retaining and landing the bead's unique work cleanly. It must
    not drop the entire reviewed range and then close the child without a bubble. The union tier
    catches the narrower append-only case (two beads each appending a different line at a
    whitelisted file's EOF) that no replay order resolves, while re-validation guards against
    landing a union-merged result that doesn't actually build/test.

    `target` is the bead branch's worktree (where the branch is checked out) — the rebase runs
    there since a branch can only be rebased where it lives. `union_globs` defaults to empty
    (union disabled ⇒ behaviour identical to before). Identity/signing kwargs match
    `merge_no_ff`."""
    idkw = dict(name=name, email=email, signing_key=signing_key, sign=sign, message=message)
    rc, out = merge_no_ff(entry, branch, base, **idkw)
    if rc == 0:
        return 0, out, "clean"

    # Conflict: main is already aborted/clean on `base`. Snapshot, rebase, retry.
    backup = worktree.backup_branch(entry, branch, worktree._session_id(), label="premerge")
    rrc, rout = worktree.rebase_onto(target, base)
    if rrc != 0:
        worktree.rebase_abort(target)
        worktree.reset_hard(target, backup)
        urc, uout, uhow = _try_union_tier(
            entry, branch, base, target, backup, union_globs, validate_cmd, idkw
        )
        if uhow == "union":
            return urc, out + rout + uout, "union"
        return rrc, out + rout + uout, "conflict"

    # Git may report a successful rebase after dropping every reviewed patch because equivalent
    # content is already present on the newer base.  A subsequent `merge --no-ff` then says
    # "Already up to date" and creates no child integration bubble at all.  Treat that as a
    # conflict-shaped, recoverable bounce: the merger cannot close a child whose reviewed history
    # has no distinct commit left to attribute, and must restore the submitted branch unchanged.
    # Recognising the drop-everything outcome is `beadhive_worktrees.is_zero_delta_rebase`
    # (bh-qdezo.8, moved verbatim); reading the commit shas is git-plumbing mechanics.
    if is_zero_delta_rebase(worktree.commit_shas(entry, branch, base)):
        worktree.reset_hard(target, backup)
        return (
            1,
            out
            + rout
            + "\nzero-delta rebase dropped every reviewed child commit as already applied; "
            "refusing to close without an attributable no-ff integration bubble",
            "conflict",
        )

    try:
        rc2, out2 = merge_no_ff(entry, branch, base, **idkw)
    except MergePreconditionError:
        worktree.reset_hard(target, backup)
        raise
    if rc2 != 0:
        worktree.reset_hard(target, backup)  # restore the pre-rebase bead branch — never drop work
        urc, uout, uhow = _try_union_tier(
            entry, branch, base, target, backup, union_globs, validate_cmd, idkw
        )
        if uhow == "union":
            return urc, out + rout + out2 + uout, "union"
        return rc2, out + rout + out2 + uout, "conflict"
    return 0, out2, "rebased"
