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
#   * --unshare-all also gives the run its OWN PID NAMESPACE, and that is the structural answer
#     to the orphaned-dolt-server leak (bh-7wp2y): a server the suite starts CANNOT outlive the
#     fence, because the namespace dies with it — the leak becomes impossible rather than cleaned
#     up afterwards. Measured, not assumed: `sleep` backgrounded inside the fence gets inner pid 3
#     and is gone from the host the moment the fence exits. `--die-with-parent` closes the other
#     direction (SIGKILL the wrapper and bwrap plus every descendant goes with it). Where there is
#     NO fence — macOS, BH_HERMETIC=0, a bare `pytest` — the backstop is the session sweep,
#     `harness.world.sweep_orphaned_dolt_servers`, whose docstring states what it cannot catch.
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
BWRAP_PID=""
CLEANED_UP=0

cleanup_once() {
    if [ "${CLEANED_UP}" -eq 0 ]; then
        CLEANED_UP=1
        rm -rf "${SCRATCH}"
    fi
}

terminate_fence() {
    local requested_signal="$1"
    local pid="${BWRAP_PID}"

    if [ -n "${pid}" ]; then
        # Bash job control starts bwrap in its own process group below, so its pid is also the
        # group id inherited by every fenced descendant. Signal the group, wait briefly for
        # cooperative shutdown, then
        # close the namespace with SIGKILL if any process ignored INT/TERM/HUP. Reap the leader so
        # neither it nor a descendant can outlive cleanup or retain the caller's output pipes.
        kill "-${requested_signal}" -- "-${pid}" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 -- "-${pid}" 2>/dev/null || break
            sleep 0.05
        done
        if kill -0 -- "-${pid}" 2>/dev/null; then
            kill -KILL -- "-${pid}" 2>/dev/null || true
        fi
        wait "${pid}" 2>/dev/null || true
        BWRAP_PID=""
    fi
}

on_signal() {
    local requested_signal="$1"
    local signal_number="$2"

    # Ignore a second cancellation while the first handler closes the process group. Removing
    # EXIT here makes cleanup_once the sole owner of the signal path instead of running it again
    # when this shell terminates itself below.
    trap '' INT TERM HUP
    trap - EXIT
    terminate_fence "${requested_signal}"
    cleanup_once

    # Preserve native signal termination for callers that inspect returncode, with 128+signal as
    # a defensive fallback for shells that defer or ignore a self-signal.
    trap - "${requested_signal}"
    kill "-${requested_signal}" "$$"
    exit "$((128 + signal_number))"
}

on_exit() {
    local rc=$?
    trap '' INT TERM HUP
    trap - EXIT
    # This is normally already reaped. It also closes the group if an unrelated shell error
    # occurs after launch, without letting the error path leak a namespace or scratch tree.
    terminate_fence TERM
    cleanup_once
    exit "${rc}"
}

# INT/TERM/HUP as well as EXIT. With EXIT alone, bash exiting on SIGINT took its status from the
# trap's last command (`rm -rf`, which succeeds), so an INTERRUPTED fenced gate exited 0. Signal
# handlers now close and reap the complete fenced group before cleaning the scratch tree exactly
# once. SIGKILL cannot be trapped, so that one path can still leave scratch behind.
trap on_exit EXIT
trap 'on_signal INT 2' INT
trap 'on_signal TERM 15' TERM
trap 'on_signal HUP 1' HUP

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

# Resolve the package cache before bwrap overlays /tmp and $HOME. A selected host tmpfs cache
# must be rebound after that overlay or the framework sees an empty private /tmp and the locality
# guarantee disappears only inside the authoritative validation fence.
CACHE_LINES=()
CACHE_APPLICATION=""
CACHE_TARGET=""
case " $* " in
    *" pnpm "*) CACHE_APPLICATION="pnpm"; CACHE_TARGET="${REPO}/node_modules" ;;
    *" uv "*) CACHE_APPLICATION="uv"; CACHE_TARGET="${REPO}/.venv" ;;
esac
CACHE_PYTHON="${REPO}/.venv/bin/python"
if [ ! -x "${CACHE_PYTHON}" ]; then
    CACHE_PYTHON="$(command -v python3 || true)"
fi
if [ -n "${CACHE_APPLICATION}" ] && [ -n "${CACHE_PYTHON}" ]; then
    mapfile -t CACHE_LINES < <(
        PYTHONPATH="${REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" \
            "${CACHE_PYTHON}" -m beadhive.cache_locality "${CACHE_APPLICATION}" "${REPO}" \
                --target "${CACHE_TARGET}" --format lines
    )
fi
if [ "${#CACHE_LINES[@]}" -ge 2 ]; then
    CACHE_PATH="${CACHE_LINES[0]}"
    CACHE_LINK_MODE="${CACHE_LINES[1]}"
    if [ -d "${CACHE_PATH}" ]; then
        # --tmpfs /tmp and --tmpfs $HOME above hide the host destination tree. --dir recreates
        # every destination component, including the cache mountpoint itself, in order before the
        # later writable bind. This also handles a durable path nested several levels below HOME.
        CACHE_DEST=""
        IFS='/' read -r -a CACHE_COMPONENTS <<< "${CACHE_PATH#/}"
        for CACHE_COMPONENT in "${CACHE_COMPONENTS[@]}"; do
            [ -z "${CACHE_COMPONENT}" ] && continue
            CACHE_DEST="${CACHE_DEST}/${CACHE_COMPONENT}"
            args+=(--dir "${CACHE_DEST}")
        done
        args+=(--bind "${CACHE_PATH}" "${CACHE_PATH}")
        if [ "${CACHE_APPLICATION}" = "uv" ]; then
            args+=(--setenv UV_CACHE_DIR "${CACHE_PATH}" --setenv UV_LINK_MODE "${CACHE_LINK_MODE}")
        else
            args+=(--setenv npm_config_store_dir "${CACHE_PATH}")
            args+=(--setenv npm_config_package_import_method "${CACHE_LINK_MODE}")
        fi
    else
        echo "⚠ cache locality: selected cache path does not exist: ${CACHE_PATH}" >&2
    fi
    if [ "${CACHE_LINK_MODE}" = "copy" ] || [[ "${CACHE_LINES[2]:-}" == *fallback* ]]; then
        echo "⚠ cache locality: ${CACHE_LINES[2]:-copy fallback selected}" >&2
    fi
fi

# THE CHECKOUT'S OWN GIT AND BEAD STATE ARE READ-ONLY, and this is the whole point rather than a
# refinement. The suite must be able to write INSIDE the checkout (.venv, .pytest_cache), so
# $REPO is bound read-write above — but bh-njdxk's actual damage was `git config core.bare true`
# and `git remote set-url origin <tmpdir>` against the clone the suite was RUNNING IN, and at
# push time (scripts/main-push-gate.sh -> just check-all) that clone IS $REPO. A writable $REPO
# therefore leaves the original incident wide open, which an adversarial review reproduced in
# full: every mutation landed and survived the run.
#
# `.git` may be a DIRECTORY (ordinary clone) or a FILE (linked worktree, a gitdir: pointer);
# --ro-bind handles both. A linked worktree needs the block below as well.
for state in .git .beads; do
    [ -e "${REPO}/${state}" ] && args+=(--ro-bind "${REPO}/${state}" "${REPO}/${state}")
done

# A LINKED WORKTREE NEEDS ITS GITDIR BOUND TOO, READ-ONLY (bh-gsg8x). Binding the `.git` FILE
# binds only the `gitdir:` pointer; its target lives outside $REPO — under the main clone — and
# the tmpfs $HOME hides it, so inside the fence `git rev-parse` answers "fatal: not a git
# repository: (null)". That is not a theoretical wart: EVERY bead is developed in a
# `wt/bead/<id>` worktree, so the fenced gate ran with git broken in exactly the checkout the
# work happens in, and one integration test failed there and nowhere else
# (test_migrated_furnished_hive_does_not_untrack_the_moved_aside_store, quarantined by bh-pxoby
# as an unexplained fence incompatibility).
#
# DEMONSTRATED, not inferred — three runs of that one test, one variable each:
#   fenced from the linked worktree                                  FAIL
#   fenced from the MAIN CLONE (a real .git directory)               PASS
#   fenced from the linked worktree + this gitdir bind               PASS
#
# READ-ONLY, so the protection is unchanged: the common dir is the main clone's `.git`, which is
# precisely the history/config/hooks this fence exists to keep the suite out of. The worktree's
# own gitdir lives inside that common dir, so one bind covers both.
if [ -f "${REPO}/.git" ]; then
    GIT_COMMON="$(cd "${REPO}" && git rev-parse --git-common-dir 2>/dev/null || true)"
    case "${GIT_COMMON}" in
        "") ;;
        /*) ;;
        *) GIT_COMMON="${REPO}/${GIT_COMMON}" ;;
    esac
    if [ -n "${GIT_COMMON}" ] && [ -d "${GIT_COMMON}" ]; then
        GIT_COMMON="$(cd "${GIT_COMMON}" && pwd -P)"
        args+=(--ro-bind "${GIT_COMMON}" "${GIT_COMMON}")
    fi
fi

# Re-bind the toolchain read-only, AFTER the tmpfs that hid it. ~/.local/bin/uv is a SYMLINK
# into ~/.nix-profile, so binding ~/.local alone fails with "execvp uv: No such file or
# directory" — both paths are needed, and that is not obvious from the error.
# Only the BINARY directories, never all of ~/.local: ~/.local/share holds real bh and bd state
# (~/.local/share/beadhive), so binding ~/.local wholesale both leaks that state into the fence
# and makes it read-only — which broke `bd backup add` in the storage-migrate integration test
# with a finding that looked like a migration bug. Left unbound it lands on the tmpfs: writable,
# empty, and gone when the run ends.
#
# Two ~/.local/share toolchain paths are added to that list. `mise` holds scie-pants and other
# installed tools. `uv/python` holds uv's managed interpreters: an outer `uv sync` may select one
# for .venv, whose executable symlink must keep resolving inside the fence. Both are disjoint from
# ~/.local/share/beadhive's bh/bd state and stay read-only; binding the selected interpreter
# preserves the outer environment instead of silently switching Python versions inside validation.
for dir in .local/bin .local/lib .local/share/mise .local/share/uv/python .nix-profile; do
    [ -e "${HOME}/${dir}" ] && args+=(--ro-bind "${HOME}/${dir}" "${HOME}/${dir}")
done

# uv's cache is the one host path that must stay WRITABLE: uv takes a lock file inside it on
# every run and dies with "Could not acquire lock ... Read-only file system" otherwise. It is a
# content-addressed download cache, not project or hive state, so it is outside what this fence
# exists to protect — the git config, the bead stores and the operator's HOME still are not.
if { [ "${CACHE_APPLICATION}" = "uv" ] || [ -z "${CACHE_APPLICATION}" ]; } &&
    [ "${#CACHE_LINES[@]}" -lt 2 ] && [ -e "${HOME}/.cache/uv" ]; then
    args+=(--bind "${HOME}/.cache/uv" "${HOME}/.cache/uv")
fi

# Same reasoning for Pants (bh-1j3ei.2): `~/.cache/nce` is scie-pants's own bootstrap cache
# (its downloaded interpreter + the Pants engine venv). It is content-addressed, but NOT
# read-only safe like the toolchain dirs above — every invocation has pants_loader chmod its
# cached `sandboxer` binary executable again, so a ro-bind dies with "Read-only file system".
# `~/.cache/beadhive/pants` is scripts/pants_cache.py's local store/worktree cache and, like
# uv's, takes its own lock and gets written to on every run, so it needs to stay WRITABLE too.
# Both are unbound (i.e. still hidden on the tmpfs) unless already populated on the host — a
# first-ever Pants invocation still needs network to seed them, same as a first-ever `uv sync`
# needing network to seed ~/.cache/uv.
[ -e "${HOME}/.cache/nce" ] && args+=(--bind "${HOME}/.cache/nce" "${HOME}/.cache/nce")
[ -e "${HOME}/.cache/beadhive/pants" ] &&
    args+=(--bind "${HOME}/.cache/beadhive/pants" "${HOME}/.cache/beadhive/pants")

# NOT `exec`: exec replaces this shell, so the EXIT trap never fires and $SCRATCH — which is
# TMPDIR inside the fence, i.e. pytest's whole tmp tree of dolt stores and hive clones — is left
# on the host. Measured before this was fixed: 60 directories, 2.2 GB, one per invocation. That
# is bh-njdxk's factor 3 (leaked state accumulating across runs) re-created in a new place by the
# very script claiming to have removed it. Run, keep the status, let the trap clean up, exit it.
rc=0
# Non-interactive Bash otherwise starts asynchronous commands with SIGINT and SIGQUIT ignored.
# Monitor mode both preserves their normal signal dispositions and puts this job in a dedicated
# process group, without creating a new session for everything inside the fence. Turn it off as
# soon as the group exists so the wrapper's remaining control flow keeps normal script semantics.
set -m
bwrap "${args[@]}" "$@" &
BWRAP_PID=$!
set +m
wait "${BWRAP_PID}" || rc=$?
BWRAP_PID=""
exit "${rc}"
