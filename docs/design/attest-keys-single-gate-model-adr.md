# ADR: Attest keys as the single gate model

**Status:** accepted (operator-reviewed, 2026-10-12). This is Amendment 2 of
[the Attested Green ADR](attested-green-adr.md#amendment-2--attest-keys-as-the-single-gate-model).
**Revision r2:** the catalog lives in HQ only (per-hive `work.attest.keys`); no repo manifest. A
runtime drift check comparing the live HQ catalog against the repo's justfile `attest-*` recipes and
the checked-in `KEY_RECIPES` / docs table is the guard. Main-reaching phases require every key.
**Amends:** `docs/design/attested-green-adr.md` (adds Amendment 2; Amendment 1's rules stand unchanged).
**Supersedes:** bh-uj5td / bh-uj5td.1 (phase slots, reduced to its simple core),
bh-gd4tj (carry at every transition + push predicate), bh-i6gua and its spike children (this ADR
renders the decision bh-i6gua.5 would have made). **Prerequisite:** bh-x9hco (lint-time drift check
on branch `wt/bead/issue/bh-x9hco` at 44fbbbb4, plus publishing the live `hq-storage` key).

## Operator decisions (2026-10-12)

1. HQ per-hive `work.attest.keys` is the only catalog; there is no repo manifest.
2. Main-reaching phases require every active key.
3. Fleet defaults are `work.gate.model: legacy` and `push.accept_attested_green: false`; the hive
   bh opts in at cutover.
4. The work lands as a container-less workstream parent, bh-gt7t9, with sub-epics bh-xsiin,
   bh-kpv0d and bh-xg40p.
5. The carry and profiles slices run on Opus.
6. The closure-certification code is deleted.

## 1. Context

Gate logic has grown one mechanism at a time. On main at 5bfecadd, for one hive:

| Mechanism | Where | Status |
|---|---|---|
| Per-phase full command strings | `work.validate.<phase>` / `validate_cmd` via `config_work_settings.validate_cmd` (`config_work_settings.py:73-92`); live bh: `molecule`, `merge-main`, `push-main`, `postland` = `just check-all-native`; `submit`, `merge`, `union`, `validate_cmd` = `just check-native` (`hq/fleet.yaml:271-278`) | active |
| Selective key evaluator | `selective_validation.run` (`selective_validation.py:320-526`) | active at check, submit, review, batch submit/merge, merge, finish pre-land, release `--if-needed` |
| Post-land command | `work_merge.py:364-381`: `clean_checkout(validate_cmd(...,"postland"), reuse=True)` | never selective |
| Union-merge command | `work_merge.py:~1089` into `worktree_merge.py:~265` | never selective |
| Pre-push aggregate lookup | `scripts/main-push-gate.sh` into `bh hive hook push-main` into `prepush.check_push_main` (`prepush.py:~308-420`). It does an exact-tree `green_verdict` for the literal `just check-all-native` command only. A miss runs the full gate inline (measured at 1529 s / 25.5 min on 2026-10-12). | active; ignores per-key proof |
| Aggregate predicate | `selective_validation.all_keys_green` (`:117-134`), used only by `release.py:541,556`; no drift check | narrow |
| Full gate back-fills key proof | `record_full_gate_keys` (`:137-158`) for `FULL_GATE_PHASES` | active; assumes the catalog partitions the full gate |
| Hand-listed full gate | `justfile:157` `check-all-native` deps; `scripts/check_attest_catalog.py` checks the leaf partition against hard-coded `KEY_RECIPES` | active |
| Closure certification ("selective_ci") | `tests/selective-ci-policy.toml`, `tests/closures.toml`, `scripts/test_closure_*.py`, `scripts/test_closures.py`, docs `SELECTIVE-CI-OPERATIONS.md`, `TEST-CLOSURES.md`, `TEST_PROCESS_POLICY.md:111-129` (about 7,000 lines of scripts and tests) | 0 certified closures; unwired from `bh work`, but four scripts still run inside `architecture-structural-check` and `validation-evidence-refresh` on every gate |
| Trivial/semantic selection | `trivial_selection`, `semantic_selection` | opt-in, disabled on bh |
| Escape hatches | `--override-validation` (one-shot, human-authorised, audited), `work.validation_bypass` (hive-only, audited) | keep |

Consequences we pay today:

1. **Every push re-runs about 25 minutes**, even when every key is current or validly carried for the
   pushed tree. The push hook only recognises the aggregate command verdict. Amendment 1's
   Decision 4 extension already authorises accepting per-key proof at `push-main`; it was never wired.
2. **Carries do not survive successive lands.** `carry_key_verdict` (`validation_ledger.py:885-936`)
   reads its source at `:894-897` through `verdict()`, which never reads the carried namespace. So it
   only accepts a key that actually ran on the receipt's base tree. After land N carries key K,
   land N+1 finds no current K at its base and re-runs K. Unaffected keys re-run every other land.
3. **The HQ catalog drifts from the repo silently.** The live HQ catalog is missing `hq-storage`
   (bh-x9hco), so `hq-storage-check` never runs on any selective path, and `all_keys_green` ignores
   it. Nothing at runtime notices.
4. **There is no phase vocabulary for keys.** Phases differ only by command string. `run` has no
   phase parameter and ignores `<phase>-main` overrides.

## 2. Decision

Adopt one standard. The HQ attest-key catalog is the only description of what "tested" means:

1. **Keys are the only test targets.** The per-hive HQ config (`work.attest.keys` in
   `managed_repos`) is the single catalog source. The repo has no second catalog copy.
   - The full gate is *every key*.
   - `check-all-native` is generated from the key recipes, not hand-listed. It runs every
     `attest-*` recipe, a set the drift check proves equals the HQ catalog.
   - A fast check is a *named key subset*.
   - A hive with no catalog is the degenerate one-key case and keeps its `validate_cmd`.
2. **Each lifecycle gate is a profile, not a command string.** A profile names the keys the phase
   requires (default: every key). Execution is always selective, with exact-tree reuse and carry.
3. **One evaluator at every boundary:** `selective_validation.run`, given a phase. When the hive
   uses the key model, post-land, union merges and the pre-push hook stop shelling out to a full
   command string.
4. **One aggregate predicate** (`attested_green`) decides push, post-land acceptance and release.
   It holds when all of the profile's required keys are current-green or validly carried *for the
   exact tree*, and the runtime catalog drift check passes. Anything else fails closed to running
   keys, or, on drift, to the legacy full command.
5. **Carries reach back to their root run (high-water source discovery)**, keeping the original
   source time and TTL. Amendment 1 already says carrying onward must work this way ("diff T0
   against T2 directly"); today's code never finds T0.
6. **Retire** the following:
   - the closure-certification machinery;
   - the aggregate-only push lookup;
   - hand-listed `check-all-native` deps;
   - `work.validate.<phase>` strings for key-model hives. They are retained only as the no-catalog
     and drift fallback.

   `--override-validation` and `validation_bypass` remain as audited escape hatches, not routine
   policy. Pants stays an optional `build.impact` backend.

## 3. The model, made precise

### 3.1 Terms

- **Catalog** `C`: the hive's HQ `work.attest.keys`. It is an ordered list of
  `{name, cmd, policy, enabled, selectors}` (unchanged `AttestKeyConfig`). `active(C)` is the
  enabled keys, plus keys whose `disabled_until` has passed.
- **Repo key declaration** `D@T`: what the repo at tree `T` says the keys are. It is the set of
  justfile `attest-*` recipes at `T`, plus the checked-in `KEY_RECIPES` and the
  `docs/ATTEST-KEYS.md` table, as bh-x9hco's checker reads them. This is a declaration that is
  *checked against* `C`, not a second catalog.
- **Profile** `P(phase)`: a project-owned set of key names: `all`, a named subset, or an explicit list.
  Every lifecycle phase is configurable; subset names, strictness and policy belong to each project.
- **Verdict state** of key K at tree T is one of three, unchanged from Amendment 1:
  - `current`: K ran on T; green or red.
  - `carried`: a green verdict transferred by receipt from a root run.
  - `absent`: neither, with a reason.

### 3.2 Phases (closed set)

`check`, `submit`, `review`, `merge` (bead into container), `merge-main` (bead onto main),
`molecule` (finish pre-land), `postland`, `union`, `release`, `push-main`. `bh config validate`
rejects unknown phase names. `<phase>-main` resolution keeps today's semantics: a `merge-main`
profile wins for a merge onto main, else `merge`.

### 3.3 The evaluator

`selective_validation.run(entry, cfg, *, phase, head_rev, base_rev=None, runner, full=False)`:

1. If the hive is on `work.gate.model: legacy`, or has no catalog, behave exactly as today, using
   the command string for the phase. This is the rollback path.
2. Run the **drift check** (3.6). On drift, print the difference and run the legacy command for
   the phase (`validate_cmd(cfg, entry, phase)`). Record no key proof. Fail closed.
3. Compute the required set `R = P(phase) ∩ active(C)`. Keys outside `R` are not evaluated. They
   are reported as `not required by <phase>` and gain no proof.
4. For each K in R, in order:
   1. exact-tree reuse: a current green or valid carried verdict at `head`'s tree, same `cmd_hash`;
   2. else high-water carry (3.5);
   3. else `pending`.
5. If anything is pending, run `precheck` once, then the pending keys cheapest-first (unchanged).
6. The boundary is blocked if the precheck is red, any key is red, or a required key is absent or
   exits 75 (unchanged rules).

Trivial and semantic selection keep their existing opt-in seams inside step 4. Keys they skip
remain unproven, so they can never satisfy the aggregate predicate.

### 3.4 The aggregate predicate

```text
attested_green(entry, cfg, rev, phase) :=
      model == keys
  and catalog_ok(entry, cfg, rev)                                           # 3.6
  and ∀ K ∈ P(phase) ∩ active(C):
        state(K, tree(rev)) ∈ {current-green (qualifying), carried (valid on read)}
  and no required K is current-red at tree(rev)
  and work.always_run passes (as green_verdict does today)
```

It replaces `all_keys_green`, which becomes a thin alias during migration.

- `push-main` exits 0 if and only if the predicate holds.
- Otherwise the hook runs the evaluator for `push-main` (3.3), which runs only the missing keys.
- Every exception, unresolved tree, unreadable ledger, or failed drift check evaluates to `False`.

**Hard limit (correctness, not a default):** the `push-main`, `release` and `postland` profiles
must equal *every active key*, and `bh config validate` rejects a subset there. Every other profile
is freely configurable. A key can still be disabled only through the existing `enabled: false` +
`disabled_reason` + optional `disabled_until` route, which is already audited and warned on by
`bh doctor`.

### 3.5 High-water source discovery for carries (the chaining fix)

Amendment 1 says: "A carried verdict is never itself a source. To carry K onward to T2, the resolver
diffs T0 against T2 directly." This ADR implements exactly that and changes no rule.

- **Root run.** The source of any carry is always a *current green* run of K on tree T0, with the
  same `cmd_hash`, within the TTL measured from that run's `at`.
- **Discovery.** Candidate roots for K at head H are tried in this order:
  1. a current green K on the receipt base tree (today's only candidate);
  2. the `source_tree` / `source_run_id` of a *valid* carried K record on the base tree. This is
     the high-water checkpoint: the base's carry points at its root.
  3. the most recent current green runs of K with the same `cmd_hash` within TTL that the ledger
     knows of. This is bounded: the newest `work.gate.carry.max_roots`, default 3.

  A candidate whose commit cannot be resolved locally is skipped, never treated as a pass.
- **Proof.** For each distinct root T0, the evaluator resolves `ImpactReceipt(T0 → H)` directly:
  one resolve per distinct root, shared across keys. K carries only if all of these hold:
  - the receipt has no `fallback_reason`;
  - it lists K as unaffected;
  - none of the five fail-closed rules fired *across the whole span T0 → H*.
- **Record.** The carried record keeps `source_tree = T0`, `source_run_id`, and
  `at = T0's run time`. TTL still runs from when K last actually ran, so carrying never refreshes age.
- **Tests.** `tests/test_validation_key_ledger.py::test_never_chains_from_a_carried_hop` stays true:
  a carried record is still never a *source*. A new test proves that a carry *through* a carried
  base reaches the root and diffs root → head.

TTL (P1D default) bounds how far back a root can be. A command change alters the `cmd_hash`, and a
catalog change fails the drift check; either invalidates every root.

### 3.6 Catalog drift check (runtime guard)

**Single source:** the hive's HQ catalog. There is no repo manifest and no second catalog copy.

- **Lint time (bh-x9hco, 44fbbbb4 on its branch).** `scripts/check_attest_catalog.py` compares the
  justfile `attest-*` recipes and the `docs/ATTEST-KEYS.md` table against `KEY_RECIPES`. It runs
  in CI without HQ. `scripts/check_native_impact_map.py` compares the live configured catalog
  against the same set.
- **Runtime (this ADR), `catalog_ok(entry, cfg, rev)`.** This is the guard the predicate depends on.
  - It runs the hive's opt-in `work.attest.drift_check` command at `rev`'s checkout. bh passes the
    live HQ catalog as JSON on stdin (name, cmd, enabled).
  - The command stays opaque to bh, so other hives can supply their own.
  - For bh, the command is a mode of bh-x9hco's checker. It compares the live catalog's key set and
    `cmd`s (`just attest-<name>`) against the tree's justfile `attest-*` recipes, `KEY_RECIPES` and
    the docs table.
  - Exit 0 means no drift. Any other exit, a timeout, or an unset command while
    `work.gate.model: keys` counts as drift.
  - The result is cached in the ledger keyed by `(tree, catalog digest, drift-check cmd_hash)`, so a
    repeat costs nothing. Any HQ catalog edit changes the digest and forces a re-check.
- **Drift handling.** Drift makes the predicate false, and the evaluator runs the legacy phase
  command (3.3 step 2). This closes today's hole, where a key absent from HQ (`hq-storage`) is
  silently unenforced.

### 3.7 `check-all-native` = every key

`check-all-native` stops hand-listing leaves. It runs `just attest-<key>` for every `attest-*`
recipe in the justfile, in declaration order.

- The recipe set is discovered with `just --summary`, which needs no extra file.
- Because the drift check proves that the `attest-*` set equals the HQ catalog, "every recipe" is
  "every key".
- `check-attest-catalog` keeps the leaf-partition rule: each leaf is owned by exactly one key, and
  the union of all leaves is the complete native gate.
- `check-native` (the fast profile) becomes a named key subset rather than a second hand-listed
  leaf list.

### 3.8 Config schema sketch

```yaml
work:
  gate:
    model: legacy            # legacy | keys. Fleet default legacy (today's behaviour). Per-hive
                             # switch; setting legacy is the rollback.
    profiles:                # consulted only when model: keys
      default: all           # every phase not listed requires every active key
      check: fast            # a subset name or an explicit list
      # Every phase accepts a named subset or key list, or inherits default.
      # This example's subset name is not a predefined strictness level.
    subsets:
      fast: [docs, unit, architecture-contracts]
    carry:
      high_water: true       # default true under model: keys; false = today's single-hop source
      max_roots: 3
    push:
      accept_attested_green: false  # fleet default false; hive bh sets true at cutover
  attest:
    drift_check: "uv run python scripts/check_attest_catalog.py --live-catalog -"  # bh example; required for model: keys
    keys: [...]              # unchanged AttestKeyConfig list: the single catalog source (HQ)
    impact: {backend: paths, on_unresolved: fallback}   # unchanged; pants stays an optional backend
  validate:                  # used when model: legacy, when there is no catalog, or on drift
    postland: just check-all-native
```

The contracts live in `src/beadhive/modules/config/contracts.py`, beside `AttestConfig` (`:712`)
and `WorkConfig` (`:856`). Validation rejects:

- unknown phases;
- unknown subset names;
- unknown key names in a profile;
- a non-`all` profile for `push-main`, `release` or `postland`;
- `model: keys` without `attest.drift_check`.

## 4. Migration

Each step lands on main independently and is useful on its own.

1. Land this ADR as Amendment 2 (docs only).
2. bh-x9hco lands (lint drift check), and the operator publishes the 10-key HQ catalog including
   `hq-storage`.
3. Add the runtime drift check and the `attested_green` predicate. Nothing changes until they are
   enabled.
4. The push hook accepts `attested_green` behind `work.gate.push.accept_attested_green` (opt-in).
   On a miss it still runs the full command, as today.
   - **This is the roughly 25-minute-per-push win.**
   - A selective land onto main already ran or carried every key at the landed tree, so the
     following push becomes a lookup.
5. Add high-water carry discovery, which means fewer re-runs at every land.
6. Add phase profiles, the `work.gate.model` switch, and the `phase` argument through every call
   site.
7. Post-land, union, the push-miss path and release go through the evaluator when `model: keys`.
8. Generate `check-all-native` from the `attest-*` recipes, and retire the closure-certification
   machinery.
9. Docs sweep. The operator then switches bh to `work.gate.model: keys` and trims `work.validate`
   to the fallback strings.

Interim policy until step 9: plain selective `bh work submit` / `merge`, with no routine
`--override-validation`.

## 5. Rollback

- **Per hive, one setting.** `work.gate.model: legacy` restores today's exact behaviour: command
  strings at post-land, union and push, and the aggregate-only push lookup.
  - No ledger migration is needed. A root-sourced carry is the existing record schema; its receipt
    base equals its `source_tree`.
  - The current `key_verdict` checks read such a record as valid, or as `absent`, which only costs a
    re-run.
- **Narrower switches:**
  - `work.gate.push.accept_attested_green: false` reverts the push hook only.
  - `work.gate.carry.high_water: false` restores single-hop sources only.
  - `work.attest.impact.backend: native-full` invalidates every key on every change.
- Removing the catalog restores the single `validate_cmd` path, as today.

## 6. What is retired

| Retired | Replacement | Kept as |
|---|---|---|
| `work.validate.<phase>` full-command strings as the gate for catalog hives | phase profiles | fallback for `legacy`, no-catalog hives, and drift |
| Aggregate-only push lookup (`push_main_cmd` must equal `gate_cmd`) | `attested_green(push-main)`, plus the evaluator on a miss | legacy path |
| Hand-listed `check-all-native` leaf deps | a recipe that runs every `attest-*` recipe | none |
| Closure certification, listed below | attest keys | git history; `beadhive_pants.impact_selector` is detached from the certification-script import (`impact_selector.py:172`) |
| `record_full_gate_keys` as the normal way to seed key proof | per-key runs and carries | legacy and no-catalog bridge only |
| Routine `--override-validation` | plain selective submit and merge | audited one-shot escape hatch; `validation_bypass` unchanged |
| bh-uj5td slot unions and multi-selector slots | one profile = one key set; one receipt per root | not built |

The closure-certification surface being removed:

- `tests/selective-ci-policy.toml`, `tests/closures.toml`;
- `scripts/test_closures.py` and `scripts/test_closure_{certification,shadow_policy,shadow_verifier,promotion_policy,operational_report}.py`;
- their tests and the `test-closure*` recipes;
- the four closure jobs in `architecture-structural-check` / `validation-evidence-refresh`;
- `docs/SELECTIVE-CI-OPERATIONS.md`, `docs/TEST-CLOSURES.md`, and `TEST_PROCESS_POLICY.md:111-129`.

Not retired:

- the Pants impact backend (optional `build.impact`);
- the trivial and semantic selection seams (opt-in, disabled on bh);
- `validate_subset` (developer loop only);
- `precheck`;
- `KEY_RECIPES` and the docs table, which are the repo-side declarations the drift check reads.

## 7. Safety argument (Attested Green holds)

1. **Proof follows project policy.** Every key required by the configured phase profile must
   be qualifying current green or validly carried at the exact tree, and the catalog drift check
   must pass. Narrowing a profile is an explicit project policy choice; omitted keys acquire no
   proof and a later broader profile still requires their evidence. Advisory defaults may keep
   publication boundaries broad and feedback loops smaller; no boundary is locked to a subset.
2. **Exact tree.** The predicate reads `(tree, cmd_hash)` slots for `tree(rev)`. A rebase, a
   near-miss, or a subtree match still runs fresh.
3. **Carries obey every Amendment 1 rule.**
   - The source is always a current green run.
   - A carried record is never a source.
   - The receipt spans root to head directly; hops are not composed.
   - The five fail-closed rules apply to the whole span.
   - Age and TTL come from the root run.
   - Red, unknown and fallback verdicts never carry.

   High-water discovery only adds *candidates*, and each still needs its own full-span receipt.
4. **Catalog drift fails closed.** Each of these makes `catalog_ok` false, which makes the predicate
   false and makes the evaluator run the legacy full command:
   - a key in the repo's `attest-*` recipes but absent from HQ, or the reverse;
   - a renamed key;
   - a changed command;
   - an unset, failing or timed-out drift-check command.

   This closes today's hole, where `hq-storage` is silently unenforced. Because there is one
   catalog source (HQ) and one check against it, there is no second copy to fall out of step.
5. **Profiles cannot shrink what reaches main.** Main-reaching phases are validator-locked to every
   active key. Subsets are allowed only for inner-loop phases, whose results are re-checked at land.
6. **Uncertainty costs work, never proof.**
   - Unowned paths, global inputs, resolver errors and timeouts still invalidate every key
     (`on_unresolved: fallback`).
   - A missing commit for a root candidate drops that candidate.
   - Any exception in the push lookup returns False and runs keys.
7. **The trust model is unchanged.** The ledger stays a host-local cache, not an authorisation
   boundary (Decision 4 / bh-ku9n9.17).
   - The catalog stays in HQ, where it is today.
   - Key command bodies stay in the repo's justfile, as today.
   - The drift check is opaque and runs from the evaluated checkout. A branch that edits the
     checker already had the power to edit key bodies, and both go through review.
8. **Escape hatches stay audited.** Override and bypass never create green key proof and never
   satisfy `push-main` (unchanged).

## 8. Relationship to open work

| Bead | Disposition |
|---|---|
| bh-x9hco (in progress) | Prerequisite. It provides the lint drift check (44fbbbb4 on its branch) and the live `hq-storage` key. Not duplicated: the runtime guard adds a `--live-catalog` mode to the same checker. |
| bh-gd4tj (deferred) | Absorbed: push predicate (`predicate`, `push`), post-land/union selective (`postland`), release (`release`), 3-bead carry test (`carry`). Superseded. |
| bh-uj5td, bh-uj5td.1, bh-nt02i, gate bh-q90tl | Simple core absorbed: phase profiles, high-water checkpoint, root time/TTL, and the fail-closed list. Slot unions and per-selector no-op records are not built. Superseded. |
| bh-i6gua + .1-.5 (deferred spike) | Decision rendered by this ADR: adopt; the global gate is the degenerate key set; retire closures. Superseded; the spikes are not run. |
| bh-phlcd (deferred) | Unaffected. The semantic seam stays inside the evaluator, opt-in. |
| bh-s02y2 (open bug) | Partly helped: a rebased candidate can carry from its roots. Leave open; re-measure after `carry`. |
| bh-2znxs (decision) | Resolved by the `dedupe` bead per the recommendation: `root-composition` runs `-m "not integration"`; the `integration` key's selectors add both hq-storage package dirs. |
| bh-38zpi | Out of scope: later perf narrowing inside one key. |
| bh-1vi8i | Out of scope: categories could later name subsets. No conflict. |
| bh-7s67j | Orthogonal: the broker plugs into the evaluator's pending-key runner. No conflict. |
| bh-ku9n9.5 / .17, bh-0jndj | Landed foundations (named push-main phase, exact-tree landing reuse, `just attest --if-needed`). Built on, not changed. |
