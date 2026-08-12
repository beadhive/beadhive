#!/usr/bin/env bash
# Run a command inside a filesystem + network fence (bh-pxoby, fixing bh-njdxk factor 1).
#
# WHY. The integration suite rewrote the git config and HQ lease state of whatever repo it ran
# in: it set `origin` to a pytest temp dir and flipped `core.bare = true` on the operator's live
# clone. Only the pre-push hook stopped 65 commits going to that temp dir. Nothing in the harness
# asserted "this test cannot see the operator's hive" — the suite happened to pass outside
# GIT_WORKSPACE, and that is not isolation.
#
# WHAT THE FENCE IS. bubblewrap: one binary, no daemon, no root, no KVM. Measured on
# beadhive-factory: 41ms per spawn. That price is the whole argument for this being the default
# rather than an opt-in — at 41ms, the safe path should not be the one someone has to remember.
#
#   * the host filesystem is READ-ONLY; only $REPO and a scratch dir are writable
#   * $HOME is a fresh tmpfs, so ~/.beads and ~/.gitconfig leave bd's upward resolution walk
#     entirely — that walk is the mechanism bh-njdxk names
#   * --unshare-all leaves LOOPBACK UP (verified), so a test's own dolt sql-server still works
#     while the internet does not
#   * the netns and tmpfs die with the run, and the scratch tree is removed on every exit path a
#     trap can see — normal, non-zero, INT, TERM, HUP. SIGKILL cannot be trapped, so that one
#     path still leaves its scratch dir behind; nothing in userspace can change that.
#
# WHAT IS STILL IN SCOPE, because the suite has to be able to write somewhere: TRACKED AND
# UNTRACKED FILES in the checkout under test. `.git` and `.beads` are read-only, so history,
# config, hooks and bead state are safe — but uncommitted work in the operator's live clone is
# not, and a test that writes over it will win. "The checkout is writable" reads milder than it
# is, so it is spelled out here.
#
# ESCAPE HATCH: BH_HERMETIC=0 runs unfenced. It says so, loudly — see bh-xx292, which decides
# whether an undeclared non-hermetic test is allowed to exist at all.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "${BH_HERMETIC:-1}" = "0" ]; then
    echo "⚠ hermetic: DISABLED by BH_HERMETIC=0 — this run may write outside ${REPO}" >&2
    exec "$@"
fi

if ! command -v bwrap >/dev/null 2>&1; then
    # Loud, never silent. bubblewrap is Linux-only and macOS is a real seat here, so this path
    # is expected — but a fence that quietly is not there is worse than no fence, because the
    # gate still reports green. The in-suite hermeticity assertions still run either way.
    {
        echo "⚠ hermetic: bubblewrap (bwrap) is not on PATH — running UNFENCED."
        echo "  bwrap is Linux-only. On this platform the suite CAN write to your real HOME,"
        echo "  git config and hive state; see bh-njdxk for what that cost last time."
        echo "  There is NO CI safety net: this repo's workflows are release-only and run no"
        echo "  tests, so an unfenced run here is the only run there is."
    } >&2
    exec "$@"
fi

# Writable scratch OUTSIDE the tmpfs $HOME: pytest's tmp tree (dolt stores, real servers) is far
# too big for RAM, and TMPDIR here is often under $HOME.
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/bh-hermetic-XXXXXX")"
# INT/TERM/HUP as well as EXIT. With EXIT alone, bash exiting on SIGINT took its status from the
# trap's last command (`rm -rf`, which succeeds), so an INTERRUPTED fenced gate exited 0 — a
# false green, the exact shape this bead exists to prevent. SIGTERM/SIGHUP were already correct
# (143/129); only SIGINT never reached the `exit` below. SIGKILL cannot be trapped, so that one
# path still leaks its scratch dir; nothing in userspace can fix that.
trap 'rc=$?; rm -rf "${SCRATCH}"; trap - INT TERM HUP EXIT; exit "${rc}"' EXIT
trap 'rm -rf "${SCRATCH}"; trap - INT TERM HUP EXIT; kill -INT $$' INT
trap 'rm -rf "${SCRATCH}"; trap - INT TERM HUP EXIT; kill -TERM $$' TERM
trap 'rm -rf "${SCRATCH}"; trap - INT TERM HUP EXIT; kill -HUP $$' HUP

args=(
    --ro-bind / /
    --dev /dev
    --proc /proc
    --tmpfs /tmp
    --tmpfs "${HOME}"
    --bind "${SCRATCH}" "${SCRATCH}"
    --bind "${REPO}" "${REPO}"
    --unshare-all
    --die-with-parent
    --setenv HOME "${HOME}"
    --setenv TMPDIR "${SCRATCH}"
    --setenv BH_HERMETIC_FENCE "1"
    --chdir "${REPO}"
)

# THE CHECKOUT'S OWN GIT AND BEAD STATE ARE READ-ONLY, and this is the whole point rather than a
# refinement. The suite must be able to write INSIDE the checkout (.venv, .pytest_cache), so
# $REPO is bound read-write above — but bh-njdxk's actual damage was `git config core.bare true`
# and `git remote set-url origin <tmpdir>` against the clone the suite was RUNNING IN, and at
# push time (scripts/main-push-gate.sh -> just check-all) that clone IS $REPO. A writable $REPO
# therefore leaves the original incident wide open, which an adversarial review reproduced in
# full: every mutation landed and survived the run.
#
# `.git` may be a DIRECTORY (ordinary clone) or a FILE (linked worktree, a gitdir: pointer);
# --ro-bind handles both. Do not mistake a linked worktree for protection: from a worktree the
# pointer's target is outside $REPO and the tmpfs hides it, so git reports "not a git repository"
# — the write fails for the wrong reason, and that accident disappears in the main clone.
for state in .git .beads; do
    [ -e "${REPO}/${state}" ] && args+=(--ro-bind "${REPO}/${state}" "${REPO}/${state}")
done

# Re-bind the toolchain read-only, AFTER the tmpfs that hid it. ~/.local/bin/uv is a SYMLINK
# into ~/.nix-profile, so binding ~/.local alone fails with "execvp uv: No such file or
# directory" — both paths are needed, and that is not obvious from the error.
# Only the BINARY directories, never all of ~/.local: ~/.local/share holds real bh and bd state
# (~/.local/share/beadhive), so binding ~/.local wholesale both leaks that state into the fence
# and makes it read-only — which broke `bd backup add` in the storage-migrate integration test
# with a finding that looked like a migration bug. Left unbound it lands on the tmpfs: writable,
# empty, and gone when the run ends.
for dir in .local/bin .local/lib .nix-profile; do
    [ -e "${HOME}/${dir}" ] && args+=(--ro-bind "${HOME}/${dir}" "${HOME}/${dir}")
done

# uv's cache is the one host path that must stay WRITABLE: uv takes a lock file inside it on
# every run and dies with "Could not acquire lock ... Read-only file system" otherwise. It is a
# content-addressed download cache, not project or hive state, so it is outside what this fence
# exists to protect — the git config, the bead stores and the operator's HOME still are not.
[ -e "${HOME}/.cache/uv" ] && args+=(--bind "${HOME}/.cache/uv" "${HOME}/.cache/uv")

# NOT `exec`: exec replaces this shell, so the EXIT trap never fires and $SCRATCH — which is
# TMPDIR inside the fence, i.e. pytest's whole tmp tree of dolt stores and hive clones — is left
# on the host. Measured before this was fixed: 60 directories, 2.2 GB, one per invocation. That
# is bh-njdxk's factor 3 (leaked state accumulating across runs) re-created in a new place by the
# very script claiming to have removed it. Run, keep the status, let the trap clean up, exit it.
rc=0
bwrap "${args[@]}" "$@" || rc=$?
exit "${rc}"
