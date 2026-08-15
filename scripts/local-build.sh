#!/usr/bin/env bash
# Build/install bh from THIS checkout, stamped with a PEP 440 local version segment (bh-7hacm).
#
# WHY. `uv tool install --force .` from a tree carrying 19 merged beads produced a `bh` reporting
# 0.11.5 — byte-identical to the published 0.11.5 those beads POSTDATE. `bh --version` was useless
# as an install check, and the only way to tell old from new was probing for verbs that did not
# exist before. A version that cannot distinguish "the release" from "someone's working tree"
# makes every field report ambiguous.
#
# THE STAMP is a PEP 440 LOCAL VERSION IDENTIFIER:
#     0.11.5+local.g790ef0d          built from a clean tree at that commit
#     0.11.5+local.g790ef0d.dirty    uncommitted changes were present
# Three properties make that correct rather than merely conventional:
#   1. it sorts AFTER the same public version, so a local build never reads as older than the
#      release it came from;
#   2. PYPI FORBIDS LOCAL SEGMENTS, so a stamped artifact physically cannot be published by
#      accident — enforced by the packaging ecosystem, not by our discipline;
#   3. importlib.metadata returns it verbatim, so `bh --version` (cli.py, which reads the
#      INSTALLED distribution's metadata) picks it up with NO code change to src/beadhive.
#
# THE BASE IS DERIVED, NEVER TYPED — scripts/release-pin.sh reads `[project] version`, the same
# field commitizen owns (`version_provider = "pep621"`, so `cz version --project` reads it too).
# It is preferred over shelling out to cz here for three reasons: it needs no venv sync (`uv run
# cz` would create/refresh one — a mutation, and this script promises none), it works in a
# checkout with no dev group installed, and it already REFUSES a tree whose tag disagrees with
# pyproject. `cz bump` remains the only thing that ever changes that number.
#
# NOTHING MUTATES THE REAL TREE. The obvious implementation — stamp pyproject, build, `git
# checkout pyproject.toml` — transiently dirties a clone several agents work in, leaves pyproject
# and uv.lock modified when the build FAILS, and has to run dirty detection before the mutation
# or every build reports `.dirty`. Instead the stamp is applied inside a throwaway `git worktree`
# whose content is made to match the real working tree (so uncommitted work is really built), and
# dirty detection reads the truth because it runs against the real tree first.
#
# ONE WORKTREE PER INVOCATION, PLUS A SWEEP. A fixed path is the pattern bh-gc4h1 was: two
# concurrent runs collide, `git worktree add` fails on an existing path, and "remove it first" is
# itself the race. Same answer worktree.clean_checkout reached (`verify-<leaf>-<rand6>`, bh-nikb).
# The price is accumulation on SIGKILL, which cannot be trapped — hence `sweep`, below, which
# BOUNDS that leak rather than preventing it.
#
# Usage:
#   local-build.sh version    print the stamped version on stdout and nothing else
#   local-build.sh install    uv tool install --force '.[otel]', stamped
#   local-build.sh build      uv build into the REAL tree's dist/, stamped
#
# A publishable (unstamped) artifact is deliberately NOT available here: run `uv build` — which
# is exactly what .github/workflows/release.yml does on a v* tag. Forgetting to opt out therefore
# yields an artifact PyPI will reject, never a silently mislabelled one.
#
# --------------------------------------------------------------------------------------------
# EDITABLE (`uv tool install -e .`) IS DECLINED, not overlooked. The stamp would apply — the
# version is baked into dist-info the same way — but the INSTALL then follows the working tree
# while the STAMP does not: commit once and `bh --version` names a sha it is no longer running.
# A stale-but-plausible version is worse than no stamp, because it invites trust, and this bead
# exists precisely because a plausible version could not be trusted. If it is ever added it must
# be stamped `+editable` with NO SHA — honest that it cannot name a commit rather than naming one
# it will outlive. Note that `install_plane.EDITABLE` is a different thing: "this bh is running
# out of a source checkout", not `pip -e`.
#
# THE SWEEP STAYS SEPARATE FROM bh-x2g6v'S hermetic-dir sweep, deliberately. It shares that
# bead's PROBLEM (SIGKILL cannot be trapped, so bound the leak) and its PATTERN (unique name,
# liveness, an age backstop, and reporting what was removed — the shape clean_checkout /
# sweep_verify_dirs already settled here), but not a mechanism, for two reasons that are not
# stylistic:
#   * DIFFERENT CLEANUP CONTRACT. A hermetic scratch dir is an ordinary directory: `rm -rf` is
#     the whole of it. A build worktree is REGISTERED WITH GIT, so its cleanup has a second half
#     (.git/worktrees bookkeeping, cleared only by `worktree remove` / `worktree prune`) that has
#     no analogue on the other side. A shared sweep would carry a git-specific branch that never
#     fires for hermetic dirs.
#   * DIFFERENT LAYER. bh-x2g6v's sweep runs inside the bh process at session start; this one
#     runs in shell, in `just install` — the command whose PURPOSE is to produce that bh. That
#     rules out ONE particular way of sharing: routing this sweep THROUGH bh would make building
#     bh depend on an already-working bh, the bootstrap this repo cannot assume (see the
#     justfile's own `${BH_EXEC:-bh}` guards, which exist because the installed bh lags the
#     tree). It does not forbid sharing as such — a shell helper, or just the pattern, would
#     carry no bh dependency. This is the secondary reason; the cleanup contract above carries
#     the conclusion on its own.
# Reusing a pattern is not duplicating a mechanism; merging these two would be.
# --------------------------------------------------------------------------------------------
set -euo pipefail

# The checkout is the script's own parent, not the cwd — `just` runs recipes from the justfile
# directory, and deriving it here keeps the script correct when run by hand from anywhere
# (same reasoning, and the same line, as scripts/release-pin.sh).
root="$(cd "$(dirname "$0")/.." && pwd)"

mode="${1-}"
case "$mode" in
  version | install | build) ;;
  *)
    echo "local-build: expected one of: version | install | build (got '${mode}')" >&2
    exit 2
    ;;
esac

# ---- the stamp --------------------------------------------------------------

base="$("$root/scripts/release-pin.sh")"  # exits 1 on tag/pyproject skew — a broken release
sha="$(git -C "$root" rev-parse --short=7 HEAD)"
# Read the REAL tree, before anything else happens. `--porcelain` counts untracked-but-not-ignored
# files too: a build that picks up a new module nobody committed is not the commit it names.
dirty=""
[ -n "$(git -C "$root" status --porcelain)" ] && dirty=".dirty"
stamped="${base}+local.g${sha}${dirty}"

if [ "$mode" = "version" ]; then
  printf '%s\n' "$stamped"
  exit 0
fi

# ---- scratch worktrees: unique per invocation, swept at entry ---------------

# Under the COMMON git dir, so it is invisible to `git status` (a scratch dir inside the working
# tree would itself dirty the tree it is measuring) and dies with the clone.
scratch="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir)/bh-build"

# Both halves of cleanup, in the one place that always knows both. Removing the DIRECTORY is not
# enough: `git worktree add` writes administrative files under .git/worktrees/ that only
# `worktree remove` / `worktree prune` clears, and they accumulate invisibly.
drop() {
  git -C "$root" worktree remove --force "$1" >/dev/null 2>&1 || rm -rf "$1"
  git -C "$root" worktree prune
}

# BOUNDS the SIGKILL leak; it does not prevent it. SIGKILL cannot be trapped, so a killed build
# leaves its worktree behind and only a later run can reap it — which is why this is not optional.
# What it cannot catch: a run in flight (deliberately — see the liveness test), and a leak on a
# clone nobody ever builds from again. Reports what it removes; an invisible leak is one that
# returns.
sweep() {
  [ -d "$scratch" ] || return 0
  local d pid
  for d in "$scratch"/build-*; do
    [ -d "$d" ] || continue
    pid="${d##*/build-}"
    pid="${pid%%-*}"
    # `ps -p` rather than `kill -0`: kill(2) fails with EPERM on a live process owned by someone
    # else, which would read as "dead" and reap a worktree out from under a running build. Age is
    # the backstop for the one case liveness gets wrong in the other direction — a recycled pid
    # that happens to be alive keeps an orphan alive forever.
    if ps -p "$pid" >/dev/null 2>&1 &&
      [ -z "$(find "$d" -maxdepth 0 -mmin "+${BH_BUILD_SWEEP_TTL_MIN:-1440}" 2>/dev/null)" ]; then
      continue
    fi
    echo "local-build: sweeping orphaned build worktree $(basename "$d")" >&2
    drop "$d"
  done
}

work=""
cleanup() {
  if [ -n "$work" ]; then
    drop "$work"
  fi
}
# EXIT alone is not enough: bash runs the EXIT trap on an UNTRAPPED fatal signal only if the
# signal is also trapped. Same set scripts/hermetic.sh traps, for the same reason.
trap cleanup EXIT INT TERM HUP

sweep
mkdir -p "$scratch"
# pid is already unique among CONCURRENT runs — which is the collision this is defending against.
# $RANDOM only has to separate a live run from an ORPHAN left by a killed one that happened to
# hold the same recycled pid. A bash builtin, so nothing here depends on a PATH lookup.
work="$scratch/build-$$-$RANDOM"

git -C "$root" worktree add --detach --quiet "$work" HEAD

# Make the throwaway match the WORKING TREE, not just HEAD. `just install` exists to put the code
# you are looking at on PATH; building HEAD would silently drop every uncommitted edit — a far
# worse lie than the one this script exists to fix, and it is what the `.dirty` marker is
# promising is present. `ls-files` decides what "the tree as it is now" means, so .gitignore is
# honoured for free and .git, .venv and dist/ are never copied.
# --ignore-failed-read: `--cached` names INDEX paths, and an unstaged `rm` leaves one the
# worktree no longer has. Without it tar exits 2 on the first such path and `set -euo pipefail`
# kills the build — `just install` simply broken in an ordinary tree state, with nothing in the
# message naming this script.
git -C "$root" ls-files -z --cached --others --exclude-standard |
  tar -C "$root" --null -T - --ignore-failed-read -cf - | tar -C "$work" -xf -
# ...and REMOVE what the real tree deleted, which the HEAD checkout above still holds. Both
# flags are load-bearing, and `ls-files --deleted` (the obvious spelling) gets both wrong:
#   * `diff HEAD` sees STAGED deletions — after `git rm`, `--deleted` prints nothing, so the
#     wheel would keep a module you removed, silently, ENDORSED by the `.dirty` stamp.
#   * `--no-renames` decomposes `git mv a b` into D a + A b. Without it git reports R, the D
#     filter matches nothing, and the wheel ships BOTH paths.
git -C "$root" diff -z --no-renames --name-only --diff-filter=D HEAD |
  (cd "$work" && xargs -0 -r rm -f)

# The one mutation, and it lands in the throwaway. `--frozen --no-sync` keeps it to the single
# pyproject key: no re-lock, no .venv.
(cd "$work" && uv version --frozen "$stamped" >/dev/null)

case "$mode" in
  install) (cd "$work" && uv tool install --force '.[otel]') ;;
  # --out-dir: an artifact that vanishes with the scratch dir is useless. The wheel FILENAME then
  # carries the stamp — beadhive-0.11.5+local.g790ef0d-py3-none-any.whl — so anyone validating a
  # build knows what is under test from the name alone, with nothing to look up.
  build) (cd "$work" && uv build --out-dir "$root/dist") ;;
esac
