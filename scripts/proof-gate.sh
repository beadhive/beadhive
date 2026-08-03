#!/usr/bin/env bash
# The proof gate (bh-pc2a.17): does a locally-baked image work with every bundled component
# TOGETHER?
#
# WHY THIS EXISTS AS A SCRIPT. The gate's design asked for "a shell script plus that matrix, not
# a test framework". The matrix alone is a transcription of someone's session — nobody can re-run
# it, and a stale row looks identical to a fresh one. This makes the gate reproducible: you run
# it, it reports, and docs/proof/bh-pc2a.17-image-proof.md is the record of a run rather than a
# claim about one.
#
# Scope is the COMBINATION. A component reporting --version proves nothing about whether bh can
# drive it, so the layers below exercise couplings, not presence.
#
# CREDENTIALS. Layers 4 and 5 need real ones. Absent them the layer SKIPS — loudly, and counted
# separately from a pass, because a gate that reports "green" while silently skipping half its
# checks is worse than no gate. Supply them to get full coverage:
#
#     GH_TOKEN=...            layer 4 (git/gh/git-workspace against a real remote)
#     BH_GATE_REPO=owner/repo layer 2-green — a repo you OWN; furnishing COMMITS to it
#     harness credentials     layer 5 — see docs/CONTAINER.md
#
# Usage:  scripts/proof-gate.sh [image-ref]        (default: beadhive/agent:dev)
# Exit:   0 all runnable layers passed · 1 a layer FAILED · 2 nothing to test (no image)
set -uo pipefail

IMAGE=${1:-beadhive/agent:dev}
PASS=0; FAIL=0; SKIP=0
RESULTS=()

ok()   { RESULTS+=("PASS  $1"); PASS=$((PASS+1)); printf '  ✓ %s\n' "$1"; }
bad()  { RESULTS+=("FAIL  $1"); FAIL=$((FAIL+1)); printf '  ✗ %s\n' "$1"; }
skip() { RESULTS+=("SKIP  $1 — $2"); SKIP=$((SKIP+1)); printf '  – %s (skipped: %s)\n' "$1" "$2"; }
layer() { printf '\n=== layer %s ===\n' "$1"; }

# Run a command inside a throwaway container.
#
# GIT_WORKSPACE is set explicitly so a BARE run matches the deployed shape. Without it the image
# defaults the workspace root to ~/workspace, while compose mounts it at /workspace — so a gate
# written against one silently tests a different layout in the other. That mismatch is exactly
# what this line exists to remove; it is not a convenience.
#
# No token is ever placed on a command line: credentials arrive by `-e VAR` passthrough only.
inimg() {
    docker run --rm -e GIT_WORKSPACE=/workspace --entrypoint bash "$IMAGE" -lc "$1" 2>&1
}

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "proof-gate: no image '$IMAGE' — bake one first: just image-local" >&2
    exit 2
fi

echo "proof gate — $IMAGE"
inimg 'echo "  bh $(bh --version)  ·  manifest $(jq -r .image.build_sha /etc/beadhive/image-manifest.json | cut -c1-12)"'

# ---- layer 1: components vs the manifest ---------------------------------------------------
# The manifest is what in-image `bh setup check` trusts INSTEAD of probing, so a component listed
# but absent is a lie the check structurally cannot catch. Assert the manifest against reality.
layer "1 — components cross-checked against the manifest"
if inimg 'bh config init >/dev/null 2>&1; bh setup check' | grep -q '✓ setup complete'; then
    ok "bh setup check green off the manifest"
else
    bad "bh setup check"
fi
missing=$(inimg '
  for c in $(jq -r ".components[].name" /etc/beadhive/image-manifest.json); do
      case "$c" in python) b=python3 ;; *) b="$c" ;; esac
      command -v "$b" >/dev/null 2>&1 || echo "$c"
  done')
if [ -z "$missing" ]; then
    ok "every manifest component resolves on PATH"
else
    bad "manifest lists components that are NOT present: $(echo "$missing" | tr '\n' ' ')"
fi

# ---- layer 2: bh drives bd -----------------------------------------------------------------
# The combination that historically breaks. The remote-less half needs no credentials and proves
# the coupling; `hive ready` GREEN additionally needs a repo you own, because furnishing commits.
layer "2 — bh drives bd (config init → onboard → bd live)"
out=$(inimg '
  set -e
  bh config init >/dev/null 2>&1; bh setup check >/dev/null 2>&1
  R=/workspace/gh/probe/throwaway; mkdir -p "$R"; cd "$R"
  git init -q -b main; git config user.email p@localhost; git config user.name p
  echo x > README.md; git add -A; git commit -qm init
  bh hive onboard gh/probe/throwaway --yes >/dev/null 2>&1
  BH_BD_PASS_ENABLED=1 bh bd create --title="gate canary" --type=task --priority=2 \
      --description="proves bd is live under the hive" >/dev/null 2>&1
  cd "$R" && bh work list 2>&1 | tail -3
  find / -name ".dolt" -type d -path "*embeddeddolt*" 2>/dev/null | head -2')
if grep -q "embeddeddolt" <<<"$out"; then
    ok "onboard ran, bd is live, dolt store created"
else
    bad "bh could not drive bd end to end"
fi

if [ -n "${BH_GATE_REPO:-}" ] && [ -n "${GH_TOKEN:-}" ]; then
    if inimg "bh config init >/dev/null 2>&1; bh setup check >/dev/null 2>&1;
              bh hive onboard gh/${BH_GATE_REPO} --clone-url https://github.com/${BH_GATE_REPO} --yes --claude >/dev/null 2>&1;
              cd /workspace/gh/${BH_GATE_REPO} && bh hive ready" | grep -q "ready for AGF"; then
        ok "bh hive ready GREEN against an owned repo"
    else
        bad "bh hive ready did not go green against ${BH_GATE_REPO}"
    fi
else
    skip "bh hive ready green" "needs BH_GATE_REPO (a repo you own) + GH_TOKEN; furnishing COMMITS"
fi

# ---- layer 3: dolt store placement + survival ----------------------------------------------
# Needs compose, not a bare container: the point is that NAMED VOLUMES carry state across a
# recreate. Run from the repo root so compose finds its file.
layer "3 — dolt store on the intended volume, surviving down && up"
if command -v docker >/dev/null && [ -f docker-compose.yml ]; then
    docker compose up -d >/dev/null 2>&1
    docker compose exec -T bh bash -lc '
        bh config init >/dev/null 2>&1; bh setup check >/dev/null 2>&1
        R=/workspace/gh/probe/gatecanary; mkdir -p "$R"; cd "$R"
        git init -q -b main; git config user.email p@localhost; git config user.name p
        echo x > README.md; git add -A; git commit -qm init
        bh hive onboard gh/probe/gatecanary --yes >/dev/null 2>&1
        BH_BD_PASS_ENABLED=1 bh bd create --title="survival canary" --type=task --priority=2 \
            --description="must survive a recreate" >/dev/null 2>&1' >/dev/null 2>&1
    docker compose down >/dev/null 2>&1
    docker compose up -d >/dev/null 2>&1
    if docker compose exec -T bh bash -lc \
        'cd /workspace/gh/probe/gatecanary 2>/dev/null && bh work list 2>&1' | grep -qi "survival canary"; then
        ok "bead read back OUT of dolt after down && up"
    else
        bad "durable state did not survive a recreate"
    fi
    if docker compose exec -T bh bash -lc \
        'findmnt -no SOURCE --target /home/$(whoami)/.beadhive' | grep -q "bh-hq"; then
        ok "BH_HOME resolves onto the bh-hq volume"
    else
        bad "BH_HOME is not on the bh-hq volume"
    fi
else
    skip "dolt survival" "run from the repo root, with docker available"
fi

# ---- layer 4: git + gh + git-workspace -----------------------------------------------------
# NEGATIVE CONTROL FIRST. A public-repo clone succeeds with no credential at all, so it proves
# nothing about the credential path — only a private repo does.
layer "4 — git + gh + git-workspace against a real remote"
if [ -n "${GH_TOKEN:-}" ]; then
    if docker run --rm -e GH_TOKEN --entrypoint bash "$IMAGE" -lc \
        'gh api rate_limit --jq ".resources.core.limit"' 2>/dev/null | grep -q 5000; then
        ok "gh authenticates from GH_TOKEN (limit 5000, not the unauthenticated 60)"
    else
        bad "gh did not authenticate from GH_TOKEN"
    fi
    if [ -n "${BH_GATE_PRIVATE_REPO:-}" ]; then
        neg=$(docker run --rm --entrypoint bash "$IMAGE" -lc \
            "git -c credential.helper= clone --depth 1 -q https://github.com/${BH_GATE_PRIVATE_REPO} /tmp/n" 2>&1)
        pos=$(docker run --rm -e GH_TOKEN --entrypoint bash "$IMAGE" -lc \
            "gh repo clone ${BH_GATE_PRIVATE_REPO} /tmp/p -- --depth 1 -q && echo CLONED" 2>&1)
        if grep -qi "could not read Username\|Authentication failed\|not found" <<<"$neg" \
           && grep -q CLONED <<<"$pos"; then
            ok "private clone refused without a token, succeeded with one"
        else
            bad "the credential path is not proven (negative control did not refuse, or clone failed)"
        fi
    else
        skip "private-repo clone" "set BH_GATE_PRIVATE_REPO — a PUBLIC repo proves nothing here"
    fi
else
    skip "gh / git / git-workspace" "no GH_TOKEN in the environment"
fi

# ---- layer 5: harness reachability ---------------------------------------------------------
# Restated by bh-pc2a.36: the harness is no longer baked, so this is install → authenticate →
# answer. Presence is not reachability and this script never conflates them.
layer "5 — harness reachability (install → authenticate → answer)"
if inimg 'command -v claude >/dev/null && echo yes' | grep -q yes; then
    skip "harness answer" "claude present, but an authenticated round-trip is not scripted here"
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ]; then
    skip "harness answer" "credentials present — run 'bh harness install claude' then verify by hand"
else
    skip "harness reachability" "no harness credentials; see docs/CONTAINER.md"
fi

# ---- layer 6: worktree lifecycle on the scratch volume -------------------------------------
# MUST run under compose, not a bare container. The claim is that worktrees land on the SCRATCH
# VOLUME, and a bare `docker run` has no volumes — `findmnt --target /worktrees` returns `overlay`
# there, so a bare run cannot evidence this either way. An earlier revision of this script did run
# it bare and reported a PASS, which was a false green: exactly the failure this gate exists to
# catch, in the gate itself.
layer "6 — worktree created and removed on the scratch volume"
if [ -f docker-compose.yml ] && docker compose ps --status running 2>/dev/null | grep -q bh; then
    wt=$(docker compose exec -T bh bash -lc '
      bh config init >/dev/null 2>&1; bh setup check >/dev/null 2>&1
      R=$GIT_WORKSPACE/gh/probe/wtgate; mkdir -p "$R"; cd "$R"
      git init -q -b main; git config user.email p@localhost; git config user.name p
      echo x > README.md; git add -A; git commit -qm init
      bh hive onboard gh/probe/wtgate --yes >/dev/null 2>&1
      cd "$R"; bh worktree add --branch gate-wt 2>&1 | tail -1
      echo "MOUNT:$(findmnt -no SOURCE --target /worktrees 2>/dev/null)"
      bh worktree rm gate-wt 2>&1 | tail -1' 2>&1)
    if grep -q "worktree ready" <<<"$wt" && grep -q "removed" <<<"$wt"; then
        ok "worktree created and removed"
    else
        bad "worktree lifecycle"
    fi
    if grep -q "^MOUNT:.*bh-worktrees" <<<"$wt"; then
        ok "worktrees land on the bh-worktrees volume"
    else
        bad "worktrees are NOT on the scratch volume (got: $(grep '^MOUNT:' <<<"$wt"))"
    fi
else
    skip "worktree lifecycle" "needs the compose stack up — the volume claim cannot be tested bare"
fi

# ---- verdict --------------------------------------------------------------------------------
printf '\n%s\n' "----------------------------------------------------------------"
printf '%s\n' "${RESULTS[@]}"
printf '\npassed %d · failed %d · skipped %d\n' "$PASS" "$FAIL" "$SKIP"

if [ "$FAIL" -gt 0 ]; then
    echo "✗ proof gate FAILED — the image is not proven." >&2
    exit 1
fi
if [ "$SKIP" -gt 0 ]; then
    echo "✓ every layer that could run passed — but $SKIP were SKIPPED, so the image is"
    echo "  PARTIALLY proven. Supply the credentials named above for full coverage."
else
    echo "✓ proof gate PASSED — every layer ran and passed."
fi
echo "  Record the result in docs/proof/bh-pc2a.17-image-proof.md."
