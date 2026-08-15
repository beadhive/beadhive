#!/usr/bin/env bash
# git pre-push: run the FULL gate (`just check-all`) when — and only when — the push updates
# the integration branch, UNLESS bh already holds a green verdict for the exact tree being
# pushed. This is the "wire at main-merge points" the justfile's `check-all` comment has always
# asked for and nothing ever did (bh-dfz2): before this, `check-all` was a recipe you had to
# remember to type, so eight integration tests rotted red on `main` unnoticed.
#
# THE LOOKUP IS AN OPTIMISATION, NEVER A SUBSTITUTE (bh-ku9n9.5). One line asks
# `bh hive hook push-main` whether the pushed tree already passed this exact command recently;
# anything other than a clean yes runs the gate below, unchanged. A missing, stale, malformed
# or unreadable attestation — or no `bh` at all — can never mean "pass". See that block below.
#
# IT DOES NOT SOLVE bh-53o8f, and nobody should read it as having done so: the MISS path still
# runs ~371s inside the push, on a connection git opened before this hook started, so the SSH
# keepalive (`just push` / scripts/push-main.sh) is still required for any push that has not
# just been attested. It makes the expensive path rarer; it does not make it safe to run bare.
#
# Every route to the shared branch ends in a push of `refs/heads/main` from a host with the
# hooks installed — `bh work merge`'s local `--no-ff` land, `bh work finish`'s molecule bubble,
# or a hand push — so this one seam covers them all without a per-verb hook.
#
# BOTH CONDITIONS IN THAT SENTENCE ARE LOAD-BEARING, and both failed here (bh-4kq1b):
#   * "from a host with the hooks installed" — this repo's own clone had none. `just hooks`
#     invoked a bare `lefthook`, which is a mise tool and resolves only in an activated shell;
#     it exited 127 and left `.git/hooks/` holding nothing but git's `.sample` files.
#   * "ends in a push" — eventually, yes; before LANDING, no. `bh work merge` / `finish` land on
#     LOCAL main and the push is batched, so 44 commits sat on main unpushed and ungated while
#     four integration tests were red.
# So this is the belt, not the braces: the enforcing seam is the land itself, via
# `work.validate.molecule` / `.merge-main` on the hive's registry entry (see the justfile's
# `check-all`). Do not let a later edit re-promote this to "the" gate.
#
# WHY THE REMOTE REF AND NOT THE CURRENT BRANCH: lefthook's `only: {ref: main}` matches the
# CHECKED-OUT branch, which is the wrong question in both directions — measured 2026-08-05 on
# lefthook 2.1.10: pushing `HEAD:side` while sitting on main RUNS it (waste), and pushing
# `HEAD:main` from a side branch SKIPS it (the gate silently absent at the exact moment it is
# needed). git's own stdin protocol names the ref actually being updated; that is the only
# reliable signal. Multiple `use_stdin: true` jobs each receive the full list, so consuming it
# here does not starve the `bh-fence` job.
#
# A GREEN GATE IS NOT A LANDED PUSH, and this hook is the last thing anyone reads before
# concluding otherwise (bh-53o8f). git opened its connection to the remote BEFORE running this
# hook — it had to, the ref list on our stdin came down it — so the socket sits idle for the
# ~390s `just check-all` takes, and GitHub closes it. git then finishes a fully green gate and
# writes to a dead socket: exit 141 (SIGPIPE), "failed to push some refs", remote unchanged.
# Measured three times on 2026-08-12; one of those runs was reported as a successful push on the
# strength of this hook's own green output and was not caught until `git ls-remote`.
#
# So the last line this hook prints says the push has NOT happened yet, and `just push`
# (scripts/push-main.sh) sets the SSH keepalive that prevents the drop and then VERIFIES the
# remote actually moved. Use it rather than a bare `git push` for main.
#
# Usage: main-push-gate.sh [integration-ref]   (default refs/heads/main); reads git's pre-push
# ref list on stdin, one "<local_ref> <local_sha> <remote_ref> <remote_sha>" per line.
#
# Bypass, when you genuinely must: `git push --no-verify` / `LEFTHOOK=0 git push`.
set -euo pipefail

target=${1:-refs/heads/main}
zero=0000000000000000000000000000000000000000

# The one command this gate runs, named once: it is both what runs below and what
# `work.validate.push-main` must resolve to for a recorded verdict to be about THIS gate.
gate_cmd="just check-all"

# `read -r || [ -n "$line" ]` so a final line without a trailing newline is still seen.
# `gate` holds the SHA being pushed to $target (empty = not this push's business), because the
# attested-green lookup below needs the tree that sha names, not merely a yes/no.
gate=
while read -r _local_ref local_sha remote_ref _remote_sha || [ -n "${_local_ref:-}" ]; do
  # A deletion (local sha all zeros) pushes no tree to test — never gate it, or `git push
  # origin :main` would run an 11-minute suite to delete a branch.
  [ "$remote_ref" = "$target" ] && [ "$local_sha" != "$zero" ] && gate=$local_sha
done

[ -n "$gate" ] || exit 0

# ── THE ONLY THING BETWEEN HERE AND THE GATE, AND IT CAN ONLY EVER REMOVE WORK (bh-ku9n9.5) ──
# `push-main` is a named validation phase like every other point (`work.validate.push-main`),
# so a land-time run that already tested this exact TREE under this exact command counts. Exit
# 0 = that verdict exists, fresh and green, for this tree: nothing to re-prove, push in ms.
#
# EVERY OTHER OUTCOME IS NON-ZERO AND FALLS THROUGH TO THE FULL GATE BELOW — a miss, a stale or
# malformed record, an unconfigured/mismatched phase, no ledger, a `bh` that is not installed
# (127), a `bh` that crashes. That is the property that makes this safe to land: the worst case
# of consulting it is exactly the behaviour of the line not being here. There is deliberately
# no path in which a missing or unreadable attestation means "pass".
#
# The whole decision lives in the verb (docs/design/hooks-as-functionality-adr.md, bh-smcj) —
# tree resolution, TTL, phase lookup, exit semantics. This file states no policy about it and
# so cannot drift from bh's own notion of the gate; it just honours an exit code.
#
# WHAT THIS DOES NOT FIX: only the HIT path is fast. The gate below still takes ~371s inside
# the push, holding the connection git opened before this hook started — so the bh-53o8f SSH
# keepalive (`just push`) is STILL REQUIRED, and the warning right below still earns its place.
if ${BH_EXEC:-bh} hive hook push-main "$gate" --gate "$gate_cmd"; then
  exit 0
fi

echo "→ $target push: running the full gate ($gate_cmd) — unit + the real-bd integration
  suite, several minutes. Wait for it; do not background or poll it." >&2

case "${GIT_SSH_COMMAND:-}" in
  *ServerAliveInterval*) ;;
  *)
    echo "⚠ no SSH keepalive on this push (GIT_SSH_COMMAND carries no ServerAliveInterval)." >&2
    echo "  The connection git already opened will sit idle for the whole gate below, and that" >&2
    echo "  is where a green gate three times failed to push anyway (bh-53o8f). Cancel and use" >&2
    echo "  \`just push\`, or expect to verify with \`git ls-remote\` afterwards." >&2
    ;;
esac

# NOT `exec`: exec replaces this shell and the trailing banner never runs. The banner IS the
# point — it is the last thing printed before git writes to a connection that may have died
# during the gate, and without it "six minutes of green" is the final word an operator reads.
rc=0
$gate_cmd || rc=$?
if [ "$rc" -ne 0 ]; then
  echo "✗ gate FAILED (exit $rc) — nothing was pushed. Fix the suite, not the transport." >&2
  exit "$rc"
fi
echo "" >&2
echo "✓ gate GREEN — AND THE PUSH HAS NOT HAPPENED YET. git now writes to the connection it" >&2
echo "  opened before this hook started. If the next thing you see is 'failed to push some" >&2
echo "  refs' or exit 141 (SIGPIPE), THAT IS THE TRANSPORT, NOT THE CODE — the tests above" >&2
echo "  passed. Do not reach for --no-verify; retry with \`just push\`, which keeps the" >&2
echo "  connection alive and verifies the remote with ls-remote (bh-53o8f)." >&2
echo "  Verify either way:  git ls-remote origin $target" >&2
exit 0
