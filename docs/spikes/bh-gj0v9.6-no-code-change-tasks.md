# Spike `bh-gj0v9.6` — Does wisp (or a new bead kind) fit a purposefully branch-free operational task?

**Bead:** `bh-gj0v9.6` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-gj0v9.4` (adopt formula/wisp, push beads-as-Guide-StateBackend, or
close the operational-substrate question with an ADR)

> Sibling of `bh-gj0v9.1`, whose finding is taken as settled and not re-derived: a formula
> `Step` has **no `action` / `script` / `command` field**, so a formula never executes
> anything — it only materialises bead records via `pour` / `wisp`. The "doing" always happens
> out-of-band.

## Question

`bh-87ktb` made `bh work review --run/--demo` **refuse** (nonzero exit, `validate_cmd` never
invoked) when the resolved branch has zero commits over its base
(`src/beadhive/work_show.py:253-262`). Correct for the false-green bug it fixed — and it now
also blocks a task that is *purposefully* code-free, the operator's example being **"apply the
tofu infra changes"**: an operational item that will never have a commit, and for which we do
not even want a branch/worktree at claim time.

**Three-way GO/NO-GO:**

1. **Formal no-code-change bead kind** — a kind/label that `claim` (skip worktree+branch),
  `submit` (record completion directly, no clean checkout) and `review` (skip the
  zero-commits refusal) all recognise. GO or NO-GO?
2. **Wisp specifically** — does bd's *existing* TTL/ephemeral wisp machinery
  (`bd create --ephemeral --wisp-type`, `bd mol wisp/gc/squash/burn`) fit that role? GO or
  NO-GO — judged on actual wisp semantics, not the abstract concept.
3. **Keep out of the bead lifecycle entirely** — track ops work some other way, losing bead
  dependency/gating/audit. GO or NO-GO?

This is **NOT** asking whether `bh-87ktb`'s refusal should be softened (it should not — see
**E9**), nor re-litigating whether a formula can execute (settled: it cannot).

## Method

- Read the refusal and its blast radius in this hive's own source: `work_show.py`,
  `work.py` (`_claim_single_bead`, `submit` and its five guards, `_guard_bead_clean_history`),
  `work_logic.py` (`_history_ok`, gate seams), `claim_authority.py`.
- `grep -rn wisp --include=*.py src/` across all of `bh` to establish current integration.
- Read `bd mol wisp --help`, `bd mol wisp gc --help`, `bd mol burn --help`,
  `bd create --help` (bd 1.1.0).
- **Ran a live wisp experiment** in a throwaway hive (`bd init --prefix wl`, scratchpad, never
  the real hive) — the first live exercise of that path on this machine
  (`loop-ownership-and-execution-memory-adr.md:222` records `bd mol wisp list` as empty in
  every hive here). Created an ephemeral bead titled "apply tofu infra changes", wired a
  persistent bead to depend on it, closed it, and ran `bd mol wisp gc --closed --force`.
- Ran the same scenario through the **gate-bead** shape (`bd gate create --blocks … --type
  human`, `bd gate resolve --reason`, `bd ready`, `bd history`) for comparison.

## Evidence

### On wisp

- **E1.** **A wisp is invisible to the work queue.** In the scratch hive, `bd create "apply tofu infra
  changes" --ephemeral --wisp-type patrol` produced `wl-wisp-d6m`, and it appears in **neither
  `bd list` nor `bd ready`** — both showed only the persistent bead (`Total: 1 issues`). It is
  visible only via `bd mol wisp list` / `bd show <id>`. A primary work item nobody's ready
  queue can see is not dispatchable work.

- **E2.** **A wisp *can* block a persistent bead, invisibly.** `bd dep add wl-pdh wl-wisp-d6m`
  succeeded, and `bd ready` then reported *"No ready work found (all issues have blocking
  dependencies)"* with the blocker absent from every list view. Real work blocked by an
  invisible blocker.

- **E3.** **GC of a completed wisp silently rewrites a persistent bead's dependency graph.** After
  `bd close wl-wisp-d6m`, `bd mol wisp gc --closed --force` deleted it — and `bd show wl-pdh`
  afterwards has **no `DEPENDS ON` section at all**. The record of what gated that code work is
  gone from the permanent bead, with no tombstone. (`gc --closed` also warns *"Cascade mode
  enabled - will also delete all dependent issues"*.) A completed op is exactly the
  most-GC-eligible wisp state: `gc --help` — only `open` and `closed` steps are
  age-reclaimable, and `--closed` purges all closed wisps regardless of age.

- **E4.** **Wisps are local-only.** `bd mol wisp --help`: *"Wisps are issues with Ephemeral=true
  in the main database. They're stored locally but NOT synced via git."* An infra apply kept as a
  wisp is invisible to every other seat and machine — including the dispatcher. `bh`'s own
  publish boundary treats them the same way: `publish_export.py:47` names `--all` permanently
  forbidden because it *"also reaches bd's ephemeral wisps table."*

- **E5.** **bd's own routing doc sends this case to `pour`, not `wisp`.** `bd mol wisp --help`:
  wisp = *"Any operational workflow **without audit value**"*; pour = *"Work you may need to
  reference later … Anything worth preserving in git history."* An infra apply — who applied
  what, to which environment, when — has audit value by definition.

- **E6.** **`bh` has zero wisp integration today.** The only occurrences of `wisp` in `src/` are
  table names in `hub_bulk.py` (bulk-copy fidelity) and the forbidden-flag note in
  `publish_export.py`. No verb, no config key, no code path.

- **E7.** **This hive already rejected the mechanism once, and named where it does fit.**
  `docs/design/loop-ownership-and-execution-memory-adr.md:196-237` — Decision 3 rejects
  formula/wisp for the dispatch loop, notes *"the path has zero live rows"*, and names the
  good fit: **a wisp molecule per dispatch pass, as a trace** — burn on a clean pass, squash
  into a digest on escalation. Housekeeping signal, not primary work item. The `--wisp-type`
  enum agrees: `heartbeat, ping, patrol, gc_report, recovery, error, escalation`.

### On a formal no-code-change bead kind

- **E8.** **The claim record lives inside the worktree.** `claim_authority.py:152-192` —
  `LocalTrustAuthority` writes `bh-claim.json` into *the worktree's own git-dir*. "Skip
  worktree provisioning" therefore also deletes the store that seat verification
  (`_resolve_submit_actor`, `work.py:2168-2183`) and the fencing-token epoch guard
  (`_guard_claim_fence`, `work.py:2191-2200`) both read. A no-worktree kind needs a *second*
  claim-authority backend before it can be claimed at all.

- **E9.** **The zero-commit refusal is not one check — it is four, on three verbs.**
  - `submit` → `_guard_submit_ready` (`work.py:2202-2218`): clean tree, on the expected
     branch, then `_history_ok`, whose `count == 0` branch returns *"no commits over the
     integration branch — nothing to submit"* (`work_logic.py:555`). Submit was already
     refusing before `bh-87ktb`; review only caught up.
  - `submit` → `_validate_submit_checkout` + `_record_submit_commits` + `sha =
     head_sha(target)` for the gate reason: all three presuppose a branch and a commit.
  - `review` → the `bh-87ktb` refusal (`work_show.py:253-262`).
  - `merge` → `_guard_bead_clean_history` (`work.py:3259-3273`) re-runs `_history_ok` as a
     backstop, and `bh work merge` merges `--no-ff`, which has nothing to merge.

  So option (a) is not "one flag on claim". It is a parallel lifecycle: a second claim store,
  four guard bypasses, a substitute for the sha the review gate is keyed on
  (`ensure_review_gate`, `work_logic.py:358-386`), and a merge path that does not merge.

### On the third way — what already exists

- **E10.** **`gate` is already a bd issue type that lives entirely outside the branch lifecycle.**
    This very spike is blocked by one: `bh-95y5q · Gate: human · Type: gate`, *"Ad-hoc gate
    blocking bh-gj0v9.6"*. Verified end-to-end in the scratch hive:
    `bd gate create --blocks wl-pdh --type human --reason "ops: apply the tofu infra changes"`
    → `bd ready` shows the dependent bead blocked; `bd gate resolve wl-5hf --reason "applied
    tofu plan sha256:abc123 at 2026-08-20T12:00Z by ops/dev1"` → the bead is ready again. No
    branch, no worktree, no commit, at any point.

- **E11.** **Gate beads are persistent and versioned — they satisfy the archive rule.** `bd history
    wl-5hf` returns both versions with timestamps and author (`○ open` → `✓ closed`), i.e. the
    lifecycle record CLAUDE.md's "bead history is an archive" rule exists to protect. Unlike a
    wisp, nothing about a gate bead is TTL- or GC-eligible.

- **E12.** **`bh work approve` already dispatches non-review gate kinds by seat.** `work.py:2340-2375`
    plus `_approve_security_gate` (warden) and `_approve_release_hold_gate` (releaser), with
    `_gate_kind` classifying `review | security | release-hold | kickoff | other`
    (`work_logic.py:389-400`). Adding an `ops:` gate kind is an entry in that existing table —
    not a new lifecycle. It is the only real gap: today an ops gate must be resolved with raw
    `bd gate resolve`, since `approve` refuses non-review gates.

## Verdict

| Option | Verdict |
|---|---|
| Formal no-code-change **bead kind** (claim/submit/review/merge all special-cased) | **NO-GO** |
| **Wisp** specifically | **NO-GO** |
| Keep ops work out of the **branch/review** lifecycle, inside the **bead graph** | **GO** |

**Wisp — NO-GO.** Not a shortfall, a category mismatch, on four independent grounds: a wisp is
invisible to `bd list` / `bd ready` (E1), local-only and never synced (E4), GC of the *completed*
record silently strips the dependency edge off a persistent bead (E3), and bd's own docs route
anything with audit value to `pour` (E5). E3 alone disqualifies it for infra applies: the one
state an infra-apply wisp spends its life in is the state GC reclaims. The CLAUDE.md
archive-rule question resolves cleanly — it is *not* acceptable to say "the record that matters
lives in tofu state", because what GC destroys is not the apply's own record but the
*persistent* bead's history of having been gated by it.

**Formal no-code-change bead kind — NO-GO.** The cost is a parallel lifecycle: a second
claim-authority store (E8) plus four guard bypasses across submit/review/merge and a substitute
key for the review gate's sha (E9) — for a case two mechanisms already in this repo cover with
zero new code (E10-E12). Softening `bh-87ktb` is likewise rejected: forcing an operational task
to leave a committed artefact is a feature, not friction.

**Keep it out of the branch lifecycle, not out of beads — GO.** The operator's "TBD ... if we
need a 'bh' concept to wrap them" has an answer: the concept exists and is called a **gate
bead**.

## Recommendation

**Two shapes, both already shipping. Write them down; build nothing.**

- **E1.** **Human / out-of-band op that gates code work** → a **gate bead**:
  `bd gate create --blocks <bead> --type human --reason "ops: apply the tofu infra changes"`,
  resolved with the evidence in the reason (plan hash, run URL, state serial). Persistent,
  versioned, dependency-tracked, never branched, never GC-eligible. Zero `bh` changes.

- **E2.** **Agent-performed op with a real outcome** → an **ordinary bead** whose deliverable is the
  *evidence record* (e.g. `docs/ops/<date>-<env>-apply.md` carrying the plan hash and apply
  output). It then has ≥1 commit and every existing guard passes untouched — and `bh-87ktb`'s
  refusal becomes the mechanism that *forces the audit trail to exist* rather than the thing
  blocking the work.

**The one follow-on worth filing** (do not build it in this spike): teach `bh work approve` an
`ops:` gate kind, alongside `security:` and `release-hold:` — an entry in the `_gate_kind` table
and one `_approve_*_gate` helper in the existing shape (E12), so an operator resolves an ops
gate through the convention layer instead of dropping to raw `bd gate resolve`. Small, additive,
and it touches no part of claim / submit / merge.

**For `bh-gj0v9.4`:** this spike contributes a second independent NO-GO on wisp as a work-item
substrate, converging with `bh-gj0v9.1` and with Decision 3 of the loop-ownership ADR. The only
live wisp use case named anywhere in this hive remains the **per-pass dispatch trace**
(burn on clean / squash on escalation) — a housekeeping signal, which is what the
`--wisp-type` enum has said all along.
