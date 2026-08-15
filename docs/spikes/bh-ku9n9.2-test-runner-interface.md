# Spike `bh-ku9n9.2` — can bh get per-test results without a config string per hive per framework?

**Bead:** `bh-ku9n9.2` · **Seat:** `dev/spike` · **Type:** research-only (no product code)
**Feeds decision on:** [`bh-ku9n9.4`](#recommendation--the-three-options-for-bh-ku9n94) —
*decision: adopt a test-runner shim/plugin, a built-in attestation provider, or per-hive config*
**Parent epic:** `bh-ku9n9` — *Attested Green*

## Question

**Is a registered test-runner shim/plugin worth building, so that bh can read machine-readable
test results out of any hive without that hive declaring more configuration?**

Concretely, GO/NO-GO on: bh grows a registry of per-framework adapters (Rust preferred, Go
acceptable, Python worst case, TypeScript excluded) that discover a hive's test framework and
drive it for the epic's three capability tiers — (1) machine-readable results, (2)
failure-scoped re-run, (3) per-test coverage attribution.

**What this is critically NOT asking.** Not whether the attestation ledger should exist —
`src/beadhive/validation_ledger.py` shipped it under bh-dfx0. Not how it is keyed — the epic
settled that (tree, not sha). Not whether the capability tiers are the right decomposition —
they are the epic's design. Only: *who produces the per-test record, and what does that cost.*

The epic's **INVARIANT** bounds every answer below: *no capability tier may ever ADD a pass a
full run would not have produced.* An option that lets bh run less than the hive's gate runs,
and still call the result an attestation, is disqualified regardless of how cheap it is.

## Method

Read, in this repo (worktree at `790b084`):

* `src/beadhive/validation_ledger.py` — the shipped ledger: key, TTL, cap, trust model.
* `src/beadhive/config.py:1876` `validate_cmd()` — how `work.validate.<phase>` resolves.
* `src/beadhive/validate_probe.py` — bh's existing justfile-graph resolver (bh-l44i).
* `src/beadhive/plugins.py` — the shipped plugin seam (one frozen dataclass, one static list).
* `justfile` — the `check` / `check-all` / `test` / `test-integration-land` / `cov` recipes.

Surveyed upstream sources (fetched, not recalled): nextest's JUnit and libtest-JSON docs,
pytest's `cache.rst` (`--lf` semantics), Go's `cmd/test2json` doc comment and `build-cover.md`,
coverage.py's `contexts.rst`, cargo-llvm-cov's README, vitest's `reporters.md`, the
OpenTelemetry `test.*` attribute registry, the crates.io search API, and the GitHub API for the
`ctrf-io` org and `trunk-io/analytics-cli`.

Measured, on this repo, on this machine — no assumed numbers:

* tree/commit churn from `git log --pretty=%T | sort -u`;
* per-test record size across four encodings over the real 4,934 collected node ids;
* wall time and result-file size for the fast suite (`-m "not integration"`, `-n auto`, under
  `scripts/hermetic.sh`) with and without `--junitxml`, baseline run repeated to bound noise;
* JUnit-XML ingest cost (wall + peak RSS) with Python's stdlib `xml.etree.ElementTree`.

No mini-spike shim was prototyped. Evidence 1 disqualified the shim shape before a prototype
could inform the choice — see the Verdict.

## Evidence

### 1. `validate_cmd` is a pipeline, not a test invocation — this is the load-bearing fact

`justfile:53` — `check: lint lint-md license-check test`
`justfile:120` — `check-all: require-bd lint lint-md license-check (test FAST)
test-integration-land demo-local-loop`

The epic's own measured breakdown of the 371.4s gate is `test FAST` 108.9s,
`test-integration-land` 134–141s, `demo-local-loop` 122.9s — *"three near-equal thirds, no
hotspot"*. Roughly two thirds of the gate is not a single pytest invocation, and
`demo-local-loop` is not pytest at all.

A shim that **owns the run** (`adapter.run(pytest)`) therefore has exactly two outs, and both
are dead ends:

* run only the framework it adapts → the attestation is written from a run that skipped
  `lint`, `license-check` and `demo-local-loop`. That is the INVARIANT violated in its
  purest form: the record asserts more than was proven.
* reproduce the pipeline → the adapter must be told, per hive, what the pipeline is. That is
  Option C wearing a plugin registry as a hat.

There is no third out, because the pipeline is the hive's knowledge and nothing else has it.
This is why the survey below never needed a prototype to settle the question.

### 2. bh already resolves runners — deliberately to classify, never to drive

`validate_probe.py` walks the justfile recipe graph transitively (`check: lint lint-md test`
→ `uv run pytest`) to answer "does this command actually run tests". Its docstring records
the design constraint verbatim: it is **tri-state**, and anything it cannot fully resolve —
`make check`, a bespoke script, an empty command — returns `None` and *never warns*, because
*"an unresolvable command is exactly the case a false verdict would repeat bh-05w7's mistake
in."*

So the "can detection replace declaration?" question already has a shipped, load-bearing
answer in this codebase, and it is the same one bh-d0kb set for `toolchain`: **detection may
inform and may suggest, never enable.** Any option that silently switches behaviour on a
detected framework contradicts a precedent this repo has already paid for twice.

### 3. JUnit XML is the real lowest common denominator — and nobody agrees on its schema

| Runner | Machine-readable output | Needs a plugin? |
|---|---|---|
| pytest | `--junitxml=PATH` | no — built in |
| cargo-nextest | `[profile.X.junit] path = …` in `.config/nextest.toml` | no — but config-file only, **not** a CLI flag |
| `cargo test` | libtest JSON | **unstable** upstream (rust-lang#49359) |
| go test | `-json` (test2json) → `gotestsum --junitfile` / `go-junit-report` | JSON built in; XML needs a tool |
| vitest | `--reporter=junit` (writes `.vitest/junit/output.xml`) | no — built in |
| jest | `--json`; JUnit via `jest-junit` | XML needs a plugin |
| dotnet / Maven / Gradle / RSpec | trx→junit, surefire, `RspecJunitFormatter` | varies |

Nextest's own documentation states the schema problem outright: *"There are several slightly
different formats all called 'JUnit' or 'XUnit'. Nextest adheres to the Jenkins XML format"*
(llg.cubic.org/docs/junit). So JUnit XML is universal in *availability* and only approximate
in *shape* — which is fine for the three fields an attestation needs (test id, status,
duration) and unreliable for anything richer.

Note the second column carefully: for **pytest, nextest and vitest the report is turned on
inside the project's own already-maintained config** (`addopts` / `.config/nextest.toml` /
`vitest.config.ts`), not by a flag bh would have to inject. That is a config surface that
already exists and is already owned by the hive.

### 4. The newer cross-language JSON standards are not ready, or are the forbidden language

* **libtest JSON (Rust)** — upstream unstable since 2019 (rust-lang#49359). Nextest's port is
  experimental behind `NEXTEST_EXPERIMENTAL_LIBTEST_JSON=1`, and its own page still reads
  `## Format specification` / `TODO`.
* **CTRF (Common Test Report Format)** — the most plausible candidate, and it does not survive
  contact. The `ctrf-io` org has 23 repos; the top 18 by stars are 16 TypeScript, 1 C#, 1 Go.
  Highest star count is 371 (`github-test-reporter`, TS). There is no first-party pytest
  reporter. And it ships **`junit-to-ctrf`** — CTRF's own on-ramp for the rest of the world is
  JUnit XML. Adopting CTRF means adopting a TypeScript tooling ecosystem the operator has
  excluded, to reach a format whose input we would already be parsing.
* **OpenTelemetry `test.*` semconv** — Development status, and exactly four attributes
  (`test.case.name`, `test.case.result.status` ∈ {pass, fail}, `test.suite.name`,
  `test.suite.run.status`). A naming vocabulary worth stealing for the record's field names.
  Not a transport, and not a producer — nothing emits it from a test run today.
* **TAP** — genuinely universal and genuinely too thin: a stream of `ok/not ok N - name` with
  no timing and no stable node id. It answers "did it pass", which we already know from the
  exit code.

### 5. We would not have to write the parser — and we also do not need a binary

Rust already has mature JUnit-XML readers, which answers the bead's "must we write it?"
question with *no*:

* `quick-junit` 0.7.0 — **10,941,675** downloads — *"Data model, serializer, and deserializer
  for JUnit/XUnit XML"* (this is nextest's own crate).
* `junit-parser` 1.6.0 — 122,572 downloads — *"Rust library to parse JUnit XML files"*.
* `ctrf-rs` 0.2.0 — 95,755 downloads.

And the market has already voted on the ingest side: `trunk-io/analytics-cli`, the commercial
flaky-test product's uploader, is **Rust** and ingests JUnit XML. Datadog and Buildkite ship the
same shape. Every polyglot test-analytics product converged on *parse JUnit XML*, not on *drive
each framework*.

Measured cost of doing the same parse in the process bh already has, on this repo's real
report: see Evidence 9.

### 6. `--last-failed` is the wrong mechanism for tier 2; explicit node ids are the right one

pytest's `--lf` reads `.pytest_cache` — *implicit state from whatever ran last in this
rootdir*. It is keyed to nothing, it is per-rootdir (a seat worktree has its own cache, and
this spike's own measurement runs used `-p no:cacheprovider` and had none at all), and it is
invisible to bh. An attestation built on "whatever pytest happened to remember" is exactly the
laundering hazard the epic names.

Every runner instead accepts an **explicit selector**, which is portable, auditable, and
tree-keyable because *bh* holds the list:

| Runner | Explicit failure-scoped selector |
|---|---|
| pytest | `pytest <nodeid> <nodeid> …` |
| go test | `go test -run '^(TestA\|TestB)$'` (and `-skip`, Go 1.21+) |
| cargo-nextest | `cargo nextest run -E 'test(=name)'` |
| jest | `jest --onlyFailures` / `-t` |
| vitest | `vitest <file> -t <name>` |

The selector *syntax* is trivially normalizable. What is **not** normalizable is how a hive's
pipeline forwards a selector into its runner: `just check` fans out to `lint lint-md
license-check test`, and only the hive knows that `test` is the leg that takes node ids and
that `lint` must still run. **Tier 2 therefore costs exactly one template string per hive no
matter which option is chosen** — that is a floor, not a differentiator.

### 7. Tier 3 (per-test coverage) is confirmed rare outside Python

| Ecosystem | Per-test attribution | Mechanism |
|---|---|---|
| **Python** | **first-class** | coverage.py dynamic contexts, `[run] dynamic_context = test_function`, driven by `pytest-cov --cov-context=test` |
| Rust | possible, not native | LLVM source-based coverage has no contexts; only works because nextest runs **one process per test**, so `LLVM_PROFILE_FILE` patterns yield one `.profraw` per test — O(tests) files plus a report pass |
| Go | not native | `-coverprofile` is per-package; Go 1.20 `GOCOVERDIR` is per-binary-*run*. Per-test = one run per test |
| JVM | not native | JaCoCo is per-JVM; per-test needs a process per test (or research RTS tooling: Ekstazi, STARTS) |
| Node | not native | `NODE_V8_COVERAGE` is per-process; jest `--coverage` has no per-test contexts |

The epic's working assumption is **confirmed**: outside Python, tier 3 is not a flag, it is an
execution-model change (process-per-test) plus O(n) artifacts. It should not shape v1 at all.

### 8. Attestation size — measured over this repo's real 4,934 node ids

| Encoding | Bytes | gzip -9 |
|---|---:|---:|
| raw node-id list (`\n`-joined) | 378,737 | 72,882 |
| per-test JSON `{"n":id,"o":"p","t":0.12}` | 502,103 | 75,593 |
| JUnit XML from a real fast-suite run (4,877 testcases) | 573,785 | 95,038 |
| **digest-only green attestation** | **242** | — |

**The rows are not like-for-like.** Rows 1–2 are computed over all **4,934 collected** node
ids; row 3 is a real report from the **fast set — 4,877 cases run** (`-m "not integration"`).
Row 3 is ~1% short of a same-population figure. That does not move any conclusion drawn here:
the three encodings sit within a factor of 1.5 of each other and the digest row is three orders
of magnitude below all of them.

Mean node id is **75.8 bytes** (max 196) — the node-id strings *are* the record; status and
duration are noise beside them.

The digest-only row is the whole memory-efficiency argument. A **green** attestation does not
need to name its tests; it needs to prove *which set of tests* it covered, so a later reader
cannot mistake a 400-test run for a 4,877-test one:

```json
{"tree": <40 hex>, "cmd_hash": <16 hex>, "suite_digest": sha256(sorted node ids),
 "n": 4877, "passed": 4868, "failed": 0, "skipped": 9, "rc": 0, "at": <float>}
```

`n` is what **ran** (the fast set — 4,877 of 4,934 collected), and `suite_digest` is over
exactly those node ids, so `passed + failed + skipped == n` is checkable by the reader and a
record that does not balance is a malformed record.

242 bytes. That is a **~2,000×** reduction against per-test records, and it costs nothing the
gate needs — because per-test detail is only ever *useful* for a red run (triage, bh-8e1vn) or
a retry (flake tracking). **Green runs compress to a hash; red runs keep their node ids.**

### 9. Tree churn and the storage/pruning shape — measured

```text
total commits 1,075   distinct trees   828   (first commit 2026-06-28, 48 days)
last 30 days    942   distinct trees   711
last  7 days    203   distinct trees   159   ≈ 23 distinct trees/day
```

Three findings:

* **The epic's core bet is quantified and real — and it is a bet on merges specifically.**
  1,075 commits collapse to 828 distinct trees: **247 commits (23.0%) are savable excess**, and
  **466 commits (43.3%) actually share a tree** with at least one other (192 groups of 2, 26 of
  3, 1 of 4). Those are two different figures and only the first is a saving — 23.0% is the
  number of gate runs tree-keying removes.
* **The decomposition, which is the point.** Of the **244** commits whose tree equals a
  parent's, **242 are merge commits**. Strip merges and duplication collapses to **3 excess
  among 725 non-merge commits — 0.4%**. Essentially *all* of the modeled saving is the `--no-ff`
  merge onto an unmoved base, which is exactly the case the epic's settled decisions 2 and 4
  name. The bet is **sharper** than a flat "23% of commits" reading suggests, not weaker: it is
  not a diffuse quarter of history, it is one structural event.
  **This relocates the entire saving to the landing boundary — see Evidence 12.** The landing
  boundary is precisely where the seat changes between the run that *produces* an attestation
  (`dev/<name>`, in the bead worktree) and the run that *consumes* it (`merge/` or `finish`, at
  the integration branch). So the `bh-ku9n9.12` exposure class is not incidental to this epic;
  it is **co-extensive with the epic's entire payoff**. Every reuse this evidence justifies is
  a reuse across a seat change.
* **Retention is the pruning constraint, not size-per-record.** At ~23 trees/day: digest-only
  is **5.6 KB/day**; per-test JSON is **11.5 MB/day**, ~345 MB/month. Today's ledger is capped
  at `_MAX_ENTRIES = 200` with a 24h TTL — about **48 KB**. Putting per-test records in the
  same file makes that 200-entry cap worth **~96 MiB** inside every hive's `.git/`.

**Storage shape that falls out of this:** keep the existing single small JSON as the *verdict*
ledger (digest-only rows, `_MAX_ENTRIES`, TTL — unchanged shape, ~2 KB growth), and put per-test
detail in **separate per-run files keyed by tree**, written only when a run is red or a test
retried, pruned by the same TTL. Green attestation stays O(200 × 242 B) forever; triage detail
is bounded by how often the suite is red.

### 10. Ingest cost — measured, and it is not in the hot path

Parsing the real 573,785-byte report above with Python's **stdlib** `xml.etree.ElementTree`,
peak RSS via `resource.getrusage(RUSAGE_SELF)`:

| Ingest | Wall | Peak RSS |
|---|---:|---:|
| full DOM parse → 4,877 per-test records (id, status, duration) | **30 ms** | 19.9 MiB (interpreter baseline 15.7 MiB → **+4.2 MiB**) |
| streaming `iterparse` + `el.clear()` → digest + counts only | **32 ms** | no growth beyond the same +4.2 MiB arena |

Against the epic's measured **371,400 ms** gate, the ingest is **0.008%** of one validation. It
runs once per `validate_cmd`, in a process bh already has, on a file already on disk.

That is the whole memory/startup argument settled with a number, and it is the reason the
language preference does not bite here — see the Recommendation. (The streaming variant is
listed because it is the shape to use if a hive ever produces a report large enough to matter;
at this size it is not measurably different.)

### 11. Asking pytest for the report is free — measured

Four runs of `./scripts/hermetic.sh uv run pytest -n auto -m "not integration" -q
-p no:cacheprovider`, alternating the `--junitxml` flag, same machine, same tree:

| Run | `--junitxml` | Wall | Result |
|---|---|---:|---|
| A | no | 152.75 s | 4868 passed, 9 skipped |
| B | **yes** | 140.24 s | 4868 passed, 9 skipped |
| C | no | 135.56 s | 4868 passed, 9 skipped |
| D | **yes** | **125.28 s** | 4868 passed, 9 skipped |

**This table settles nothing on its own, and cannot.** Spread *within* the same configuration
is 17.2 s (A vs C) — larger than any plausible effect — so at n=4 the ordering is a coin flip,
and no feasible n rescues it: see the direct number below. Do not read a winner out of these
four rows in either direction.

**The direct measurement settles it.** Building, serializing and writing the full 4,877-case
JUnit XML with the same stdlib `xml.etree` path pytest's reporter uses costs **23.6 ms**
(median of 7; 23.2–27.5 ms) for a 569 KB file. Against the epic's measured 371,400 ms gate that
is **0.006%** — and it is **~730× smaller than the 17.2 s of noise** the table above shows, which
is why the table is unresolvable by construction rather than merely underpowered. This is the
same direct technique Evidence 10 uses on the ingest side, and it is the sole basis for the
conclusion: **asking a runner for machine-readable output is free.**

Two honest caveats on this table:

* **Peak RSS of the run itself could not be measured this way.** `scripts/hermetic.sh` runs
  pytest under `bwrap`, so `getrusage(RUSAGE_CHILDREN)` reports the wrapper's 11 MiB, not
  pytest's. The number that actually matters for this decision — the cost of the *ingest bh
  would add* — was measured directly instead (Evidence 10).
* **All four runs show 0 failures, including `test_claim_supervised_leaves_identity`** — which
  the dispatcher's baseline of record (`4934 collected — 1 failed, 4867 passed, 9 skipped,
  133.8 s`, captured at `790b084`) has failing. This bead's own `bh work check` was green too
  (`4868 passed, 9 skipped in 138.09 s`). **The variable is the seat, and it is not noise:** the
  baseline of record was taken in the *dispatcher's* seat (`disp/`), every run in this table
  under `dev/spike`. Same tree, same machine, **different seat** — which is Evidence 12,
  arriving unprompted in this spike's own measurements. The verdict this tree transfers is a
  function of *where it was asked*, which no tree hash can encode.

### 12. The soundness counterexample: tree equality is NOT sufficient in this repo today

`tests/test_work.py::test_claim_supervised_leaves_identity` (filed as **`bh-ku9n9.12`**) is
**deterministic per seat** — not flaky, and not "unstable". At one tree, varying only the
ambient seat:

| Seat | Runs | Result |
|---|---|---|
| `disp/pilot` | 3/3 (reviewer: 5/5) | **fails** |
| `dev/spike` | 3/3 (reviewer: 5/5) | passes |
| `merge/m1` | 3/3 | passes |
| unset / clean clone | 3/3 | passes |

The mechanism is exact and in-repo: the test calls `work.claim(..., as_='')`, so the actor
resolves from the worktree's ambient seat stamp, and `_guard_seat` (`src/beadhive/work.py:536`)
raises on a `disp/` prefix because *a dispatcher may not claim an issue*. Same input, same
answer, every time — the input just includes something that is **not in the tree**. That is why
the bead's own title says the check *"is not hermetic — it reads the ambient seat identity of
the worktree it runs in"*, and it is why "flaky" is the wrong word: **a test filed as flaky
routes to quarantine; this one routes to the fence.**

This is a live, in-repo refutation of the epic's settled decision 2 (*"tree equality is
sufficient evidence"*) **as currently stated**. The verdict it transfers is a property of
*where it ran*, not of *what was tested*. `bh-mpk77` (widen the fence to every phase) is the
filed fix, and until it lands, at least one test in this repo makes a tree-keyed attestation
report a result the next reader cannot reproduce.

**And Evidence 9 says this class is not a corner case.** 242 of the 244 tree-duplicate commits
are merges, so essentially every reuse tree-keying buys happens at a landing boundary — where
the producing run is a `dev/` seat and the consuming run is `merge/` or `finish`. The one
variable that flips this test is exactly the one variable that changes across that boundary.

**What per-test records can and cannot do about it.** They make an environment-dependent test
*visible* **only when the environment happens to vary between two recorded runs of the same
tree** — same tree, two runs, different per-test outcome ⇒ environment-dependent or flaky, the
same comparison the epic already requires for flake tracking. That is opportunistic, not
structural. **Deterministic seat-dependence is precisely the case per-test records cannot
catch**: every run in a given seat agrees with every other run in that seat, so a fleet that
always produces in `dev/` and consumes in `merge/` may never record the two runs whose
disagreement would expose it. Detection is a consolation prize here; the fence is the fix.

Note also that the ledger records a `host` field it explicitly never reads back — *"never read
back / compared — see bh-ytbb.4"* (`validation_ledger.py:88`). Recording environment identity
without ever comparing it is the shape of the assumption `bh-ku9n9.12` breaks; the field itself
is not the remedy (a host id is constant across this failure — see the escalation below).

## Verdict — **NO-GO** on a runner shim/plugin registry · **GO** on a narrowly-scoped built-in provider

**NO-GO** for the shim/plugin registry. The blocker is Evidence 1 and it is structural, not
budgetary: `validate_cmd` is a *pipeline* (`check: lint lint-md license-check test`), so an
adapter that owns the run either drops two thirds of this repo's gate — violating the epic's
INVARIANT outright — or has to be told the pipeline per hive, which is the per-hive config it
was built to avoid. No amount of adapter quality changes that, so no prototype was built.

**GO** for a built-in attestation provider, on the narrow definition that bh **never owns the
run** — it ingests what the run already dropped. Three findings make that cheap: JUnit XML is
universal (Evidence 3), the report is enabled in each project's *own* already-maintained config
rather than by bh (Evidence 3, second column), and green attestations compress ~2,000× to a
242-byte digest (Evidence 8).

## Recommendation — the three options for `bh-ku9n9.4`

The decision bead must choose between exactly these three. Costs below are stated as
**per-hive configuration** (what an operator maintains forever) and **build cost** (what we
maintain forever).

### Option A — registered test-runner shim / plugin registry

bh grows a registry of per-framework adapters (like `plugins.py`) that discover the hive's
framework and drive it for all three tiers. Preferred language Rust.

* **Per-hive config:** ~zero *if* the hive's gate is a bare runner invocation; **the entire
  pipeline, re-declared** otherwise — which is every pipeline hive checked, **4 of the 7 `work`
  blocks in `~/.beadhive/hq/fleet.yaml`**: this hive's `just check-all` (justfile:120) and the
  fleet-default `just check` (justfile:53), `baml-harness`'s `sh -c 'just check && just test'`,
  and `orca`'s `sh -c 'pnpm typecheck && pnpm exec vitest run …'` (molecule leg: `pnpm lint &&
  pnpm typecheck && pnpm test`). The remaining 3 — `just build`, `just test`,
  `just validate-scaffold` — live in other repos and were **not inspected**; the claim is not
  made about them. The argument needs one pipeline hive and has four.
  Worth noting how thin the "bare runner" escape hatch is even so: this repo's own
  bare-looking `just test` is `./scripts/hermetic.sh uv run pytest -n auto -m FAST`
  (justfile:318) — the sandbox fence *and* the marker selection are hive knowledge an adapter
  would still have to be told.
* **Build cost:** one adapter per framework, forever, each tracking upstream flag drift; a
  discovery surface; a new binary + release artifact per platform + a `deps.py` row if Rust; a
  subprocess spawn on every validation.
* **Invariant:** ✗ **actively hostile.** The adapter runs the framework; the gate runs a
  pipeline. Whenever those differ the attestation asserts more than was proven.
* **`bh-ku9n9.12` class:** would not detect it — an adapter that owns the run has *less*
  environment context than the hive's own fenced recipe, not more.
* **Verdict:** reject. It is Option C with a plugin registry bolted on and a build cost.

### Option B — built-in attestation provider (RECOMMENDED)

bh keeps invoking the hive's `validate_cmd` **verbatim, unchanged**. It additionally exports
one environment variable (`BH_TEST_REPORT_DIR`) into the validation subprocess, and after the
run ingests any JUnit XML it finds there. No report ⇒ today's behaviour exactly, no warning, no
config. Per-test outcomes fold into the tree-keyed ledger as a `suite_digest` + counts for
green, full node ids for red.

* **Per-hive config in bh: ZERO.** A hive opts in by routing the variable in its *own* test
  config — `addopts = --junitxml=$BH_TEST_REPORT_DIR/…` in `pyproject.toml`,
  `[profile.default.junit]` in `.config/nextest.toml`, a `reporters` entry in
  `vitest.config.ts`. That surface already exists and is already maintained by the hive
  (Evidence 3). Nothing new appears in `bh config`.
* **Build cost:** one JUnit-XML ingest (stdlib `xml.etree`, measured in Evidence 10), one
  ledger schema widening, one env var. Roughly a day, and it is *the* change bh-1owpi already
  scoped as "record per-test outcomes".
* **Invariant — scoped, and the scope matters:**
  * **Tier 1 (ingest a report): ✓ by construction.** bh runs strictly what it runs today and
    only *reads more* about it. Reading a report cannot change what ran, so it cannot add a
    pass because it cannot subtract a run.
  * **Tier 2 (`work.validate.subset`): ✓ by discipline, not construction.** The moment a subset
    command exists *and anything consults it*, bh **can** run less than the full gate, and the
    invariant then rests on settled decision 1 (the confirming run is mandatory) being obeyed
    — a rule, not a structural impossibility. Scoring the whole option "by construction" would
    be false, and an ADR must not carry it that way.
  * **Trust boundary — three v1 constraints, not optional.** bh would ingest XML from a path it
    advertises (`BH_TEST_REPORT_DIR`), written by a process it does not control. So: (1) **`rc`
    is authoritative** — the report is detail only, and a report claiming 4,877 passed against
    a non-zero `rc` is a discrepancy to surface, never a verdict; (2) **the report dir is
    cleared per run** — a stale green report from a previous tree is *literally* "a pass a full
    run would not have produced", a direct invariant violation, and the only defence is that
    nothing survives into the next run's directory; (3) **a missing report is not a failure** —
    it is today's behaviour. All three are cheap; none may be left implicit.
* **`bh-ku9n9.12` class:** ~ **detects it only opportunistically.** Two runs of the same tree
  with different per-test outcomes is mechanically a flake-or-environment signal (the same
  comparison the epic already requires for flake tracking) — but that needs the environment to
  *happen* to vary across two recorded runs of that tree. Deterministic seat-dependence is the
  case per-test records **cannot** catch by construction (Evidence 12), and per Evidence 9 the
  seat changes exactly where the reuse happens. Options B and C see more than Option A here;
  none of the three is a substitute for `bh-mpk77`.
* **Tier 2 caveat:** failure-scoped re-run still costs **one template string per hive**
  (Evidence 6) under *every* option. Ship it as an optional `work.validate.subset` and let it be
  absent — absent means the epic's mandatory confirming run is the only run, which is today's
  behaviour.
* **Tier 3:** out of v1. Python-only in practice (Evidence 7).

### Option C — per-hive config remains the honest answer

Every hive declares where its results land and how to run a subset: `work.results.path`,
`work.results.format`, `work.validate.subset`.

* **Per-hive config:** 2–3 strings **per hive**, hand-maintained, silently drifting when a
  framework changes a flag — precisely the outcome the bead names as the thing to avoid.
* **Build cost:** near zero (config plumbing) — but note it still needs the same JUnit parser
  Option B needs, so it is not actually cheaper to build; it is only cheaper by one env var and
  more expensive by ~20 hives × 2 strings.
* **Invariant:** ✓ preserved (bh still never owns the run).
* **`bh-ku9n9.12` class:** ~ same opportunistic detection as B, once the results are parsed —
  and the same blind spot.
* **Verdict:** the fallback, not the answer — it is strictly Option B plus per-hive paperwork.

### Recommendation

**Option B**, because it is the only one of the three where bh never owns the run *and* costs
zero per-hive configuration in `bh` — the opt-in lives in each project's own already-maintained
test config. On the invariant, the honest claim is the scoped one: **for tier 1 it holds by
construction** (reading a report cannot subtract a run), and for tier 2 it holds by
**discipline** under settled decision 1, with the three trust-boundary constraints above (`rc`
authoritative, report dir cleared per run, missing report is not a failure) as v1 requirements
rather than hardening to add later.

**Implementation language: Python.** This contradicts the operator's stated preference and the
reason is measured, not stylistic — see Evidence 10. The ingest is a once-per-`validate_cmd`
parse of a file already on disk, in a process bh already has. A Rust binary would add a build
toolchain, a per-platform release artifact, a `deps.py` row and a process spawn to save a
number that rounds to nothing against a 371-second gate. `quick-junit` (Evidence 5) remains the
right dependency the day the ingest is ever *not* a once-per-gate parse. If the operator holds
the Rust preference regardless, the honest cost is that build/release surface — the runtime cost
argument does not support it.

## What the epic's design must change — **needs operator sign-off**

Three items, because two of them touch decisions the epic records as *settled*. Flagging, not
deciding:

1. **Settled decision 2 — "tree equality is sufficient evidence" — is falsified in this repo
   today** by `bh-ku9n9.12` (Evidence 12), and per Evidence 9 the falsifying variable (the
   seat) changes at exactly the landing boundary where ~all of the modeled saving is. **The
   remedy is `bh-mpk77` (widen the fence to every phase), as a hard prerequisite before any
   tree-keyed reuse is trusted — and it is the sole prerequisite.**
   An environment **fingerprint is not an alternative remedy** and is not offered as one. The
   variable here is the **seat** (`$BH_DEV`'s prefix): same host, same clone, different
   worktree. A host-level fingerprint is *constant across this failure* and catches exactly
   nothing — it would add a key field, cost cache hits, and leave the exposure intact. Anything
   that did work would have to name the **seat** explicitly, at which point the honest fix is
   to stop the seat leaking into the test at all, which is `bh-mpk77`.
   What such a key change would reverse, stated precisely, is not the `host` *field* but the
   **decision at bh-ytbb.4 that environment identity is not part of the trust key** — the
   `validation_ledger.py:88` comment (*"never read back / compared"*) is that decision's
   artifact, not its substance. This is the loudest item and the epic should not proceed to
   implementation without ruling on it.
2. **Tier 2 is a developer-loop capability, not an attestation input.** Combined with settled
   decision 1 (*the confirming run is MANDATORY*), failure-scoped re-run saves the gate nothing
   — the full run happens either way. It only shortens a developer's converge-to-candidate loop.
   The design currently reads as though tier 2 is a gate optimisation, which it can never be.
   Say so explicitly or a later implementer will build it into the gate and reintroduce exactly
   the laundering hazard the epic names.
3. **Tier 3 should be struck from v1 scope**, not merely called "genuinely rare". Evidence 7
   confirms it is Python-only-in-practice, and outside Python it is an execution-model change
   (process-per-test) rather than a capability. It belongs in the backlog next to the
   already-filed `bh-acvq`.

Non-blocking, but worth folding in: the ledger's storage shape should split *verdict* rows
(digest-only, 242 B, existing file, existing `_MAX_ENTRIES`/TTL) from *triage* detail
(per-tree files, written only for red or retried runs) — Evidence 9. Putting per-test records
in the existing ledger file makes its 200-entry cap worth ~96 MiB per hive.

Adopt the OpenTelemetry `test.*` attribute names (`test.case.name`,
`test.case.result.status`) for the record's fields (Evidence 4). Free, and it is the only
naming standard in the space with a stable home.
