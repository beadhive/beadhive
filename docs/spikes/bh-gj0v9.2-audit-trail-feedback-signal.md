# Spike `bh-gj0v9.2` — is a scheduling/model-tier feedback loop worth building?

**Bead:** `bh-gj0v9.2` · **Seat:** `dev/gj0v92` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-gj0v9.4` (the joining decision bead for epic `bh-gj0v9`), and
discharges the open prerequisite recorded in
[loop-ownership-and-execution-memory-adr.md](../design/loop-ownership-and-execution-memory-adr.md)
§"Open prerequisite — the gate-instrumentation gap".

## Question

**Would a scheduling / model-tier feedback loop — one that learns which model tier or seat to
route a bead to from past outcomes — change a routing decision this factory actually makes; and
if so, is it built on the `events` table, `dolt_diff_*`, or a new execution record?**

Critically **not** asking:

- whether the signal *exists* (epic `bh-gj0v9` already established most of it does);
- whether `formula`/`wisp` is the mechanism (that is `bh-gj0v9.1` / `bh-gj0v9.3`, and
  [loop-ownership ADR Decision 3](../design/loop-ownership-and-execution-memory-adr.md) already
  rejected the formula path for the dispatch loop);
- whether to *fix* anything found along the way. This spike routes defects; it does not repair
  them.

The bar is deliberately **value, not availability**. "The signal is there and it is not
actionable" is an expected and acceptable NO-GO.

### The motivating query, declared before any measurement

> **For beads that carry a `model:` tier, does the tier predict the outcome the router cares
> about — whether the bead bounced at review, and how long it took to close?**

If a feedback loop is worth building, this is the first query it must be able to answer, because
it is the *cheapest* member of the family: it needs no new instrumentation, only the tier the
planner already stamped and the outcome the lifecycle already records. A loop that cannot beat a
single `SELECT` over data that already exists has nothing to add.

## Method

The hive's own live store, queried from the cheapest source that works, in the order the bead's
design field prescribes: **current-state columns on `issues` first**, then event records, then
`dolt_diff_*` **as a cross-check only**.

1. **`bd sql` works now.** The epic recorded "`bd sql` does not work at all in embedded mode,
   which is still this hive's mode." That is **stale**: this hive now runs a Dolt sql-server
   (`.beads/dolt-server.port` → `3308`) and every query below ran through `bh bd sql`. This
   changes what is *reachable*, not any verdict below.
2. Corpus shape: `issues` (3300 rows), `events` (6025 rows), `labels`, `dependencies`.
3. Durability audit of each candidate source — `dolt_ignore`, `dolt_diff_events`, `dolt_log`,
   `dolt_status`, `.beads/config.yaml`, `git ls-files .beads/`.
4. The motivating query, answered two ways (bounce rate, time-to-close), with medians computed
   in Python from the raw per-bead rows rather than trusting `AVG()`.
5. Statistical power for the answer, so "no effect" can be distinguished from "not enough data".
6. Gate-instrumentation gap: recut by era and by gate `reason`, then traced to the code path.
7. Write-volume measurement against the unverified "~6000 rows/day" premise.
8. Both declared hazards re-tested rather than inherited.

Code read: `src/beadhive/work.py:211-265`, `src/beadhive/work_next.py:20-50,330-344`,
`src/beadhive/plan.py:254-300`, `src/beadhive/work_logic.py:326,365-383`,
`src/beadhive/state.py:99-207`, `docs/design/work-runtime-tiers-adr.md`,
`docs/design/loop-ownership-and-execution-memory-adr.md`.

Every query below is reproducible verbatim via `bh bd sql "<query>"` from the hive root.

## Evidence

### 1. The motivating query is answerable today, from the cheapest source, and the answer is *flat*

Bounce rate by tier — `issues` + `labels` + `dependencies`, no `events` table, no `dolt_diff`:

```text
tier         | beads | bounced
-------------+-------+--------
model:sonnet | 169   | 12        -> 7.1%
model:opus   | 112   |  8        -> 7.1%
model:haiku  |   8   |  0        -> n too small
```

Time-to-close by tier (medians computed from the raw rows, `n=85` closed non-infra beads):

```text
model:sonnet: n=48 median=50.5m p75=78m p90=178m max=24839m
model:opus:   n=35 median=55.0m p75=72m p90=101m max=170m
model:haiku:  n=2  median= 8.5m
```

**Bounce rate is identical to the first decimal place. Median time-to-close differs by 4.5
minutes, with sonnet *faster*.**

The `AVG()` of the same time-to-close column reads `sonnet 1097.1m` vs `opus 59.2m` — an
apparent 18× gap that is **entirely one bead** (`max=24839m`, a bead left open ~17 days). A
scheduler fed the mean would draw the opposite conclusion from a scheduler fed the median, off
the same rows. Recorded here because it is the single most likely way a feedback loop built on
this data would be *confidently* wrong.

### 2. Not enough data to detect an effect if one existed — and none for ~1.5 years

```text
closed non-infra beads (whole corpus lifetime, 2026-06-29 .. 2026-08-20):  830
...of those carrying a model: tier — the actual training set:              126
beads that bounced at review, all tiers combined:                           20
  (distribution: 44 beads bounced once, 13 twice, 1 three times)
```

Two-proportion power calculation, α=0.05, 80% power, to detect a **2× change** in bounce rate
(7.1% → 3.55%): **626 beads per arm, 1252 total.**

The training set is **126**. Lifetime throughput is 830 closed work beads over 53 days ≈
15.7/day, of which the tiered fraction is 126/830 ≈ 15% ⇒ **≈2.4 new training examples per
day** ⇒ **~520 days** to reach 1252. Even assuming every future work bead carries a tier, it is
~80 days — of a process that must stay stationary throughout, while `bd`/`bh` change weekly,
model versions rotate, and the planner's own tiering heuristic is itself under active revision.

### 3. Not merely underpowered — confounded in the direction that makes a learner dangerous

Model tier is **not a randomized treatment**. It is the planner's difficulty judgment, stated
explicitly in this very epic's design field:

> "Why every bead is `model:opus`: each spike is a cross-system architectural investigation …
> whose output is an ADR-grade artifact, and a wrong verdict misroutes an operational-substrate
> decision for the whole factory. None is a mechanical measurement task."
> — `bh-gj0v9` design field

So harder beads got opus, easier beads got sonnet, and both landed at 7.1%. Equal outcomes under
unequal difficulty is the signature of a **manual policy that is already working**. A
correlational learner cannot see the difficulty term; it sees only "tier does not predict
failure" and recommends demoting everything to the cheapest tier — precisely inverting the
policy that produced the flat rates. The failure is not "the loop learns nothing"; it is "the
loop learns the opposite, with a number attached."

### 4. `events` is `dolt_ignore`d and host-local — the worst available foundation

This is the decisive fact about source selection, and it was not known to the epic.

```text
select * from dolt_ignore;
pattern                   | ignored
--------------------------+--------
events                    | 1
leases                    | 1
local_metadata            | 1
repo_mtimes               | 1
wisp_%                    | 1
wisps                     | 1
ignored_schema_migrations | 1
```

Corroborating measurements, all consistent:

| Check | Result |
|---|---|
| `to_commit` in `dolt_diff_events` | `WORKING` for all **6025** rows — never committed |
| `select count(*) from dolt_status` | `0` — clean tree, because the table is ignored |
| `created_at` range on `events` | `2026-08-07` .. `2026-08-20` — a **13-day** horizon |
| `min(created_at)` on `issues` | `2026-06-29 03:43:46` — a **53-day** corpus |
| `.beads/config.yaml` | `# events-export: false` (`bd config get events-export` → *not set*) |
| `.beads/events.jsonl` | does not exist |
| `git ls-files .beads/` | `.gitignore`, `config.yaml`, `metadata.json` — no event data |

**⇒ `events` is never Dolt-committed, therefore never pushed, therefore host-local, absent from
every other clone, and outside every backup that covers the corpus.**

Two independent observations show it is also **unstable across readings**:

- the epic recorded "**9k+ rows** live in this hive"; it now holds **6025** — a table that shrank
  while the corpus it describes grew from 2321 to 3300 issues;
- the epic recorded `bh-w4qsu` as having "**ZERO rows** in `events` despite being closed"; it
  now has **two** (`created` @ 13:10:30, `closed` @ 13:29:56, both `actor=dev/dev1`).

A source whose row count moves *down* between observations, whose per-bead answers move *up*,
and which no other machine can see, cannot carry a decision about how to spend money on model
tiers.

### 5. The gate gap: 60% is a durability artifact, 8 rows are a real defect on one code path

The loop-ownership ADR recorded 453 gates / 406 created / 232 closed and assigned the
classification to this spike. Re-measured today:

```text
gates (issue_type='gate'):                            680
...with a 'created' row in events:                    266
...with a 'closed'  row in events:                    266
...with closed_at set on the issue row (ground truth): 629
```

Split at the `events` horizon from §4 (`2026-08-07 07:25:10`):

```text
era          | gates | with_created
-------------+-------+-------------
pre-horizon  |  406  |   0
post-horizon |  274  | 266
```

**Every single one of the 406 "uninstrumented" gates predates the working set.** They are not
dropped events; they are gates that existed before the local, uncommitted table that would have
described them. That is not a defect — it is §4's durability class, read as a bug.

The residual 8 are real. Split post-horizon gates by the `reason` in their description, which
maps 1:1 onto the code path that created them:

```text
reason    | gates | with_created
----------+-------+-------------
bh:review |  173  | 173          -> 100.0%   (work_logic.py:374)
kickoff   |  103  |  95          ->  92.2%   (plan.py:260-266)
```

All 8 drops are `kickoff` gates, confirmed individually (`kickoff bh-gj0v9` ×3, `bh-7jm7v`,
`bh-rwryq`, `bh-1b0rc`, `bh-j4gbx`, `bh-13spb`). Two rival explanations were tested and refuted:

- **not the horizon** — `events` was actively recording within minutes of each drop (40 rows in
  the 3.5h window around the 08-09 pair, 22 around 08-15, 104 around 08-18);
- **not a write burst** — dropped gates were created in *smaller* concurrent bursts than
  instrumented ones (mean 1.75 vs 4.26 gates within ±5s).

What distinguishes the two paths is in the source. `work_logic.py:374` checks the exit code and
carries a documented recovery for the exact partial-failure mode where the row is created but
`bd` exits non-zero:

```python
# work_logic.py:326
"""True iff `bd gate create` opened the review gate but exited non-zero on the blocking dep."""
```

`plan.py:260-266` discards it:

```python
def _create_kickoff_gate(root_id: str, epic_id: str, cwd, actor: str) -> None:
    """Open THE kickoff gate for one molecule root (see the contract note above)."""
    bd.run(
        ["gate", "create", "--type=human", "--blocks", root_id, "--reason", f"kickoff {epic_id}"],
        cwd,
        actor=actor,
    )   # <- return code never inspected
```

The path that checks is 173/173. The path that does not is 95/103. **Classified: defect**, on
`plan.py:_create_kickoff_gate`, narrow and independently fixable by mirroring its sibling.
(`_create_release_hold_gate`, plan.py:284-300, has the identical unchecked shape and is
presumed to share it.) Not fixed here — routed in the Recommendation.

### 6. …and the whole worry was aimed at the wrong table

Both the ADR's open prerequisite and `work_next.py`'s module docstring cite the gate/`events`
numbers as grounds to treat the derived retry count as a lower bound:

> "KNOWN LIMIT, deliberately not papered over: the event record is incomplete today (of 453
> `issue_type='gate'` rows only 406 carry a created event and 232 a closed event). A dropped
> event makes a derived count UNDER-count… `bh-gj0v9.2` owns classifying that as defect or
> by-design; until it lands, treat :func:`attempt_count` as a lower bound."
> — `src/beadhive/work_next.py:30-38`

**No derived count in this codebase reads the `events` table.** `attempt_count`
(`work_next.py:330`) and `dispatch_cause_count` (`work.py:246`) both consume `_flow_events`
(`work.py:211-218`), which is:

```python
rows = bd.json(["list", "--parent", bead, "--include-infra"], cwd)
return [r for r in rows if str(r.get("issue_type") or "") == "event"]
```

— `issue_type='event'` **beads** written by `bd set-state`, i.e. ordinary `issues` rows. Those
are Dolt-committed and git-synced, and their measured history is a different class entirely:

| | `events` table | `issue_type='event'` beads |
|---|---|---|
| rows | 6025 | 916 (28% of a 3300-row corpus) |
| earliest | 2026-08-07 | **2026-07-12** |
| in `dolt_ignore` | **yes** | no |
| committed / pushed / visible from another host | **no** | yes |
| stable across re-reads | **no** (§4) | yes |

Live projection over that durable source, the query `attempt_count` exists to serve:

```text
State change: review → pending           479
State change: review → changes-requested  73    (44 beads ×1, 13 ×2, 1 ×3)
State change: kickoff → approved          66
State change: review → approved           18
```

The prerequisite is therefore **discharged by re-aiming it, not by repairing anything**.

### 7. The retry/cause gap the ADR named is already built — cost to close it is zero

`work-runtime-tiers-adr.md:244` names the gap:

> "`local` has no retry policy and no execution history. It records *that* state changed, never
> *why* it was retried."

It has since been closed in code, exactly as
[loop-ownership ADR Decision 2](../design/loop-ownership-and-execution-memory-adr.md) specified:

- `state.py:181` — `STATE_DIMENSIONS["dispatch"] = DISPATCH_CAUSES`, a closed reason-code set;
- `work.py:258` — `record_dispatch_failure(bead, cause, reason, …)`, documented "FAILURE PATH
  ONLY — bounces, stalls and escalations, never a per-pass or per-attempt heartbeat";
- `work.py:246` — `dispatch_cause_count(events, cause)`, derived, "never a stored counter";
- `work_next.py:330` — `attempt_count(events, action)`, the loop-breaker's input.

**Cost of building the retry/cause record: already paid.** Live rows today:
`select label, count(*) from labels where label like 'dispatch:%'` → **0 rows** — the mechanism
ships, its consumer (the unattended dispatch loop) has not yet run in anger. So there is nothing
to fund, and also nothing yet to learn from.

### 8. The "~6000 rows/day" bloat premise is wrong by ~10× — but bloat is real, and misattributed

Measured write volume, durable corpus (`issues`, the only rows that persist and sync):

```text
2026-08-20   88     2026-08-15  152     2026-08-11  135
2026-08-19  109     2026-08-14   23     2026-08-10  103
2026-08-18   82     2026-08-13  104     2026-08-09   91
2026-08-17   10     2026-08-12   58     2026-08-08  124
```

**≈60–150 rows/day**, lifetime mean 3300/53d ≈ 62/day. `events` adds ≈463/day but is
host-local and never persisted (§4), so it costs nothing durable. The `~6000/day` figure is
unsupported at this hive's scale and should not be cited again.

Bloat nonetheless exists — in the other dimension:

```text
Dolt commits (dolt_log):        8622 over 26 days  (recent: 330-725/day)
.beads/issues.jsonl:            6.9 MB   (logical content)
.beads/backup (Dolt archive):   470 MB   -> ~68x amplification
```

**The cost driver is commit count, not row count** — every bead write is its own Dolt commit.
And the remedy is unavailable here: `bd compact` and `bd flatten` are forbidden in this hive per
`CLAUDE.md` until `bh-3vs6c` lands. Any new durable execution record would add rows *and*
commits to a store whose only compaction path is closed. This makes the "new execution record"
option strictly worse than it looks, independently of whether the signal is valuable.

### 9. Both declared hazards, re-tested

**Hazard A — the ~7h `events`-vs-Dolt timezone offset: does not reproduce.**

```text
max(events.created_at)  = 2026-08-20 17:07:13     +0000 UTC
max(dolt_log.date)      = 2026-08-20 17:07:13.497 +0000 UTC
now() = utc_timestamp() = 2026-08-20 17:12:31     +0000 UTC   (host TZ = UTC)
```

Offset is **zero to the second**. The 7h gap was a property of the earlier *embedded-mode*
observation on a non-UTC session, not of the schema. It is still true that a row does not record
which regime wrote it, so any join across the two must normalize — but the hazard is
backend/TZ-dependent, not intrinsic.

**Hazard B — phantom `dolt_diff` rows: far worse than "at least one", and mechanically general.**

```text
select count(*) total,
       sum(diff_type='modified' and from_status<=>to_status
           and from_updated_at<=>to_updated_at) phantom
from dolt_diff_issues;

total | phantom
------+--------
11461 | 3077        -> 26.8%
```

Concentration identifies the mechanism — and it is **not** merge resolution:

```text
to_commit                        |   c  | message
---------------------------------+------+------------------------------------------------
v6aed1e3u0ef1iik0seji32t25loqqtg |  886 | schema: apply migration 0055_move_leases_to_table
r086dp96t2a5ntvaon62bmtvigt8m7hl |  886 | schema: apply migration 0054_add_lease_columns
t1aoiabnli1nni17k8peth6vkkutbptk |   22 | groom 2026-08-11: friction cluster restructure
```

Two `bd` schema migrations account for 1772 of the 3077. **Any full-table rewrite injects one
phantom "modified" row per issue** — and `bd` is at migration 62 and ships more with each
release, so this recurs on every upgrade, permanently, at corpus scale. `dolt_diff_*` is
disqualified as a primary source and is unreliable even as a cross-check without filtering
schema-migration commits by hand.

### 10. The cheap descriptive query does work — and says the premise behind it is false

For completeness, the query the original brief imagined ("this gate historically takes 40
minutes, don't block on it synchronously"), from `issues` columns alone — no events, no diff:

```text
bh:review (n=348)  p50=  5.4m  p75=17.4m  p90= 44.6m  p99= 534.3m   mean= 35.7m
kickoff   (n=237)  p50=  5.2m  p75=35.1m  p90=462.0m  p99=16669.9m  mean=682.2m
review    (n= 37)  p50=  2.8m  p75= 7.8m  p90=439.7m  p99=8688.7m   mean=287.7m

all closed gates (n=629):  <1m:107  1-10m:296  10-60m:162  1-24h:48  >24h:16
```

Cross-check against the epic's spot measurement: `bh-w4qsu` created `13:10:31`, closed
`13:29:56` = **19m25s** — matches exactly. The cheap SELECT reproduces.

**The median gate — of every class, including the human kickoff gate — closes in about five
minutes.** The 40-minute premise is a *mean* artifact (§1's failure mode again, in a second
place). What genuinely differs is the p90: 44.6m for review vs 462m for kickoff, a 10× tail
split — and that split is **fully predictable from the gate's own `reason` string**, which is
already in the row at creation time. There is nothing to learn, only something to read.

## Verdict — **NO-GO**

**No.** The blocker is value, not availability.

The one query a model-tier router must answer is answerable today, in one `SELECT`, from durable
git-synced data — and it returns **7.1% bounce rate for sonnet and 7.1% for opus** (§1), with
medians 4.5 minutes apart in the *cheaper* tier's favour. There is no effect to route on. There
also could not have been: n=126 against the 1252 needed to detect even a 2× difference, growing
at ~2.4/day (§2).

And the data is worse than empty. Tier is the planner's difficulty judgment, not a randomized
treatment, so flat outcomes across unequal difficulty are what a **working** manual policy looks
like — a learner fit on it recommends demoting everything to the cheapest tier and inverts the
policy that produced the flat rates (§3). The loop's most likely output is not "no
recommendation" but a confident, expensive, backwards one.

Source selection is therefore moot, and each candidate fails on its own terms anyway: `events` is
`dolt_ignore`d, never committed, host-local, 13 days deep, and demonstrably unstable across
readings (§4); `dolt_diff_issues` is 26.8% phantom rows, regenerated by every `bd` schema
migration (§9B); a new execution record would add both rows and Dolt commits to a store with 68×
amplification whose only compaction path is forbidden until `bh-3vs6c` (§8).

**No argument against a standing ADR is required, because the evidence supports both.**
`work-runtime-tiers-adr.md` Decision 1 and `loop-ownership-and-execution-memory-adr.md`
Decision 2's zero carve-out say durable lifecycle memory lives in beads and nowhere else. The one
table in this store that sits outside the bead corpus is exactly the one that turned out to be
non-durable, non-shared, and unstable (§4). The invariant was right for reasons its authors had
not yet measured.

## Recommendation

**Close the question. Do not build the loop on any source, and do not re-open it on "we just need
more data" — §2 prices that at ~520 days, and §3 shows more of this data does not help.**

Four concrete follow-ups, in descending value. None is large.

1. **Correct the misdirected prerequisite (documentation only, no code).** Amend
   `loop-ownership-and-execution-memory-adr.md` §"Open prerequisite — the gate-instrumentation
   gap" and the KNOWN LIMIT paragraph in `src/beadhive/work_next.py:30-38`. Both cite `events`-
   table gate coverage as grounds to treat `attempt_count` as a lower bound, but `attempt_count`
   reads `issue_type='event'` **beads** via `_flow_events` (§6) — a Dolt-committed, git-synced,
   39-day source with no analogous horizon. The prerequisite is **discharged**, and the ADR can
   move from "accepted with an outstanding dependency" to accepted outright.

2. **Route one defect (§5): `plan.py:_create_kickoff_gate` discards `bd gate create`'s return
   code.** Its sibling `work_logic.py:374-383` checks it and handles the known partial-failure
   mode; the checked path is 173/173 instrumented, the unchecked one 95/103.
   `_create_release_hold_gate` (plan.py:284-300) has the identical unchecked shape. Worth fixing
   on its own merits — a kickoff gate that silently half-fails is a planning-plane correctness
   issue, entirely separate from this spike's verdict. **File as a `bug`, not as part of any
   feedback-loop molecule.**

3. **Record the durability class of `events` somewhere a future reader will hit it.** §4 is the
   finding most likely to be re-derived expensively. One line in `docs/OBSERVABILITY.md` — *"the
   `events` table is in `dolt_ignore`: host-local, never committed, never pushed; the durable
   audit record is `issue_type='event'` beads"* — prevents the next investigation from starting
   where this one did. If durability of that table is ever genuinely wanted, the switch is one
   line (`events-export: true` in `.beads/config.yaml` → `.beads/events.jsonl`); **not
   recommended without a named consumer**, since it turns a free local table into corpus growth
   against §8's forbidden compaction path.

4. **If gate latency is ever revisited, read percentiles from `issues`, not a model.** §10's
   table is a single `SELECT` over columns that already exist, needs no new storage, and already
   answers the scheduling question — with the correction that the median gate closes in ~5
   minutes and the only real signal (a 10× p90 split) is readable directly off the gate's
   `reason` string. Note prominently that `AVG()` reverses the conclusion in **two independent
   places** (§1 tier durations, §10 gate durations); any future consumer of these columns should
   be required to use percentiles.

**Explicitly not recommended:** a `bead_intents`-style table, per-attempt instrumentation, wisp
telemetry, an OTEL bead-id join, or any new execution record. Each adds durable write volume to
a store that cannot compact, in service of a question whose cheap answer is already known to be
flat.
