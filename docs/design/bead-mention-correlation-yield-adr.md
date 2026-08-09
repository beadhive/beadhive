# Bead-mention correlation yield ADR — NO-GO for overlay v1 (bh-rwryq.3)

> Status: **decided (NO-GO), qualified.** Bead-ID mention correlation is not sufficient to carry
> the "Bead Graph × Git History Overlay" v1 **unconditionally**: read per hive against the
> pre-registered threshold, one of three in-scope repos falls short — and it is beadhive-ui, the
> repo the overlay ships in.
>
> **The qualification travels with the label.** The *accuracy* half of the threshold passed
> decisively in **all three** repos (0.0% post-tightening false positives against a <5% bar). The
> matcher works. What failed is the *coverage* half, in exactly one hive, for a reason no matcher
> can fix — history that predates that hive adopting beads. This is a narrow, structural finding
> about one repo's history, **not** a rejection of the matching technique. Quoting "NO-GO on
> correlation" without this paragraph overstates the evidence.
>
> Evidence record (per-repo numbers, false-positive token list, the matcher pattern, reproducible
> invocations): [`../spikes/bh-rwryq.3-correlation-yield.md`](../spikes/bh-rwryq.3-correlation-yield.md).
> That document is the measurement; this one is the decision. The numbers are deliberately **not**
> restated here — one source of truth per fact.

## Context

Spike `bh-rwryq` asked one question with a threshold **pre-registered before any number was
seen**: does commit-message bead-ID correlation resolve enough commits to render a commit↔bead
overlay without waiting on durable linkage (`metadata.git.commits`, epic `bh-1b0rc`)?

> **GO** requires ≥60% of the last 500 commits mapping to at least one **resolvable** bead, with a
> post-tightening false-positive rate under 5%.

`bh-rwryq.1` built the canonical two-step matcher
([`../../scripts/bead_commit_correlation.py`](../../scripts/bead_commit_correlation.py): loose
namespace-anchored extraction, then an existence check against that repo's **own** live bead
store). `bh-rwryq.2` measured it across beadhive, beadhive-ui, and baml-harness. `bh-rwryq.3`
rendered the verdict. An independent reviewer reproduced all three measurements byte-identically.

## Decision

**NO-GO on ID-mention correlation as an *unconditional* basis for overlay v1.** The threshold is
applied **per hive**, over each repo's own last-500-or-whole-history window, and one of the three
in-scope hives does not clear the 60% coverage bar.

Per-hive is not a choice made at verdict time: the epic's own hard constraint is that any
downstream view renders **one hive's own data** and must never aggregate across hives. A bar that
governs such a view has to clear per hive too. Pooling the three repos into a single ratio would
clear it — which is exactly the cross-hive average that constraint forbids.

### Why this reading, and not the two alternatives

Two other readings of the same pre-registered sentence are plausible enough that a future reader
would re-derive the ambiguity. Both were considered and rejected; naming them is the point of this
section.

- **`literal-500-window` — rejected.** Read "last 500 commits" so strictly that the threshold only
  applies to a repo with ≥500 commits of history at all. This discards two of the three in-scope
  repos outright (beadhive-ui and baml-harness both have well under 500 commits total),
  **including beadhive-ui — the overlay's own shipping target**. It leaves no way to render a
  verdict about the repo the feature actually ships in, which is the one repo the verdict is for.

- **`post-adoption-window` — rejected as the basis for the verdict.** Re-slice each repo's history
  to begin at its first bead. This is a real, documented effect, not a fudge: beadhive-ui's yield
  rises substantially once its pre-bead-adoption commits are excluded, and clears the bar. But as a
  *verdict* basis it is a discretionary re-slice of the measured population chosen **after** the
  raw number failed — precisely the post-hoc rationalization that pre-registration exists to
  prevent. It survives as a forward-looking **recommendation** (see #3 below); it is not a way to
  pass the gate retroactively.

- **The reading actually used is neither of those.** Per hive, over each repo's own
  last-500-or-whole-history window, with the window caveat stated up front in the spike's method
  and the reading fixed **before** any post-hoc adjustment was on the table. It neither drops the
  short-history repos (unlike `literal-500-window`) nor re-cuts the population after seeing the
  number (unlike `post-adoption-window`).

### What the NO-GO does and does not cover

Stated separately so it cannot be dropped when the verdict is summarized elsewhere:

- **Accuracy: passes, everywhere.** 0.0% post-tightening false positives against the <5% bar in
  all three repos. The two-step filter closes the whole `bh-<word>` hive/branch/volume-name class,
  including the case no regex can reach (a well-formed ID that no longer exists in the store). The
  technique is sound and shippable.
- **Coverage: fails, in one hive of three.** And for a cause outside any matcher's reach — commits
  authored before that hive had beads to reference. No pattern recovers a link that never existed.

## Recommendations

What downstream work consumes from this decision:

1. **Ship the matcher verbatim.** `scripts/bead_commit_correlation.py`'s
   `extract_candidates()` / `resolve_candidates()` pair is the canonical implementation. The
   durable-linkage backfill (`bh-1b0rc`) and beadhive-ui's correlator should reuse it rather than
   author a third regex.
2. **Gate the overlay per hive on a *measured* yield, not an assumption.** Render the commit↔bead
   view only where a hive clears the 60% bar; show an explicit "insufficient linkage" state
   elsewhere. Measuring a hive is cheap (one bead export plus one `git log`) and turns a silently
   misleading view into an honest one.
3. **Scope the overlay's rendering window to post-bead-adoption history.** This is a **view** fix,
   not a matcher fix, and it is where beadhive-ui's shortfall actually lives. A hive's pre-adoption
   commits never had a bead to link to; rendering them as "unlinked" reports a linkage failure that
   did not happen.
4. **Durable linkage (`metadata.git.commits`) remains the prerequisite for the blanket case.** It
   is the only thing that recovers the residual post-adoption misses — real work commits carrying
   no bead reference at all, which no matcher can recover from the commit message alone.

## Consequences

- Overlay v1 may ship **conditionally** (per-hive gated, post-adoption-windowed), not blanket.
  Recommendations 2 and 3 are the conditions.
- beadhive-ui's own overlay is gated on recommendation 3 landing; without the window fix it renders
  the "insufficient linkage" state against its own history.
- The matcher is not blocked by this decision and should not be re-litigated by it — see the
  qualification above.
- Re-measure before treating this verdict as still current: yield is a property of a repo's
  history at a point in time, and every bead-referencing commit since moves it.
