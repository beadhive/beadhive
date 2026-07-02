#!/usr/bin/env bash
# reconcile.sh — deterministic doc↔bead bridge recovery for the backfill Guide.
#
# Does ONLY the mechanical, deterministic part of a reconcile: for each source doc,
# recover the bead it belongs to via (1) a `Beads:` frontmatter line, then (2) the bead
# id in the commit that first added the doc. Joins against the rig's existing beads and
# classifies each into PRESENT-in-sync / PRESENT-needs-stamp / UNMATCHED. Judgment
# (fuzzy fallback, NEW-vs-noise, drift) stays with the agent — this tool never guesses
# and never writes unless you pass --apply.
#
# Sources:
#   default   — docs/decisions/ + docs/design/ : match docs to EXISTING beads (stamp external_ref)
#   --planning— .planning/phases/           : extract a NEW epic/issue/dep structure (GSD frontmatter)
#
# Usage:
#   reconcile.sh <rig-path> [--planning] [--prefix <p>]   # propose: print the table (TSV)
#   reconcile.sh <rig-path> [--planning] --apply           # stamp external_ref on PRESENT-needs-stamp
#   reconcile.sh <rig-path> [--planning] --verify          # exit 1 if any row is still pending
#
# Read-only by default. --apply runs `bd update` inside the rig (docs source only; NEW beads from
# --planning are created by the agent in the apply step). Requires git, jq, bd.
set -euo pipefail

# first `depends_on:` frontmatter value, normalized to a bracketed list. Handles BOTH inline
# (`depends_on: ["10-01"]` / `[]`) AND block style (`depends_on:` then `  - "01-01"` lines) — the
# block form previously read as empty and silently dropped a real edge (homelab 01-02→01-01).
# ponytail: frontmatter only; run `reconcile.sh --selftest` to exercise it.
deps_of() {
  awk '
    NR==1&&/^---/{f=1;next} f&&/^---/{exit}
    f&&/^depends_on:/{v=$0; sub(/^depends_on:[ ]*/,"",v); if(v!=""){print v; exit} b=1; next}
    f&&b&&/^[[:space:]]+-[[:space:]]/{it=$0; sub(/^[[:space:]]+-[[:space:]]*/,"",it); gsub(/[^A-Za-z0-9._-]/,"",it); if(it!=""){L=L (L?",":"") it}; next}
    f&&b&&/^[^[:space:]]/{b=0}
    END{ if(L!="") print "["L"]" }
  ' "$1"
}

selftest() {
  local tmp out fail=0
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
  # inline empty, inline quoted, inline bare, block quoted, block bare, none
  printf -- '---\ndepends_on: []\n---\n'                 > "$tmp/a"; [ "$(deps_of "$tmp/a")" = "[]" ]      || { echo "FAIL inline-empty: $(deps_of "$tmp/a")"; fail=1; }
  printf -- '---\ndepends_on: ["10-01"]\n---\n'          > "$tmp/b"; [ "$(deps_of "$tmp/b")" = '["10-01"]' ] || { echo "FAIL inline-quoted: $(deps_of "$tmp/b")"; fail=1; }
  printf -- '---\ndepends_on: [11-01]\n---\n'            > "$tmp/c"; [ "$(deps_of "$tmp/c")" = "[11-01]" ]  || { echo "FAIL inline-bare: $(deps_of "$tmp/c")"; fail=1; }
  printf -- '---\ndepends_on:\n  - "01-01"\nx: 1\n---\n' > "$tmp/d"; [ "$(deps_of "$tmp/d")" = "[01-01]" ]  || { echo "FAIL block-quoted: $(deps_of "$tmp/d")"; fail=1; }
  printf -- '---\ndepends_on:\n  - 01-01\n  - 01-02\n---\n' > "$tmp/e"; [ "$(deps_of "$tmp/e")" = "[01-01,01-02]" ] || { echo "FAIL block-multi: $(deps_of "$tmp/e")"; fail=1; }
  printf -- '---\nphase: 05\n---\n'                      > "$tmp/f"; [ -z "$(deps_of "$tmp/f")" ]           || { echo "FAIL none: $(deps_of "$tmp/f")"; fail=1; }
  [ "$fail" = 0 ] && echo "deps_of selftest: OK" || return 1
}

RIG=""; MODE="propose"; PREFIX=""; SOURCE="docs"; REFRESH=0; DOCSDIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)         MODE="apply" ;;
    --verify)        MODE="verify" ;;
    --planning)      SOURCE="planning" ;;
    --refresh-jsonl) REFRESH=1 ;;
    --selftest)      selftest; exit $? ;;
    --prefix)        PREFIX="$2"; shift ;;
    --docs)          DOCSDIR="$2"; shift ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  RIG="$1" ;;
  esac
  shift
done
[ -n "$RIG" ] && [ -d "$RIG/.git" ] || { echo "usage: reconcile.sh <rig-path> [--planning] [--docs <dir>] [--apply|--verify] [--refresh-jsonl]" >&2; exit 2; }
CANON="$RIG/.beads/issues.jsonl"
[ -f "$CANON" ] || { echo "no beads corpus at $CANON" >&2; exit 2; }

# bd writes to its embedded DB and does NOT auto-export, so the tracked issues.jsonl goes stale
# after any write — a read-after-write against it sees the pre-write state. So we read from a fresh
# `bd export` SNAPSHOT (temp, auto-deleted), never the tracked file: propose/verify stay strictly
# read-only on any rig regardless of auto-export config. Refreshing the TRACKED file is a mutation
# and is therefore opt-in (--refresh-jsonl) — the guide gates that on a human confirm when it finds
# the rig stale (auto-export off). See for making export automatic at rig-init.
JSONL="$CANON"
if command -v bd >/dev/null 2>&1; then
  SNAP="$(mktemp)"; trap 'rm -f "$SNAP"' EXIT
  if ( cd "$RIG" && bd export ) > "$SNAP" 2>/dev/null && [ -s "$SNAP" ]; then
    JSONL="$SNAP"                                  # authoritative read source
    [ "$REFRESH" = 1 ] && cp "$SNAP" "$CANON"      # opt-in: de-stale the tracked file for br
  fi
fi

# Bead id shape: <prefix>-<slug>(.N)*  — derive the modal prefix from the corpus if not given.
if [ -z "$PREFIX" ]; then
  PREFIX=$(jq -r '.id' "$JSONL" | sed -E 's/^([a-z][a-z0-9]*(-[a-z0-9]+)*)-[a-z0-9]+(\.[0-9]+)*$/\1/' \
           | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
fi
ID_RE="${PREFIX}-[a-z0-9]+(\.[0-9]+)*"

# doc → existing external_ref (empty if unset), keyed by bead id, from the canonical jsonl.
ref_of() { jq -r --arg id "$1" 'select(.id==$id) | .external_ref // ""' "$JSONL" | head -1; }
has_bead() { jq -e --arg id "$1" 'select(.id==$id)' "$JSONL" >/dev/null 2>&1; }
# id of a bead already carrying this external_ref (empty if none) — the re-run idempotency key.
bead_with_ref() { jq -r --arg r "$1" 'select(.external_ref==$r) | .id' "$JSONL" 2>/dev/null | head -1; }
# deps_of() is defined at the top of the file so --selftest can reach it before rig validation.

# Fuzzy shortlist for an UNMATCHED doc: the top existing content beads by shared title tokens.
# NARROWS, never decides — the agent picks a candidate or files NEW. ponytail: lexical overlap,
# not semantic; a bag-of-words heuristic, upgrade to embeddings only if a real corpus defeats it.
shortlist_for() {
  local q; q=$(basename "$1" .md | sed -E 's/^[0-9]+[-_]//; s/[-_]/ /g')
  jq -r 'select((.issue_type//"")|IN("gate","event","molecule")|not) | [.id,.title]|@tsv' "$JSONL" \
  | awk -F'\t' -v q="$q" '
      BEGIN{ n=split(q,qt," "); for(i=1;i<=n;i++){t=tolower(qt[i]); if(length(t)>3) Q[t]=1} }
      { low=tolower($2); gsub(/[^a-z0-9 ]/," ",low); m=split(low,tt," "); s=0; delete seen
        for(i=1;i<=m;i++) if((tt[i] in Q) && !(tt[i] in seen)){ s++; seen[tt[i]]=1 }
        if(s>0) printf "%d\t%s\t%s\n", s, $1, $2 }' \
  | sort -rn | head -3
}

# --- planning source: extract a NEW phase→epic / plan→issue / depends_on→dep structure ----------
planning_propose() {
  local pdir="$RIG/.planning/phases" new=0
  [ -d "$pdir" ] || { echo "no .planning/phases in $RIG" >&2; exit 0; }
  for phase in "$pdir"/*/; do
    [ -d "$phase" ] || continue
    local rel name plans epic_status existing
    rel="${phase#"$RIG"/}"; rel="${rel%/}"; name=$(basename "$phase")
    plans=$(ls "$phase"*-PLAN.md 2>/dev/null || true)
    # phase epic is closed only if every plan has a sibling SUMMARY (history says done).
    epic_status="closed"; [ -z "$plans" ] && epic_status="open"
    for pl in $plans; do [ -f "${pl%-PLAN.md}-SUMMARY.md" ] || epic_status="open"; done
    existing=$(bead_with_ref "$rel")
    if [ -n "$existing" ]; then
      [ "$MODE" != verify ] && printf 'PRESENT-in-sync\tepic\t%s\t%s\n' "$rel" "$existing"
    else
      new=$((new+1)); [ "$MODE" != verify ] && printf 'NEW-epic\t%s\tphase:%s\tstatus:%s\n' "$rel" "$name" "$epic_status"
    fi
    for pl in $plans; do
      local prel st deps ex
      prel="${pl#"$RIG"/}"
      [ -f "${pl%-PLAN.md}-SUMMARY.md" ] && st="closed" || st="open"
      deps=$(deps_of "$pl"); ex=$(bead_with_ref "$prel")
      if [ -n "$ex" ]; then
        [ "$MODE" != verify ] && printf 'PRESENT-in-sync\tissue\t%s\t%s\n' "$prel" "$ex"
      else
        new=$((new+1)); [ "$MODE" != verify ] && printf 'NEW-issue\t%s\tparent:%s\tstatus:%s\tdeps:%s\n' "$prel" "$name" "$st" "${deps:-[]}"
      fi
    done
  done
  if [ "$MODE" = verify ]; then
    [ "$new" -gt 0 ] && { echo "not idempotent: $new planning bead(s) not yet created" >&2; exit 1; }
    echo "idempotent: every planning phase/plan is filed"; exit 0
  fi
  exit 0
}
[ "$SOURCE" = planning ] && planning_propose

# Bridge recovery for one doc: echo "<bead-id>\t<bridge>" or nothing.
bridge_for() {
  local doc="$1" id bridge
  # 1. frontmatter back-ref: a line like "- Beads: obs-1rb (epic), obs-1rb.5"
  id=$(grep -iE '^[[:space:]]*-?[[:space:]]*beads?:' "$RIG/$doc" 2>/dev/null | head -1 \
       | grep -oE "$ID_RE" | tail -1 || true)
  if [ -n "$id" ]; then echo -e "$id\tfrontmatter"; return; fi
  # 2. bead id in the subject of the commit that ADDED the doc
  id=$(git -C "$RIG" log --follow --diff-filter=A --format='%s' -- "$doc" 2>/dev/null | head -1 \
       | grep -oE "$ID_RE" | head -1 || true)
  if [ -n "$id" ]; then echo -e "$id\tadd-trailer"; return; fi
  # unmatched — leave for the agent's fuzzy/judgment pass
}

# Doc set: an explicit --docs <dir> tree (any markdown, e.g. .planning/ on a rig that tracked work
# as prose), else the default ADR/design dirs. Same bridges apply either way — the recovery logic
# is doc-path-agnostic; --docs just widens which files feed it.
if [ -n "$DOCSDIR" ]; then
  DOCS=$(cd "$RIG" && find "$DOCSDIR" -type f -name '*.md' 2>/dev/null | sort || true)
  [ -n "$DOCS" ] || { echo "no *.md under $DOCSDIR in $RIG" >&2; exit 0; }
else
  DOCS=$(cd "$RIG" && ls docs/decisions/*.md docs/design/*.md 2>/dev/null || true)
  [ -n "$DOCS" ] || { echo "no docs/decisions or docs/design in $RIG" >&2; exit 0; }
fi

pending=0
for doc in $DOCS; do
  # Bridge 0 — reverse external_ref: a bead already points at this doc. This is the idempotency
  # key and covers EVERY prior match (deterministic and fuzzy alike), so a re-run after apply is a
  # clean no-op. Must come first.
  linked=$(bead_with_ref "$doc")
  if [ -n "$linked" ]; then
    [ "$MODE" = "propose" ] && printf 'PRESENT-in-sync\t%s\t%s\tref\n' "$doc" "$linked"
    continue
  fi
  read -r id bridge < <(bridge_for "$doc"; echo) || true
  if [ -z "${id:-}" ]; then
    if [ "$MODE" = "propose" ]; then
      printf 'UNMATCHED\t%s\t-\t(agent: pick a CANDIDATE below or file NEW)\n' "$doc"
      shortlist_for "$doc" | while IFS=$'\t' read -r score cand ctitle; do
        printf 'CANDIDATE\t%s\t%s\t%s (overlap %s)\n' "$doc" "$cand" "$ctitle" "$score"
      done
    fi
    continue
  fi
  has_bead "$id" || { [ "$MODE" = "propose" ] && printf 'DANGLING\t%s\t%s\t(ref names a bead not in corpus)\n' "$doc" "$id"; continue; }
  cur=$(ref_of "$id")
  if [ "$cur" = "$doc" ]; then
    status="PRESENT-in-sync"
  elif [ -n "$cur" ]; then
    status="DRIFTED-ref"     # bead already points somewhere else — agent decides
  else
    status="PRESENT-needs-stamp"; pending=$((pending+1))
  fi
  case "$MODE" in
    propose) printf '%s\t%s\t%s\t%s\n' "$status" "$doc" "$id" "$bridge" ;;
    apply)   if [ "$status" = "PRESENT-needs-stamp" ]; then
               (cd "$RIG" && bd update "$id" --external-ref "$doc") && printf 'stamped\t%s\t%s\n' "$id" "$doc"
             fi ;;
    verify)  [ "$status" = "PRESENT-needs-stamp" ] && printf 'PENDING\t%s\t%s\n' "$id" "$doc" ;;
  esac
done

if [ "$MODE" = "verify" ]; then
  if [ "$pending" -gt 0 ]; then echo "not idempotent: $pending stamp(s) still pending" >&2; exit 1; fi
  echo "idempotent: every doc-backed bead is linked"; exit 0
fi
