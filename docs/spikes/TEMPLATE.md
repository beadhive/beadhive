# Spike `<bead-id>` — `<one-line question>`

**Bead:** `<bead-id>` · **Seat:** `<seat, e.g. dev/name>` · **Type:** research-only (no product code)
**Feeds decision on:** `<the decision bead / follow-on work this spike's verdict informs>`

> Canonical spike-artifact template. A spike bead (`type: task`, label `tag:spike`) is done
> when `docs/spikes/<bead-id>-<slug>.md` exists in this format — all five sections filled,
> **no product code**. Worked example:
> [`fekf-10-resumable-agent.md`](fekf-10-resumable-agent.md). Conventions:
> [PLANNING-PLANE.md — Spike loop](../PLANNING-PLANE.md#spike-loop); decision record:
> [planning-seat-ux-and-spike-loop.md](../design/planning-seat-ux-and-spike-loop.md).

## Question

State the single GO/NO-GO question this spike answers — concrete enough that evidence can
settle it, including what it is critically NOT asking.

## Method

How you investigated: what you searched, read, or ran (greps, docs, experiments) — enough
for a reader to reproduce or audit the search.

## Evidence

The findings, numbered, each anchored to a source (file:line, quoted doc, command output)
— facts only, no verdict yet.

## Verdict — **GO | NO-GO**

One word plus the shortest defensible justification, naming the concrete enabler (GO) or
blocker (NO-GO) the evidence established.

## Recommendation

What to do next given the verdict: on GO, what the implementation molecule should contain;
on NO-GO, what to close or re-scope, and any alternative that fits the evidence.
