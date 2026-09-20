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
>
> **Amended 2026-09-19** by
> [Amendment 1](#amendment-1--key-scoped-verdict-transfer-proven-by-build-graph-impact) (epic
> bh-1j3ei): one *key's* verdict may carry from one tree to another when a build-graph resolver
> receipt proves none of that key's inputs changed. The whole-tree rules below stand.

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

Amendment 1 adds a fifth row, *same key input closure, proven by a resolver receipt*. It is
scoped to one key, not the tree, and it does not soften either **Never** row above.

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

## Release orchestration — the attestation as the bump's pre-flight proof (bh-ku9n9.7)

The same pattern one level up, and the place it pays most. `cz bump` writes `pyproject.toml`,
`CHANGELOG.md` and `uv.lock`, so **the release commit is a new tree with no attestation** — a
guaranteed miss on the phase above, at precisely the moment a ~371s gate inside a push hurts most.
bh-1owpi named this hole and did not solve it; it is what bit the 0.11.5 release.

**The ordering is the design, not an implementation detail.** bh-67utw's rule is that a failed
push is undoable to a clean slate *if and only if the tag never left*. The bump is therefore the
last safely reversible moment, so green is proven **before** it — never inside the push, where a
red suite is discovered only after a tag already exists locally. Pushing main becomes a short
check that green was already proven, which is also what removes the long idle socket of bh-53o8f.

```text
  bh release preflight   ── reads a verdict for HEAD's tree. REFUSES the bump without one.
                            Never runs the gate: a second place green gets established is a
                            second place it gets established too late.
  cz bump                ── new tree (pyproject + CHANGELOG + uv.lock) + tag. UNATTESTED.
  bh release attest      ── fires the gate on THAT tree, detached, now.
    --background
  bh release await       ── the push blocks here. GREEN ⇒ go. RED ⇒ refuse; nothing has left,
                            so bh-67utw's undo is still fully available.
  push --atomic          ── main AND its tag, both refs or neither.
 ══════════════════════════ THE TAG IS THE POINT OF NO RETURN ══════════════════════════
  bh release recover     ── which of bh-67utw's two cases is this? Decided on ONE MEASURED
                            FACT: did the tag reach the remote, per `ls-remote` against the
                            actual remote. Never a local tracking ref, never an assumption.
```

Four answers from `recover`, and the fourth is why it returns a code rather than a boolean:
**tag published** → case B, roll forward, never delete; **tag absent and the bump nowhere on the
remote** → case A, the undo is safe (the rewrite itself stays bh-67utw's); **main landed without
its tag** → bh-zfvbp's half-done release, finish it, do not undo it — unreachable via the atomic
push and measured for anyway; **could not read the remote** → conclude *nothing*, because a
history rewrite on a network blip is the one outcome worse than doing nothing.

The safety property is bh-ku9n9.5's, unchanged and enforced by sharing its resolver
(`prepush.push_main_cmd`): no missing, stale, red, corrupt or ambiguous attestation lets a bump
or a push proceed as though green were proven, and no flag converts a refusal into a pass.

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
  about git history. **What RUNS that set is `work.always_run`** (bh-ehmd8): `green_verdict` —
  the one question every reuse boundary asks — spawns the hive's declared command before it
  hands back a hit, so a hit means "skip the expensive command, still run the small set" rather
  than "skip everything". A failing set refuses the hit *and* seals the ledger for the rest of
  the process, so that outcome can never become an attestation. Declaring nothing honors the hit
  whole, which is the tier contract's degradation, not a gap.
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

## Amendment 1 — key-scoped verdict transfer, proven by build-graph impact

**Date:** 2026-09-19 · **Amends:** "Four kinds of the same" (the equality matrix), and through
it **Decision 4**. **Status:** decided. The operator gave the GO on 2026-09-19 (epic
bh-1j3ei). The detailed rules below (no chaining, age preservation, green-only carry, and the
per-key assembly in Decision 4) were settled in the bh-1j3ei.1 review. Replans bh-uz03f and
folds in bh-ehusd. **No code carries a verdict across trees until this amendment is on main.**

### Why the ADR needs a new row

A docs-only change pays for the whole gate. Landing bh-hhpuo (two markdown files, no code) ran
`just check-all` twice, 8+ minutes each, because the verdict is all-or-nothing by tree: any byte
changed anywhere invalidates every check. Epic bh-1j3ei splits the gate into named **attest
keys**. Each key is an opaque command with a `required` / `optional` policy and a
backend-specific **selector**. An **impact resolver** maps `(base tree, head tree)` to the set of
keys whose inputs changed, and writes a **receipt** that records how it knows.

Carrying key K's green verdict from tree T0 to tree T1 because a receipt proves none of K's
inputs changed is a transfer the matrix above does not allow. T0 and T1 are different trees, so
Decision 4's "exact tree match only" would refuse it. This amendment adds that transfer as a
new, narrower row. It does not reinterpret the existing rows.

### The new equality level

| Relationship | Content | History | Combination tested | Transfers a verdict? |
|---|---|---|---|---|
| Same key input closure, proven by a resolver receipt | Y *for K's inputs*, N for the tree | N | Y *for K* | **Transfers K only** |

The claim is narrow. Every file key K's command can read is byte-identical between T0 and T1,
and a build graph that is complete and enforced says so in a receipt. It transfers **one key's
verdict**, never the tree's verdict and never another key's.

**Why this is not the rejected "same subtree hash" row.** A subtree hash is a claim about a
*path*: "nothing under `src/` changed." It says nothing about what a command reads outside that
path, and nothing checks the claim. Measured on 2026-09-19, 74 test files read `docs/` at
runtime, so "docs cannot affect the tests" is false in this repo. A `when: ["**/*.md"]` glob
(bh-uz03f's replaced design) makes the same unproven path claim, and so does the existing
`scripts/pants_routes.py` `classify()` suffix shortcut. The new row differs on three counts:

1. **It is derived, not asserted.** The input set comes from the build graph's ownership and
   transitive dependents, not from a glob a person wrote.
2. **It is complete, or it does not apply.** A changed path the graph cannot attribute fails
   closed (rule 1 below). "No target matched" means *unowned*, never *safe*.
3. **It is enforced, or it does not apply.** A declared dependency is trustworthy only if
   something fails when it is missing. A key counts as graph-proven only once its units have
   passed in a sandbox that exposes nothing but declared inputs (rule 4 below).

"Same subtree hash" stays **Never**. So does "same patch, new base".

### The five fail-closed rules (every backend)

A receipt may list a key as *unaffected* only if none of these fire. Each rule can only
*invalidate*; none can ever mark a key unaffected.

1. **Unowned paths invalidate everything.** Any changed path the backend cannot attribute to an
   owning unit invalidates **all** keys. Deleted paths count, and are attributed through the
   **base** tree.
2. **Global inputs invalidate everything.** Any changed path among the backend's global build
   inputs invalidates **all** keys. For Pants in this repo: `pants.toml`, `BUILD` files,
   lockfiles, `pyproject.toml`, `uv.lock`, `justfile`, `.mise.toml`, `scripts/hermetic.sh`.
   Globs may appear **only** here, and only to **add** invalidation, never to remove it.
3. **Resolver failure falls back to full.** Any resolver error, timeout, or backend-version
   mismatch falls back to `native-full` (every key invalidated) and sets `fallback_reason` on
   the receipt. A resolver that cannot answer never produces a partial answer.
4. **Unproven keys depend on everything.** A key whose selected units are not yet proven (for
   Pants, its tests have not passed in the sandbox with declared inputs) is invalidated by any
   change. So is a key that has no selector for the active backend. Correctness never rests on an
   unenforced declaration.
5. **The git-metadata class never carries.** The "asterisk" class above (tests that read commit
   history, tags, `git describe`, commit counts) has no file inputs for a graph to prove
   unchanged. It never carries forward. `work.always_run` still runs on every reuse, carried or
   exact, as it does today. A key whose command reads git metadata has no selector, so rule 4
   invalidates it on every change.

The default backend is **`native-full`**, which invalidates every key on every change. It
reproduces today's all-or-nothing behavior exactly. The speedup is opt-in per hive
(`work.attest.impact.backend`), and switching it off restores this ADR's original behavior
byte for byte.

### What a carried verdict records, and why it never chains

A **carried** verdict for key K at tree T1 records at least:

- **source tree:** T0, the tree where K last *ran* green;
- **receipt digest:** the content hash of the `ImpactReceipt` whose `base_tree` is T0, whose
  `head_tree` is T1, which lists K as unaffected, and whose `fallback_reason` is empty;
- **backend and backend version:** the resolver that produced the receipt.

**Carrying never chains through an unproven hop.** A carry's source is always a **current**
verdict, meaning one K actually earned by running on its source tree. A carried verdict is
never itself a source. To carry K onward to T2, the resolver diffs T0 against T2 directly and
must prove K unaffected across that whole span. It does not compose the T0→T1 and T1→T2
receipts. If T0's verdict has aged out, there is nothing left to carry from.

A carry also preserves age. The carried verdict's timestamp is the source run's, so Decision 3's
TTL runs from when K last actually ran, and carrying never refreshes it. **Only green carries.**
A red verdict is never carried, and a red key re-runs.

### Per-key verdict states: current, carried, absent

From bh-ehusd, following beadhive-app's `bh-app-av1o` receipt (current / drift / absent), every
key at a given tree is in exactly one of three states, and they stay distinct:

| State | Meaning |
|---|---|
| **current** | K ran on this exact tree. A fresh `(tree, cmd_hash)` record, green or red. |
| **carried** | K did not run here. A green current verdict on a source tree was carried by a receipt as above. |
| **absent** | Neither. K was invalidated and not yet run, never ran, or ran and returned the "unknown" exit code 75. |

"Absent" keeps its reason. "Invalidated, not yet run" must stay distinguishable from "ran, came
back unknown", even though neither blocks an optional key.

The gate reads those states against each key's policy (these semantics are settled by bh-uz03f):

| Key policy | current green | carried | absent | current red (real nonzero, not 75) |
|---|---|---|---|---|
| `required` (default) | pass | pass | **block** | **block** |
| `optional` | pass | pass | pass | **block** |

`optional` never ignores a real failure.

### Keys stay opaque commands, so Options A and C stay rejected

A key's `cmd` is run verbatim, exactly as `validate_cmd` is today. bh never parses it, never
injects a flag, never probes for a test framework, and never learns what a key "means". The
selector is backend data (a Pants tag, a Turborepo task name, a Bazel tag) that the **resolver**
reads; the runner never reads it. The key catalog is therefore *which commands exist and which
graph units vouch for them*, not a per-hive results or runner configuration.
[`attested-green-provider-adr.md`](attested-green-provider-adr.md)'s NO-GO on **Option A** (a
registered test-runner shim) and its rejection of **Option C** (per-hive results config) as the
answer both stand unchanged. The ledger's `(tree, cmd_hash)` slot needs no schema change for
current verdicts, and a carried record is stored as its own kind so it can never pass for a
current one.

### One contract, three backend shapes

The port is build-system-agnostic: `ImpactResolver.resolve(repo, base_rev, head_rev, keys) ->
ImpactReceipt`. The receipt carries backend and version, both trees, changed / unowned paths,
global inputs hit, invalidated / unaffected keys, per-key evidence (backend unit ids),
`fallback_reason`, elapsed time, and its own digest. A backend only has to answer three
questions: *who owns each changed path*, *what transitively depends on those owners*, and
*which keys select those dependents*. The five rules apply unchanged whichever backend answers
them. Pants is the backend bh-1j3ei implements. The other two are recorded here to show the
contract's shape, not as commitments:

| Backend | Affected units | Units → keys | Global inputs (rule 2) | Proven (rule 4) |
|---|---|---|---|---|
| **Pants** (implemented) | `pants --changed-since=<base> --changed-dependents=transitive peek`; owners via `peek` / `filedeps` | target tag `attest:<name>` | `pants.toml`, `BUILD`, lockfiles, and the list in rule 2 | test target passed under `pants test`'s sandbox with declared inputs |
| **Turborepo** (documented) | `turbo run <tasks> --filter=...[<base>] --dry=json` | task name | `globalDependencies` | task declares `inputs` and ran with them enforced |
| **Bazel** (documented) | `rdeps(//..., set(<changed files>))` | intersect with `attr(tags, "attest:<name>", //...)` | `MODULE.bazel` / `WORKSPACE`, `.bazelrc`, toolchains | sandboxed actions (Bazel's default) |

A backend that cannot answer one of the three questions for a changed path hits rule 1 or rule 3
and degrades to `native-full`. None of them can degrade to a pass.

### Decision 4, extended

Decision 4 let landing boundaries reuse a verdict on exact tree match only. With this
amendment, the verdict for the exact tree being landed, pushed, or bumped may be **assembled
per key**: every required key is current or carried *at that exact tree*, and no key is current
red. The exact-tree condition still governs the tree being landed. What changes is only how one
key's green reaches that tree. A rebase, a near-miss, or a subtree match still runs fresh. The
`push-main` safety property is unchanged and one-directional: exit 0 only when every required
key is green (current or carried) for the exact tree being pushed. Every other outcome,
including a resolver fallback, runs the full gate inline.

### What stands unchanged

- **The load-bearing choice.** Verdicts are keyed on the tree, not the commit, and a current
  verdict's slot stays `(tree, cmd_hash)`.
- **Decision 1.** The confirming run is mandatory. A carry is not a converged result. It
  transfers a verdict that one clean, confirming run of K already earned, and never launders a
  retry into green.
- **Decision 2.** Tree equality is still sufficient evidence, and an exact tree match still
  reuses without any receipt.
- **Decision 3.** TTL is global, ISO-8601, and defaults to P1D. Carrying never extends it.
- **Decision 5.** The matrix is still the starting shape. This amendment is the evolution
  Decision 5 anticipated, and the two **Never** rows are unchanged.
- **Decision 6.** Cross-host attestation stays out of scope. Receipts and carried verdicts are
  host-local like the rest of the ledger (bh-1ha1o).
- **The trust model.** A receipt is a proof of correct invalidation, not an authorization. A
  carried record gets the same host-local integrity check as a current one, and it defends
  against the same things: corruption and accidental reuse, not intent.
- **The git-metadata asterisk and `work.always_run`.** Both are unchanged, and rule 5 keeps them
  out of every carry.
- **The provider ADR.** Option B stands. Options A and C stay rejected.

### What this amendment does not decide

The receipt's serialized form, the carried-record ledger shape, and the key catalog's config
keys belong to bh-1j3ei.3 and bh-1j3ei.6. Which checks become which keys in this repo belongs
to bh-1j3ei.5. The Pants backend itself is bh-1j3ei.7. Making the repo-wide dependents graph
resolve (today it fails with `UnownedDependencyError` on `cryptography`) is bh-1j3ei.2. Until
those land, every hive runs `native-full`, and this ADR behaves exactly as it did before the
amendment.
