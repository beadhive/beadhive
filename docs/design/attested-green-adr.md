# Attested Green ADR — key the validation verdict on tree, not commit

> Status: **decided** (operator, 2026-08-15; epic bh-ku9n9). Re-keys the validation ledger
> bh-dfx0 shipped from `(commit sha, validate-cmd hash)` to `(tree hash, validate-cmd hash)`,
> and widens who may read it to include landing boundaries **on exact tree match only**. This
> is deliberately filed **after** bh-1owpi's five open questions (plus a sixth) settled — see
> that bead's design section "Decisions — settled" for the same six decisions in
> operator-framing. bh-1owpi's Q2 (what belongs in the env fingerprint) is **moot, not open**:
> per Decision 2, the environment is *established from the tree*, not fingerprinted, so there
> is no fingerprint to define — fingerprinting was rejected because it would require bh to hold
> permanent per-ecosystem environment knowledge, the same coupling this epic already rejects for
> test runners. Design doc:
> <https://claude.ai/code/artifact/a2eff74a-132b-4847-b505-2f4598eb0568>

## Context

Two failures share one missing object. A release push runs a ~371s gate *inside* the push and
can outlive GitHub's SSH idle timeout (bh-53o8f). A test suite has no memory: "did this pass
before, is this a regression or a flake" is answered by running the whole suite again. Both
exist because nothing in this repo can answer "is this tree green?" without re-establishing it
from scratch every time.

**This is not a green-field build.** `src/beadhive/validation_ledger.py` already ships a
verdict ledger (bh-dfx0, merged 2026-07-17): a small untracked JSON file at
`<hive>/.git/bh-validation-ledger.json`, entries keyed by **`(commit sha, cmd_hash)`** where
`cmd_hash` is `sha256(validate_cmd)[:16]`, each carrying `{sha, cmd_hash, rc, at, host}`, a
24-hour TTL (`LEDGER_TTL_SECONDS`), a 200-entry cap, and atomic tmp+rename writes that swallow
their own errors. It is written by every clean-checkout validation and by `work check` against
a clean worktree, and it is read back today by two opt-in callers: `work submit`, which always
reuses a recorded green verdict for the exact `(sha, cmd_hash)` key (`reuse=True`, `work.py:2124`)
instead of re-running a throwaway checkout; and `work review --run`, which reuses one only under
the non-default `--no-fresh` flag (`work_show.py:237`). Landing-boundary validations (merge /
postland / finish / batch land) **never** consult it — the module docstring states this is
deliberate, because "anything that can write the file can fake a green."

So this ADR is not "build a ledger." It is three changes to the one that shipped: re-key
`sha → tree`, record per-test outcomes (tracked separately — see `bh-8e1vn`), and widen who may
read it — specifically, let landing boundaries reuse a verdict, but only under a narrower
condition than the sha-keyed scheme could ever offer for free. That narrowing is the crux of
this ADR and is spelled out in Decision 4 below.

## The load-bearing choice: key on the tree, not the commit

A `--no-ff` merge onto an **unmoved** main produces a merge commit whose tree is byte-identical
to the branch tip's tree — there is nothing from main to incorporate. Key a verdict on the
*commit sha* and that byte-identical tree gets re-validated for 371s anyway, because the merge
commit is a new sha the ledger has never seen. Key on the *tree hash* instead and the land-time
run that already tested the branch tip covers the merge for free.

Two properties fall out without any hand-written invalidation rules:

- **Correct invalidation is automatic.** If main moved since the branch forked, the merge tree
  differs from both parents' trees, the lookup misses, and the gate runs — no bespoke "did
  anything relevant change" logic to get wrong.
- **The scheme is self-covering.** The justfile that defines `check-all` is itself a file *in*
  the tree, so editing it changes the tree hash and invalidates every prior attestation
  automatically. A verdict produced by a weaker gate recipe can never be honored by mistake.

This is why tree-keying is the load-bearing choice of the whole design: it is what turns a
land-time run into something a later push can legitimately reuse, rather than merely a faster
cache for identical retries.

## Four kinds of the same — what transfers a verdict

Not every notion of "the same code" is safe to transfer a verdict across. Four cases, in
decreasing strength:

| Relationship | Content | History | Combination tested | Transfers a verdict? |
|---|---|---|---|---|
| Same commit sha | Y | Y | Y | **Transfers** |
| Same tree hash, different commit | Y | N | Y | **Transfers\*** |
| Same patch, new base | N | N | N | **Never** |
| Same subtree hash | ~ | N | N | **Never** |

**Same commit sha** is the trivial case: identical object, already what bh-dfx0's ledger
transfers today.

**Same tree hash, different commit** is the new case this ADR adds. File content is identical
and the exact recipe/tool combination that produced the verdict was tested against that exact
content — but git *history* differs (different parents, different commit message, different
author). For anything that reads the working tree, this is a total, sound transfer.

**The asterisk — the git-metadata exception.** Same-tree/different-commit means identical
*file content* and *different git history*. That transfer is unsound for anything that reads
git **metadata** rather than file content: `git describe`, commit counts, tag-derived version
strings. This repo has that exposure via commitizen (`cz bump` derives the version from commit
history, not tree content). Any test that asserts on such metadata must never be gated behind
a tree-keyed cache hit — it belongs in the always-run set, because a tree hit says nothing
about the commit graph that produced it.

**Why this is not a third exception.** Verify-flagged init rules (`worktree_init` /
`worktrees.init`) run *inside* the verify checkout, against the checked-out tree, before the
validation command runs (`run_init(..., verify_only=True)`, `worktree.py:1294`; see also
`clean_checkout`'s docstring, `worktree.py:1268-1271`) — the hive declares `uv sync` / `cargo
fetch` / `npm ci` and bh spawns it without knowing what it means. Because the environment is
**established from the tree** rather than observed, Decision 2 needs no qualifier. Two things
still fall outside it: (a) ambient host/git state (the worktree's own git config, seat stamps,
environment variables) is fenced by `scripts/hermetic.sh` (all three phases route through it —
`justfile:319`, `:364`, `:410`); a test that still reaches it is a **defect to fix**, not a
permanent always-run category — bh-ku9n9.12 is the live instance, and bh-ab5e7 (under epic
bh-1c04h) is the structural fix. (b) The git-metadata class above genuinely is always-run — no
amount of provisioning makes `git describe` a function of the tree.

**Same patch, new base** and **same subtree hash** never transfer: neither the full combination
tested nor (for same-patch) the resulting content is known to match, so there is nothing sound
to reuse.

## Settled decisions

Six decisions, settled 2026-08-15 by the operator, each with its reasoning.

### 1. The confirming run is mandatory

No attestation is ever written from a converged result. "Re-run until green" is explicitly
**not** how a tree becomes attested.

*Why:* re-running only failures until they pass is exactly how a flaky suite gets laundered
into green. Measured in-session evidence: a merge went red on
`test_read_fleet_miss_computes_and_persists`, then the identical sha passed on a subsequent
full run of 4868 tests. Converging to a candidate cheaply is fine; only **one clean full run**
may write an attestation, and anything that passed only on retry must be recorded and surfaced
as flaky rather than quietly folded into the green verdict.

### 2. Tree equality is sufficient evidence

A merge commit is never tested as its own distinct object when its tree already attested.

*Why:* this is the direct consequence of the load-bearing choice above — a `--no-ff` merge onto
an unmoved main has a tree byte-identical to the branch tip's, so re-testing it would only
re-prove what the tip run already proved. Today's gate already only ever tests the pushed tip
(never re-derives intermediate commits' correctness independently), so honoring tree equality
for the merge commit is not a regression against current practice — it is stated here on
purpose rather than left implicit.

### 3. TTL is a global tunable ISO-8601 duration, default P1D

*Why:* this matches what bh-dfx0 already ships (`LEDGER_TTL_SECONDS = 24 * 60 * 60`) — a
re-expression of the existing default as a configurable ISO-8601 duration, not a behavior
change. The realistic reuse window is minutes-to-hours in practice; operators are expected to
tune the default **down**, not up. This supersedes bh-1owpi's own proposal of a ~7-day TTL: a
pass from three weeks — or even a week — ago on an identical tree is weaker evidence than one
from this morning, and a short TTL is cheap insurance against the environmental rot bh-njdxk
tracks (a green from before a `bd` upgrade is not evidence about after it).

### 4. Landing boundaries may reuse a verdict — on exact tree match only

*Why, and why this is safe despite bh-dfx0's refusal:* bh-dfx0's ledger module docstring states
plainly that landing-boundary validations (merge / postland / finish / batch land) **never**
consult the ledger, because "anything that can write the file can fake a green" and the gate at
landing needs to stay fresh. **This decision relaxes that blanket refusal.** It is safe to
relax *only* because tree-keying (Decision 2, and the load-bearing choice above) narrows the
condition from "trust a claimed verdict for this commit" to "trust a verdict earned by testing
the literal file content this landing operation would produce, byte-for-byte." An exact tree
match is not a claim about intent or provenance — it is a claim that the bytes under test are
identical to the bytes a previous, real, mandatory-confirming (Decision 1) run already
exercised. That is a strictly narrower and stronger condition than the sha-keyed ledger could
ever offer a landing boundary, which is why the relaxation is scoped to **exact tree match
only** — never a rebase, never a subtree, never a near-miss. Any of those still runs fresh,
exactly as bh-dfx0 requires today.

### 5. The strictness matrix is adopted as the starting shape

The four-row equality-levels table above (same commit / same tree / same patch new base / same
subtree) is adopted as the **starting** shape for what may and may not transfer a verdict, with
the expectation that it moves as real usage surfaces cases not yet accounted for.

*Why:* this is a new trust surface; committing to a matrix that is allowed to evolve is safer
than either over-fitting to today's known cases or leaving the rule unstated.

### 6. Cross-host attestations are out of scope for v1

Whether an attestation is ever shared — e.g. pushed as a git note so CI or another host can
skip its own run — is **not** decided here and is filed separately as backlog.

*Why:* a shared attestation turns a host-local cache into a trust claim across hosts, which
needs a real signing story rather than the local, host-scoped MAC this design uses. That is a
deliberately separate decision, not an oversight.

## Trust model — stated plainly

**This is a cache with correct invalidation. It is not an authorization boundary.**

The ledger file is local, and the key that will produce its integrity check (a MAC over the
record) is local. Anyone who wants to bypass it can forge a record, or — more simply — just
type `git push --no-verify`, which already works today regardless of anything this ADR adds.
The MAC defends against **corruption and accidental reuse** (a torn write, a copy-pasted record
applied to the wrong tree), not against **intent**. Relaxing landing boundaries to consult the
ledger (Decision 4) does not change this: it does not add a new bypass, because the bypass
(`--no-verify`, or a local actor editing the ledger file directly) already exists and already
works. Nobody should build a security assumption — access control, release sign-off,
provenance — on a green entry in this file. It answers exactly one question: "did a real,
confirming, full run already exercise this exact tree, recently enough to trust." It answers
nothing about who ran it, why, or whether they were authorized to.

## The pre-push gate is a named phase — `push-main` (bh-ku9n9.5)

The outermost and most expensive point was the only one outside the phase system: it `exec`d
`just check-all` from `scripts/main-push-gate.sh`, so it could reuse nothing and no
`work.validate.*` key described it. It is now the named phase **`push-main`** — a plain value in
the free-form map `config.validate_cmd` already reads (`[f"{phase}-main", phase]`), so this needs
no schema change — and the hook consults it through one verb, `bh hive hook push-main`, per
[`hooks-as-functionality-adr.md`](hooks-as-functionality-adr.md).

Point `molecule`, `merge-main` and `push-main` at the same command and the land-time run covers
the push for free: the `--no-ff` land produced a tree byte-identical to the one it tested, which
is the load-bearing choice above applied at the point that costs the most.

**The safety property is what makes this landable, and it is one-directional.** Exit 0 from that
verb means, only ever: a fresh green verdict exists for the exact tree being pushed, earned under
the exact command this gate would otherwise run. *Every* other outcome — miss, stale entry, red or
malformed record, unconfigured `push-main`, a `push-main` naming a different command than the hook
runs, no hive, unresolvable rev, no `bh` on `PATH`, any exception — is non-zero and runs the full
gate inline, unchanged. The worst case of consulting the ledger here is the behaviour that existed
before it did; no path treats a missing or unreadable attestation as a pass. This is also why the
hook file keeps the gate rather than delegating the whole job to a verb: a `bh` that is absent or
broken must degrade to "the gate runs", never to "main pushed ungated".

**What it does not solve.** Only the *hit* path is fast. A miss still runs the full ~371s gate
inside the push, on the connection git opened before the hook started — so the SSH keepalive from
bh-53o8f (`just push` / `scripts/push-main.sh`) is still required. This makes that path rarer; it
does not make it safe to run bare.

## Consequences

- `validation_ledger.py`'s key changes from `(sha, cmd_hash)` to `(tree, cmd_hash)`. Existing
  entries under the old key shape are not migrated — they simply age out under the existing
  TTL/cap eviction, since a old-shape entry does not match new-shape lookups.
- Landing-boundary callers (merge / postland / finish / batch land) gain a ledger read on exact
  tree match, narrowing but not removing bh-dfx0's original refusal — see Decision 4.
- Per-test outcome recording, the MAC record shape, and the `bh gate` verb surface are tracked
  as separate implementation beads under this epic (bh-ku9n9), not decided here.
- Any test reading git metadata (commit-derived version strings, `git describe`, commit counts)
  must stay in the always-run set per the git-metadata asterisk — tree equality says nothing
  about git history.
- Establish-from-tree (see "Why this is not a third exception" above) means a test reaching
  ambient host/git state that `scripts/hermetic.sh` should have fenced is a defect to fix, not
  a second always-run category — bh-ku9n9.12 is the live instance, bh-ab5e7 (epic bh-1c04h) is
  the structural fix.
- The ledger has two writers today and only one is sound: `clean_checkout` runs verify-flagged
  init rules against the tree first, so its environment derives from the tree; `work check` on
  a clean SEAT worktree also writes (`validation_ledger.py` module docstring, since bh-i0p1.4),
  but a seat's environment was provisioned whenever that seat was created — nothing re-derives
  it from the tree at check time. That is a known unsound writer, tracked separately
  (bh-ku9n9.14) and not this ADR's to solve.
- bh-1owpi is updated to point at this ADR instead of carrying the open questions itself.
- Who *produces* the per-test record, and the per-hive configuration surface this epic ships,
  are decided separately in [`attested-green-provider-adr.md`](attested-green-provider-adr.md)
  (bh-ku9n9.4): a built-in attestation provider in Python that never owns the run, one new
  operator-facing key (`work.validate_subset`), and zero configuration for machine-readable
  results.
