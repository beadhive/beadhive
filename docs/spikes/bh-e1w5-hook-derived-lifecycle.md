# Spike `bh-e1w5` — Should git events drive bead LIFECYCLE (auto-claim, push-as-submit, reconcilers)?

**Bead:** `bh-e1w5` · **Seat:** `dev/fable-l` · **Type:** research-only (no product code)
**Feeds decision on:** whether any hook-derived-lifecycle direction from the bh-7k1p triage
discussion deserves a molecule — vs. recording a NO-GO and keeping AGF's explicit-verb premise.
Cross-references: bh-v0wu (PR landing / push semantics), bh-bh2h.2 (deterministic dispatch),
bh-nikb (per-invocation verify dirs), bh-7k1p (verify-init contract).

## Question

bd's git hooks already sync bead **data** with git activity (`.beads/hooks/*` shims →
`bd hooks run <hook>`). Could git **events** drive bead **lifecycle** so agents perform fewer
explicit verbs? Three candidate directions:

1. **Auto-claim** — `post-checkout` in a new worktree on `wt/bead/<type>/<id>` names the bead;
   a hook could claim it.
2. **Push-as-submit** — adopt the PR-world convention that pushing a bead branch = submitting it.
3. **Reconcilers** — hooks as drift *detectors*, not transition drivers: warn on
   claim/worktree/bead-state divergence; periodic divergence report.

**GO/NO-GO per direction:** can each be implemented at acceptable guard cost (verify-`*`
exclusions, hook re-entrancy, non-interactive contexts) without breaking AGF's premise that
lifecycle transitions are explicit, audited, gated verbs?

This is **NOT** asking whether bd's existing data-sync hooks are sound (they're upstream and out
of scope), nor whether agents *should* perform fewer verbs (bh-bh2h.2 already pursues that via
deterministic dispatch). It asks only whether *inferring lifecycle transitions from raw git
events* clears the guard bar.

## Method

Read the live wiring and code on this branch (base `31b4ed7`, post-bh-v0wu); no prototypes —
the question is architectural and the evidence is in the shipped machinery.

1. **Hook wiring.** `git config core.hooksPath` on the hive; read the bd shim
   `.beads/hooks/post-checkout` (markers, timeout, `BD_GIT_HOOK`); `bd hooks list` / `--help`
   for the installed set and shim contract.
2. **False-signal inventory.** Enumerated every bh-internal git op that fires `post-checkout` /
   `pre-push`: grepped `worktree add` and `clean_checkout` call sites across `work.py`,
   `work_show.py`, `work_group.py`, `worktree_merge.py`; read `_do_add`, `clean_checkout`,
   and the bh-nikb verify-dir machinery in `src/beadhive/worktree.py`.
3. **Verb anatomy.** Read `work.py`'s `claim` (guards, identity stamping order) and `submit`
   (history budget, clean-checkout validation, push-then-gate ordering) to measure what a hook
   would have to reproduce.
4. **bh-v0wu interaction.** Read `_open_landing_pr`, the `landing: pr` boundary in
   `merge`/`finish`, `is_landed`, and `config.push_remote` to see where push events now occur.
5. **Reconciler substrate.** Read `src/beadhive/wt_status.py` (the pure worktree↔bead
   classifier) and `prune`/`status` to see what drift detection already exists.
6. **Docs.** `docs/AGF.md` (tenets), `docs/WORKTREES.md` (hook caveats, verify-environment
   contract), the bh-nikb / bh-7k1p triage records.

## Evidence

### 1. Every git hook in this hive routes through bd's marker-managed shims — with hard caps

`core.hooksPath` points at `.beads/hooks` (absolute, in the shared repo config — so hooks fire
for git ops in **every linked worktree**, not just the main clone). Five shims are installed
(`pre-commit`, `post-merge`, `pre-push`, `post-checkout`, `prepare-commit-msg`, shim v1.1.0).
Each shim (`.beads/hooks/post-checkout`): exports `BD_GIT_HOOK=1`, runs
`bd hooks run <hook> "$@"` under `timeout ${BEADS_HOOK_TIMEOUT:-300}`, treats timeout as
success, and the section between the `BEGIN/END BEADS INTEGRATION` markers is **owned by bd**
("Do not remove these markers") — bh content must live outside the markers and survive bd
upgrades. `docs/WORKTREES.md:138-142` adds the two operational caveats: per githooks,
`git worktree add` **fires `post-checkout`**, and **a failing hook fails the checkout itself**.

### 2. bh's own machinery is the dominant source of checkout events — a false-signal firehose

Two bh-internal chokepoints run `git worktree add` (and therefore fire `post-checkout`):

- **`clean_checkout`** (`worktree.py:982-983`, `worktree add --detach`) — invoked from at
  least **9 call sites**: submit validation (`work.py:976`), the two `landing: pr` validations
  (`work.py:1346`, `:1364`), postland tip validation (`work.py:1407`, `:1739`), review-packet
  validate + demo (`work_show.py:228`, `:233`), batch validation (`work_group.py:383`), and
  merger union-tier re-validation (`worktree_merge.py:152`). One submit→merge cycle fires
  several detached checkouts that have nothing to do with anyone *starting* work.
- **`_do_add`** (`worktree.py:559/:564`) — fires on every `claim`/`assign`/`resume`/`start`,
  i.e. **inside the explicit verb itself**. An auto-claim hook would fire mid-`claim`, racing
  the very `bd update --claim` the verb is about to run.

The bh-nikb machinery exists precisely to mark the first class as not-a-seat:
`VERIFY_LEAF_PREFIX = "verify-"` (`worktree.py:73`), per-invocation `verify-<leaf>-<rand6>`
dirs, and a `.bh-verify.json` liveness marker (`:79`, `:863-879`). So the exclusion signal
exists (dir-name prefix + detached HEAD), but *every* transition-driving hook must implement
both checks or validations mutate tracker state — exactly the bh-nikb/bh-7k1p failure family.

### 3. Claim is not a status flip — a hook cannot reproduce its guards or its identity

`work.py:729-788`: claim resolves the actor (`--as` > config identity > `$BH_DEV` > git;
`identity.resolve_actor`, `identity.py:76`), then runs `_guard_open`, `_guard_not_other`,
`_guard_seat` (seat-typed: epic→`disp/`, else `dev/`), `_guard_conventions`,
`_maybe_open_molecule`, `worktree.ensure`, `_stamp` (per-worktree git author + SSH signing),
and only then `bd update --claim` as that actor. Two structural blockers for a hook:

- **Ordering:** `_stamp` runs *after* `ensure()` returns (`work.py:775-776`) — at the moment
  `post-checkout` fires inside `worktree add`, the worktree's agent identity does not exist
  yet. A hook-claim would attribute the claim to whatever ambient git identity happens to be
  set — provenance wrong-by-construction, and `--as` (the seat) is unavailable in a hook.
- **Non-interactive + failure coupling:** hooks can't prompt, and a failing `post-checkout`
  fails the checkout (Evidence 1) — a hook-claim that hits `_guard_not_other` would break
  `git worktree add` itself, i.e. break the claim/resume path it was meant to assist.

And the win is thin by design: `bh work assign`/`claim` already bundle worktree-create +
identity + claim into one verb; the only flow auto-claim covers is raw-git improvisation,
which `docs/AGF.md:3-5` explicitly forbids ("Don't improvise raw `git` … for the lifecycle").

### 4. bh already runs inside hook contexts — and has no re-entrancy marker of its own

`_run_git` (`worktree.py:55-59`) scrubs `GIT_DIR`/`GIT_INDEX_FILE`/`GIT_WORK_TREE` precisely
because "a git hook exports them — without this, `ws wt …` invoked inside a hook would operate
on the wrong repo". So bh verbs demonstrably get invoked from hook contexts today. bd's shim
guards *its own* recursion with `BD_GIT_HOOK=1`, but **bh exports no analog** around its
internal git ops — a transition-driving hook has no way to distinguish "developer checked out
a bead branch" from "bh's own `ensure`/`clean_checkout`/container-refresh did". Any GO here
requires a new `BH_GIT_HOOK`-style env contract threaded through every `_run_git` call first.

### 5. Push-as-submit: git has no client-side post-push hook, and submit's ordering forbids pre-push

Git's only client-side push hook is `pre-push` — it runs **before** transfer; there is no
post-push hook. So "push = submit" must run submit's logic before knowing the push succeeds.
But submit's own ordering comment (`work.py:988-989`) says why that's wrong: "Push BEFORE
set-state so a failed push blocks the gate too (no half-submitted bead)". A pre-push
implementation inverts that invariant by construction. The rest of submit doesn't fit a hook
either: claim-ownership re-check (`work.py:947-952`), history budget (`:968-972`),
clean-checkout validation (`:976`) — the full `just check` from a bare checkout, which cannot
live under the shim's 300s cap (bd's own design concedes this; the bead brief states it) —
and the idempotent gate supersede/reuse bookkeeping (`:997-1023`).

### 6. bh-v0wu made push a *landing-time* event — the "push = done" mapping is now ambiguous

Since bh-v0wu, a bead-branch push legitimately occurs at **three** distinct lifecycle points:

- **Submit** pushes only when the review gate is out-of-process (`gate.startswith("gh:")`,
  `work.py:990-995`) — a review handoff.
- **Landing** (`work.landing: pr`): `merge`/`finish` push the branch and `gh pr create` at the
  shared-integration boundary (`_open_landing_pr`, `work.py:1245`; boundary checks at `:1344`,
  `:1690`) — a **merger-owned** op at the *end* of the lifecycle, after review approval.
- **Manual** developer pushes (backup, CI probes) — no lifecycle meaning at all.

Inferring "submitted" from a push event would misfire hardest on the second class: the landing
push would re-open a review gate on an already-approved bead. bh-v0wu therefore **weakens**
push-as-submit, not strengthens it — the PR world it borrows from has GitHub's server-side PR
state machine to disambiguate; bh's client-side pre-push has only the ref name. The remote is
also now parametric (`config.push_remote`, `config.py:984-987`), with fork/dual-remote targets
on the roadmap (bh-uxam) — further multiplying push-event meanings.

### 7. The reconciler substrate already exists — drift detection is a classifier extension, not a hook

`src/beadhive/wt_status.py` is a **pure worktree↔bead divergence classifier** already shared by
`worktree status` and `prune` "so they never disagree": nine mutually-exclusive states
including `UNMERGED` ("a genuine work-loss signal"), `LANDED_REBASED`, `REVIEW`, `DIRTY`,
`MERGED_ORPHAN`, `ABANDONED`. `is_landed` (`worktree.py:1247-1278`) layers three squash-proof
landed checks; `prune` self-heals orphaned verify dirs (`:1577-1585`). What's *missing* is one
dimension: **claim drift** — a checked-out `wt/bead/*` branch whose bead is unclaimed, claimed
by a different actor, or already closed. The classifier's callers already resolve bead statuses
per managed row (module docstring, items 1-3), so the data is in hand. Two cheap surfaces:

- **Pull-based (no hook):** a claim-drift classification/flag in `wt_status` rendered by
  `bh worktree status` / `doctor` — zero hook-guard cost, fits the existing pattern.
- **Push-based (optional):** a `post-checkout` **warning** outside the bd markers — read-only,
  always `exit 0` (never fails the checkout), skipping `verify-*` leaves, detached HEAD, and
  `BD_GIT_HOOK=1` contexts. Because it mutates nothing, a missed guard costs a spurious
  stderr line, not a corrupted tracker — the guard bar drops from "correctness" to "noise".

### 8. The AGF tension resolves cleanly: detectors are compatible, transition drivers are not

`docs/AGF.md` tenets: lifecycle is driven through explicit, audited, seat-typed verbs with
gates (plan approval, kickoff, review, merge slot) — and the repo's whole guard vocabulary
(`_guard_seat`, `_guard_holds_claim`, gate supersede-by-sha) assumes a *named actor invoking a
verb*. Inferred transitions have no actor, no seat, no gate. But nothing in the tenets forbids
hooks *observing* and *reporting* — that is exactly what bd's data-sync hooks already do.
Meanwhile bh-bh2h.2 (deterministic dispatch: `work next`, escalation primitive) is the
sanctioned path to "fewer agent verbs": make the next explicit verb computable, don't infer it.
A claim-drift signal is a natural input to that epic's `next_payload`/escalation surface.

## Verdict — **NO-GO (auto-claim) · NO-GO (push-as-submit) · GO-WITH-SCOPE (reconcilers)**

- **Auto-claim — NO-GO.** The hook fires *inside* the explicit claim verb (`_do_add`) before
  identity stamping exists (`_stamp` after `ensure`, `work.py:775-776`), so provenance and
  seat-typing are wrong-by-construction; every one of the 9+ bh-internal checkout sites needs
  verify/detached/re-entrancy exclusions or validations claim beads; a failing hook breaks the
  checkout it rides on; and the covered flow (raw-git improvisation) is one AGF forbids anyway
  — `assign`/`claim` already bundle the same steps into one verb.
- **Push-as-submit — NO-GO.** Git has no client-side post-push hook, and pre-push inverts
  submit's "push before set-state, no half-submitted bead" invariant (`work.py:988-989`);
  submit's clean-checkout validation cannot fit the shim's 300s cap; and bh-v0wu **weakened**
  the mapping decisively — push is now also the merger's landing-time op (`_open_landing_pr`),
  so the event is ambiguous among review handoff, landing, and backup, with the worst misfire
  re-opening review on an approved bead.
- **Reconcilers — GO-WITH-SCOPE.** Read-only drift detection is AGF-compatible (hooks as
  detectors, never transition drivers) and nearly free: the `wt_status` classifier already
  computes worktree↔bead divergence for `status`/`prune`; adding a claim-drift dimension is a
  classifier extension with the data already resolved per row. Scope fence: **no hook ever
  mutates bead state**; pull-based surfaces first; any `post-checkout` warn is best-effort,
  always-exit-0, outside bd's markers, and skips `verify-*`/detached/`BD_GIT_HOOK` contexts.

**Concrete blocker (both NO-GOs):** transitions in bh are actor-attributed, seat-typed, gated
verbs; a git event carries no actor, no seat, and fires overwhelmingly from bh's *own* internal
git ops (9+ checkout sites, 2 push sites), so every inferred transition needs a guard stack
(verify exclusions, `BH_GIT_HOOK` re-entrancy env, non-interactive identity) that re-implements
the verb it was meant to replace — with worse failure modes (a failing hook fails the checkout).

## Recommendation

1. **Do not file molecules for auto-claim or push-as-submit.** Record both NO-GOs by closing
   this spike's decision path with this artifact; the AGF explicit-verb premise stands. Revisit
   push-as-submit only if the lifecycle ever moves to a server-side state machine (GitHub PR
   events per bh-uxam/bh-aa5b territory) where "push" has a disambiguating server context —
   never client-side pre-push.
2. **Fold the reconciler into existing work rather than a new epic** (the bead brief's own
   triage option). Smallest honest shape, in priority order:
   - a **claim-drift dimension in `wt_status`** (checked-out bead branch vs. bead
     assignee/status) surfaced by `bh worktree status` and `bh doctor` — pure classifier
     extension, no hooks;
   - feed the same signal into **bh-bh2h.2's `work next` / escalation payloads** (drift is a
     "hand up, don't improvise" trigger for cheap-model seats);
   - only then, optionally, a **passive `post-checkout` warn** outside bd's shim markers, gated
     on the scope fence in the verdict — a quick-fidelity bead, not a molecule.
3. **If any transition-adjacent hook work is ever attempted**, land the `BH_GIT_HOOK`-style
   re-entrancy env contract around `_run_git` first (Evidence 4) — it is the missing primitive
   every direction here tripped over, and it is useful to reconciler noise-suppression too.
