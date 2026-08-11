#!/usr/bin/env bash
# git pre-push: run the FULL gate (`just check-all`) when — and only when — the push updates
# the integration branch. This is the "wire at main-merge points" the justfile's `check-all`
# comment has always asked for and nothing ever did (bh-dfz2): before this, `check-all` was a
# recipe you had to remember to type, so eight integration tests rotted red on `main` unnoticed.
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
# Usage: main-push-gate.sh [integration-ref]   (default refs/heads/main); reads git's pre-push
# ref list on stdin, one "<local_ref> <local_sha> <remote_ref> <remote_sha>" per line.
#
# Bypass, when you genuinely must: `git push --no-verify` / `LEFTHOOK=0 git push`.
set -euo pipefail

target=${1:-refs/heads/main}
zero=0000000000000000000000000000000000000000

# `read -r || [ -n "$line" ]` so a final line without a trailing newline is still seen.
gate=0
while read -r _local_ref local_sha remote_ref _remote_sha || [ -n "${_local_ref:-}" ]; do
  # A deletion (local sha all zeros) pushes no tree to test — never gate it, or `git push
  # origin :main` would run an 11-minute suite to delete a branch.
  [ "$remote_ref" = "$target" ] && [ "$local_sha" != "$zero" ] && gate=1
done

[ "$gate" = 1 ] || exit 0

echo "→ $target push: running the full gate (just check-all) — unit + the real-bd integration
  suite, several minutes. Wait for it; do not background or poll it." >&2
exec just check-all
