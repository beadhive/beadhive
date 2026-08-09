# Spike `bh-rwryq` — does bead-ID mention correlation yield enough to carry the overlay v1?

**Bead:** `bh-rwryq.1` / `.2` / `.3` · **Seat:** `dev/dispatcher-a` · **Type:** research-only (no
product code)
**Feeds decision on:** the "Bead Graph × Git History Overlay" proposal — whether overlay v1 can
render commit↔bead linkage from commit-message mentions alone, or must wait on durable linkage
(`metadata.git.commits`, epic `bh-1b0rc`).

## Question

Section 06 of the overlay proposal measured commit-message **hygiene**: 73% of beadhive's last
200 commits carry at least one `bh-XXXXX`-shaped mention, in two consistently tool-generated
shapes. Hygiene is not the number the overlay rests on. **Yield** is: how many of those mentions
survive a tightened matcher and actually **resolve** against the live bead store.

This spike answers one question: **across the three beadhive-family repos, what fraction of
commits map to at least one RESOLVABLE bead, and how noisy is the mapping?**

It is explicitly **not** asking whether durable linkage should exist — that is a separate epic
and is assumed desirable regardless. It is asking whether ID-mention correlation is good enough
to ship *first*, on its own.

**Pre-registered threshold** (fixed before the number was seen, so it cannot be rationalised
after the fact):

> **GO** requires at least **60%** of the last 500 commits mapping to at least one **resolvable**
> bead, with a post-tightening **false-positive rate under 5%**.

Two accuracy traps drove the design, both from section 09:

1. **False positives.** A loose `bh-[0-9a-z]+` pattern also matches `bh-infra`, `bh-version`, and
   every hive/branch/volume name of that shape. Left in, they permanently poison any staleness
   signal built on top.
2. **Legitimately unmappable commits.** `bump: version` commits and multi-bead batch merges have
   no single bead to point at *by construction*. Counting them as misses understates real yield,
   so they get their own bucket.

Out of scope, and not attempted: shelling to `bv --robot-history`. It is dead code for this
purpose on two independent grounds — its co-commit method reads a beads JSONL that git never
tracks, and its `ExplicitMatcher` patterns all require a numeric suffix (`PROJECT-123`), which
beadhive's base36-ish `bh-xxxxx` IDs never have.

## Method

**The matcher** is a two-step filter, not a bigger regex — the whole point of `bh-rwryq.1`:

1. `extract_candidates()` — a namespace-anchored, deliberately **loose** pattern over the full
   commit message (**subject AND body**, via `%s` + `%B`). It over-matches on purpose.
2. `resolve_candidates()` — an **existence check** against that repo's own live bead ID set.
   Anything that does not resolve is **dropped, not counted**.

Both live in
[`scripts/bead_commit_correlation.py`](../../scripts/bead_commit_correlation.py), marked
`CANONICAL`, so the durable-linkage backfill and beadhive-ui's correlator consume them verbatim
rather than reinventing a third regex.

**Hive scoping** is structural, not conventional. `load_live_ids()` runs `bh bd export --all`
with `cwd` inside the target repo and caches the result per repo path, so a 500-commit walk
issues **one** lookup and resolution is **never** aggregated across hives: a token found in repo
A is only ever checked against repo A's beads. `bh bd` is used rather than bare `bd` (hive-aware;
bare `bd` can hit the wrong database).

**Namespace derivation.** All three repos leave `issue-prefix` commented out in
`.beads/config.yaml`, so the namespace is read off the data: the leading hyphen segment of every
live ID, most frequent first, until 95% of the corpus is covered. That yields `bh` (beadhive),
`bhui` (beadhive-ui), `bh` (baml-harness).

**Invocations** (reproducible; run from this worktree, 2026-08-09):

```sh
# beadhive — pinned to the pre-spike baseline (see Evidence 6)
uv run python scripts/bead_commit_correlation.py --repo . --rev 2fd801e --limit 500 \
    --show-false-positives
uv run python scripts/bead_commit_correlation.py --repo . --rev 2fd801e --limit 200 \
    --show-false-positives

# the other two repos — READ-ONLY; only `git log` and `bh bd export` touch them
uv run python scripts/bead_commit_correlation.py \
    --repo /home/bees/workspace/github/beadhive/beadhive-ui --limit 500 --show-false-positives
uv run python scripts/bead_commit_correlation.py \
    --repo /home/bees/workspace/github/beadhive/baml-harness --limit 500 --show-false-positives
```

**Preconditions re-verified before trusting any number:** `bh bd export` succeeded (exit 0) in
all three repos, returning 2377 / 303 / 130 live bead IDs respectively. No store was unhealthy.

**Window caveat, stated rather than glossed.** "Last 500" is a real window only in beadhive (900
commits). beadhive-ui (144) and baml-harness (76) have **fewer than 500 commits in total**, so
`--limit 500` there means **their entire history**, including commits that predate the hive
having any beads at all. The two are not equivalent and are not treated as such below.

## Evidence

**1. Yield, per repo, against the pre-registered 500-commit window.**

|repo|window|linked (tightened)|yield|FP before|FP after|
|---|---|---|---|---|---|
|beadhive|last 500 of 900|410 / 500|**82.0%**|2.7%|0.0%|
|beadhive-ui|whole history (144)|59 / 144|**41.0%**|0.0%|0.0%|
|baml-harness|whole history (76)|49 / 76|**64.5%**|2.1%|0.0%|

**2. beadhive's last-200 window, for direct comparison with section 06's 73% hygiene figure.**
166 / 200 = **83.0%** resolvable-linked, FP before 1.7%, FP after 0.0%. Yield is *higher* than
the 73% hygiene number section 06 reported, because hygiene was measured on subject shape while
this counts subject **and** body.

**3. The unmappable bucket, held deliberately narrow.** Only the two classes the epic authorises
— `bump: version` commits, and multi-parent batch/PR merges that name no bead:

|repo|bump|batch / PR merge|true misses|yield over mappable|
|---|---|---|---|---|
|beadhive (500)|13|20|57|410 / 467 = 87.8%|
|beadhive-ui|5|18|62|59 / 121 = 48.8%|
|baml-harness|0|0|27|49 / 76 = 64.5%|

Widening this bucket would flatter the yield number, so it was not widened.

**4. The false-positive trap is real, and the resolve check closes it.** In beadhive's last 500,
32 of 1196 mentions (2.7%) were bead-ID-*shaped* tokens that resolve to nothing:

```text
   8  bh-harness      3  bh-worktrees    2  bh-mcp          1  bh-hq         1  bh-dispatcher
   4  bh-infra        3  bh-managed      1  bh-infra-6b0    1  bh-workspace  1  bh-owned
   2  bh-side         3  bh-developer    1  bh-3yb4         1  bh-fence
```

Four commits were linked **only** by such a token — without the resolve check the overlay would
have rendered each as linked to a bead that does not exist. The worst is `4392609b`
(`feat(container): compose wires four role-separated volumes…`), whose four "bead IDs"
(`bh-hq`, `bh-workspace`, `bh-worktrees`, `bh-harness`) are all Docker **volume names**.
baml-harness shows the same class at smaller scale (`bh-developer`, `bh-config-read`; 1 commit
lost). `bh-3yb4` is the one case of a genuinely bead-shaped ID that no longer exists in the
store — exactly what an existence check is for, and unreachable by any regex.

**5. Post-tightening false-positive rate is 0.0% in all three repos**, by construction: every
non-resolving mention is dropped in step 2. This is the *complement* of a measured claim, so it
was audited rather than asserted — the residual risk is a token that resolves but whose commit
is not really about that bead. Over the 1164 resolved mentions in beadhive the shapes are the
two tool-generated forms section 06 identified (`chore(merge): bead <id>`, `<subject> (<id>)`)
plus hand-written prose references; base36-ish 5-character IDs make coincidental collision with
an English word implausible, and no instance was found. The pre-registered "<5%" bar is met with
margin under any reading.

**6. The measurement excludes this spike's own commits.** The `bh-rwryq.1` commit body quotes
`bh-infra` / `bh-version` as examples, which inflated beadhive's false-positive count the moment
it landed (32 → 35 mentions, and `bh-version` appeared from nowhere). `--rev` was added so
beadhive is measured at `2fd801e` — the pre-spike `main` tip this branch forked from — making the
numbers stable and reproducible no matter how many spike commits follow.

**7. beadhive-ui's shortfall is structural, not random.** 45 of its 62 true misses **predate the
hive's first bead** (`0ad118a2`, 2026-07-23); the repo's history begins 2026-03-24 as an upstream
open-source project with a GitHub PR-merge workflow (18 `Merge pull request #N` commits, 5
`Bump version to X`). Restricted to its post-bead-adoption window the same matcher returns
**59 / 83 = 71.1%**. baml-harness likewise: **49 / 75 = 65.3%** post-adoption (adopted
2026-07-29). These figures are diagnostic for the recommendation only — the verdict below is
stated against the pre-registered window, not against this one.

## Verdict — **GO | NO-GO**

Pending `bh-rwryq.3`, which reads the evidence above and renders the verdict against the
pre-registered threshold.

## Recommendation

Pending `bh-rwryq.3`.
