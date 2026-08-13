#!/usr/bin/env bash
# Push the integration branch THROUGH the ~6-minute pre-push gate without the two ways that
# push has silently failed on this repo (bh-53o8f). Use it — `just push` — instead of a bare
# `git push` for anything that updates `main`.
#
# THE FAILURE IT EXISTS FOR, measured three times while pushing 0.11.2 (2026-08-12):
#
#   git push connects and negotiates refs BEFORE the pre-push hook runs — the hook needs the
#   remote's ref list on its stdin, so the connection is already open when it starts. The hook
#   then execs `just check-all`, which takes ~390s. The SSH connection sits idle that whole time,
#   GitHub closes it, and git finishes a GREEN gate and writes to a dead socket:
#
#       Connection to github.com closed by remote host.     <- mid-gate, ~5-6 min in
#       EXIT=141                                            <- 128+13 = SIGPIPE
#       error: failed to push some refs to github.com:beadhive/beadhive.git
#
#   Run 1 was worse than run 2, and is why this is a defect rather than an annoyance: its gate
#   passed CLEAN ("main-gate (371.39 seconds)", every phase green, tripwires clean) and the
#   remote still never moved. It was reported as a successful push on that basis. `git ls-remote`
#   against the actual remote is what caught it, an hour later.
#
# WHAT THIS SCRIPT DOES ABOUT IT — three things, in the order they matter:
#
#   1. KEEPALIVE. `-o ServerAliveInterval=30 -o ServerAliveCountMax=10` via GIT_SSH_COMMAND, so
#      the connection is not idle during the gate and GitHub does not drop it. This is the fix
#      shape the bead ranks first: cheapest, and CORRECT — the connection is genuinely still
#      wanted. Proven on three consecutive pushes on 2026-08-12/13, including 0.11.3's.
#      DO NOT REMOVE IT AS UNEXPLAINED CRUFT. Without it a fully green gate silently does not
#      push, roughly one time in three on this link.
#
#   2. VERIFY AGAINST THE REMOTE, NOT THE LOCAL TRACKING REF. `git ls-remote` after the push, and
#      compare the sha the remote actually holds against the sha we meant to send. A green gate
#      followed by "did it land?" answered from `origin/main` is answered from a local ref that
#      a failed push may never have updated — which is exactly how run 1 was reported as
#      successful.
#
#   3. DISTINGUISH "the gate failed" FROM "the gate passed and the transport failed". Those need
#      different responses (fix the code vs retry the push) and a bare "failed to push some refs"
#      names neither. Exit 141 (SIGPIPE) after a green gate gets its own banner.
#
# THE PIPE TRAP, called out here because it cost two false "it pushed" reports on the same night:
#
#       git push | tail          # <- returns TAIL's exit status. ALWAYS 0. The failure is gone.
#
#   Any wrapper that pipes git push through anything — `| tail`, `| grep`, `| cat` — throws away
#   git's exit code unless it also sets `set -o pipefail` or reads ${PIPESTATUS[0]}. This script
#   never pipes the push; it runs it, keeps `$?`, and then checks the remote anyway, because an
#   exit code alone is not evidence that a ref moved.
#
#   THE TRAP APPLIES TO EVERY COMMAND WHOSE STATUS MATTERS, NOT ONLY TO `git push`, and reading
#   it as a rule about the push is how this script shipped with the bug it documents (bh-dt2d9):
#   the two VERIFICATION reads were written as `git ls-remote … | awk '{print $1}'`, which threw
#   away ls-remote's status the same way. A transient network failure then produced an empty
#   `after`, which fell into the "the ref did not move" branch and — with git's own rc at 0 —
#   printed "git exited 0 AND THE REMOTE DID NOT MOVE. That combination should be impossible."
#   A confident false statement, from the one command whose whole purpose is not to make those.
#   `set -o pipefail` (set below) does not save you: it fixes the PIPELINE's status, and nothing
#   was reading the status. So `remote_sha` below runs ls-remote unpiped and returns its rc, and
#   "could not verify" is its own branch with its own message — see the three-way split at the
#   bottom. Never fold "I could not look" into "I looked and it had not moved".
#
# Usage:  scripts/push-main.sh [remote] [branch]     (defaults: origin main)
#
# Exit codes:  0 = landed and verified · 3 = COULD NOT VERIFY (ls-remote failed) · anything
# else = git's own status from the push.
set -uo pipefail

#: The verification read. Echoes the ref's sha on the remote ("" when the ref does not exist yet,
#: which is a legitimate answer and not a failure) and returns `git ls-remote`'s OWN exit status.
#: No pipe: parsing happens in the shell after the status has been captured (bh-dt2d9).
remote_sha() {
    local out rc first
    out=$(git ls-remote "$1" "$2")
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        return "${rc}"
    fi
    first=${out%%$'\n'*}          # first line
    printf '%s' "${first%%[[:space:]]*}"   # its first field — the sha
    return 0
}

VERIFY_FAILED=3

REMOTE=${1:-origin}
BRANCH=${2:-main}

# Respect an operator who already set one; only ADD the keepalive when nothing is configured.
# Precedence matters here the same way it does for `run.child_env`: a deliberately-set value must
# never be silently overwritten by a default.
if [ -z "${GIT_SSH_COMMAND:-}" ]; then
    export GIT_SSH_COMMAND="ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=10"
    echo "→ keepalive: GIT_SSH_COMMAND=${GIT_SSH_COMMAND}" >&2
    echo "  (the gate outlives GitHub's idle timeout; without this a GREEN gate can still not" >&2
    echo "   push — bh-53o8f. See this script's header.)" >&2
else
    echo "→ keepalive: GIT_SSH_COMMAND already set by the environment, leaving it alone:" >&2
    echo "    ${GIT_SSH_COMMAND}" >&2
    case "${GIT_SSH_COMMAND}" in
        *ServerAliveInterval*) ;;
        *)
            echo "  ⚠ it carries no ServerAliveInterval. A ~390s gate can outlive GitHub's" >&2
            echo "    idle timeout and SIGPIPE after every test went green (bh-53o8f)." >&2
            ;;
    esac
fi

local_sha=$(git rev-parse "refs/heads/${BRANCH}") || exit 2
before=$(remote_sha "${REMOTE}" "refs/heads/${BRANCH}")
if [ $? -ne 0 ]; then
    # Not fatal HERE — the push is about to try the same remote and will report it far more
    # informatively. But it must not be recorded as "the remote was at nothing": the after-check
    # compares against this value, and "" is a legitimate answer meaning "the ref does not exist
    # yet". Unknown and empty are different facts.
    echo "⚠ could not read ${REMOTE} before pushing (git ls-remote failed) — the 'remote is at'" >&2
    echo "  below is UNKNOWN, not empty." >&2
    before=""
fi
before_short=${before:0:12}
before_short=${before_short:-<unknown>}
echo "→ pushing ${BRANCH} ${local_sha:0:12} to ${REMOTE} (remote is at ${before_short})" >&2
echo "  the pre-push gate runs the full suite — several minutes. Do not background or poll it." >&2

# NOT piped. See THE PIPE TRAP above.
git push "${REMOTE}" "${BRANCH}"
rc=$?

# THREE OUTCOMES, NOT TWO. "I could not look" is its own answer and gets its own branch —
# folding it into "I looked and it had not moved" is bh-dt2d9, and produced the script's most
# confident wrong sentence.
after=$(remote_sha "${REMOTE}" "refs/heads/${BRANCH}")
ls_rc=$?
# NB the status is captured on its own line, not read inside `if ! cmd; then` — there `$?` is the
# status of the NEGATION (always 0), which would report every verification failure as "exit 0".
if [ "${ls_rc}" -ne 0 ]; then
    echo "" >&2
    echo "✗ COULD NOT VERIFY whether the push landed: \`git ls-remote ${REMOTE}\` failed" >&2
    echo "  (exit ${ls_rc}). This is NOT 'the push failed' and NOT 'the push succeeded' —" >&2
    echo "  git's own push exited ${rc}, and the remote could not be read to confirm it." >&2
    echo "  Check by hand before concluding anything:" >&2
    echo "      git ls-remote ${REMOTE} refs/heads/${BRANCH}" >&2
    exit "${VERIFY_FAILED}"
fi

if [ "${after}" = "${local_sha}" ]; then
    if [ "${rc}" -ne 0 ]; then
        # The ref moved but git still reported failure — a second ref in the same push failed,
        # or the transport died after this one landed. Say both facts; do not pick one.
        echo "⚠ ${BRANCH} IS at ${local_sha:0:12} on ${REMOTE}, but git exited ${rc}." >&2
        echo "  Something else in the same push failed. Read git's output above before retrying." >&2
        exit "${rc}"
    fi
    echo "✓ ${REMOTE}/${BRANCH} is at ${local_sha:0:12} — verified with ls-remote against the" >&2
    echo "  actual remote, not the local remote-tracking ref." >&2
    exit 0
fi

# The ref did NOT move. Which of the two failures was it?
echo "" >&2
echo "✗ THE PUSH DID NOT LAND. ${REMOTE}/${BRANCH} is still at ${before_short}," >&2
echo "  not the ${local_sha:0:12} this push was for. Verified with ls-remote." >&2
if [ "${rc}" -eq 141 ]; then
    echo "" >&2
    echo "  EXIT 141 = 128+13 = SIGPIPE: git wrote to a socket the remote had already closed." >&2
    echo "  THE GATE ABOVE PASSED. Nothing is wrong with the code — this is bh-53o8f, the" >&2
    echo "  transport dropping while a ~390s hook held the connection idle. Do NOT reach for" >&2
    echo "  --no-verify (bh-njdxk: 'once that becomes habit the gate is gone'). Retry this" >&2
    echo "  script; the keepalive above is what stops it recurring." >&2
elif [ "${rc}" -eq 0 ]; then
    echo "" >&2
    echo "  git exited 0 AND THE REMOTE DID NOT MOVE. That combination should be impossible;" >&2
    echo "  if a wrapper piped this push, the 0 is the PIPELINE's status, not git's. File it." >&2
else
    echo "" >&2
    echo "  git exited ${rc}. A non-zero gate exit means the SUITE failed — read its output" >&2
    echo "  above, fix the code, and push again. That is a different failure from the" >&2
    echo "  green-gate-then-SIGPIPE case (exit 141) this script also reports." >&2
fi
exit "${rc:-1}"
