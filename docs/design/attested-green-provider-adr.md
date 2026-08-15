# Attested Green — provider shape and the configuration surface the epic ships

> Status: **decided** (`bh-ku9n9.4`, seat `dev/decide`, 2026-08-15; epic `bh-ku9n9`).
> Rules on [`docs/spikes/bh-ku9n9.2-test-runner-interface.md`](../spikes/bh-ku9n9.2-test-runner-interface.md).
> Companion to [`attested-green-adr.md`](attested-green-adr.md), which decides the *key*
> (tree, not commit); this one decides *who produces the per-test record* and *what an
> operator configures per hive*. Nothing here reopens that ADR's six settled decisions or the
> seventh (establish-from-tree) recorded in the epic's notes.

## Verdict

**NO-GO on Option A** (registered test-runner shim / plugin registry).
**GO on Option B** (built-in attestation provider), narrowly scoped, **in Python**.
**Option C** (per-hive results config) is the fallback shape, not the answer — it is Option B
plus paperwork.

One line: **`validate_cmd` is a pipeline, not a test invocation, so no adapter can own the run
without either dropping legs (violating the epic's INVARIANT) or being told the pipeline per
hive (becoming Option C in a registry costume).**

The spike's evidence is not restated here — it stands as filed, having survived two adversarial
review rounds with its Verdict section byte-identical across the correction pass and its key
numbers independently re-derived. The load-bearing fact is Evidence 1: `justfile:53`
(`check: lint lint-md license-check test`) and `justfile:120` (`check-all: … test-integration-land
demo-local-loop`). Roughly two thirds of the measured 371.4 s gate is not a pytest invocation and
`demo-local-loop` is not pytest at all. Four of the seven `work` blocks in the fleet are pipeline
hives. An adapter that runs the framework attests to a run that never happened.

**What GO on Option B actually authorises, and nothing more:** bh invokes the hive's
`validate_cmd` **verbatim**, exports one environment variable naming an empty directory, and
after the run ingests whatever JUnit XML it finds there. bh never owns the run, never injects a
flag, never probes for a framework, never enables behaviour from a detection. That is the whole
of the provider.

## Implementation language: **Python** — deliberately against the stated preference

The bead ranked Rust > Go > Python with memory efficiency first-class. The ingest is a
once-per-`validate_cmd` parse of a file already on disk, in a process bh already has: **30 ms
and +4.2 MiB peak RSS** on this repo's real 573,785-byte report with stdlib `xml.etree`
(spike Evidence 10) — **0.008 %** of one 371,400 ms gate. Asking pytest to write the report costs
**23.6 ms** (0.006 %; reviewer re-derived 26.6 ms, same file to the byte — Evidence 11).

A Rust binary would buy a number that rounds to zero and cost a build toolchain, a per-platform
release artifact, a `deps.py` row, and a process spawn on every validation. The preference was
justified by memory in the hot path; measurement removed the hot path. `quick-junit` (10.9 M
downloads, nextest's own crate) is the right dependency the day the ingest stops being a
once-per-gate parse — not before. **If the operator holds the Rust preference regardless, the
honest cost is that build/release surface; the runtime argument does not support it.**

## The configuration surface the epic ships with

This is the part downstream beads may rely on. Everything layers per-hive over global exactly
like every other `work.*` setting (`config.work_value` → `layered`).

| Key | Type | Default | Owner bead | What it does |
|---|---|---|---|---|
| `work.validate_cmd` | string | `just check` | shipped | Unchanged. The gate. bh runs it verbatim. |
| `work.validate.<phase>` | map phase→string | `{}` | shipped (+`push-main` from `bh-ku9n9.5`) | Unchanged. Per-boundary override; `<phase>-main` variant wins on the integration branch. |
| `work.validation` | enum | `relaxed` | shipped | Unchanged. |
| `work.ledger_ttl` | ISO-8601 duration | `P1D` | `bh-ku9n9.3` | How long a green verdict stays reusable. Tune **down**. |
| `work.validate_subset` | string template | `""` (absent) | `bh-ku9n9.8` | **The only new operator-facing key this epic adds.** Tier 2, developer-loop only. |

**That is the entire surface. Tier 1 costs zero configuration in `bh`.**

### Tier 1 — machine-readable results: no key at all

bh exports **`BH_TEST_REPORT_DIR`** into the validation subprocess, always, for every hive,
with no opt-in. A hive opts in by routing that variable in **its own already-maintained test
config** — `addopts = --junitxml=$BH_TEST_REPORT_DIR/junit.xml` in `pyproject.toml`,
`[profile.default.junit]` in `.config/nextest.toml`, a `reporters` entry in `vitest.config.ts`.
That surface exists, is owned by the hive, and is where the hive already keeps this knowledge.
**Nothing new appears in `bh config`, and no hive is asked to describe its runner.**

Three properties of `BH_TEST_REPORT_DIR`, binding in v1:

1. **It is a drop zone, not the store.** bh creates it **fresh and empty immediately before
   exec** (per binding constraint 2 — a stale green report from a previous tree is literally a
   pass a full run would not have produced). It must therefore never *be* the durable per-tree
   directory `bh-ku9n9.6` writes; bh ingests from the drop zone and then persists into
   `.bh/testreport/<tree>/`. Two directories, because clearing one and retaining the other are
   both required and cannot be the same path.
2. **`rc` is authoritative** (binding constraint 1). The report is detail. A report claiming
   4,877 passed against a non-zero `rc` is a discrepancy to surface, never a verdict. **A report
   may never upgrade a verdict.**
3. **A missing report is not a failure** (binding constraint 3). No report ⇒ rc-only ledger
   entry ⇒ today's behaviour, no warning, no nudge, no config.

Green attestations stay digest-only (`suite_digest` over sorted node ids + counts, **242 bytes**,
~2,000× smaller than per-test records — Evidence 8); per-test node ids are retained only for red
or retried runs. Record field names follow the OpenTelemetry `test.*` vocabulary
(`test.case.name`, `test.case.result.status`) — free, and the only naming standard in the space
with a stable home.

### Tier 2 — failure-scoped re-run: `work.validate_subset`, optional, developer-loop only

One template string per hive is an irreducible floor under **all three options** (Evidence 6):
the selector syntax normalises trivially, but only the hive knows which leg of its pipeline takes
node ids and which legs must still run. So it is not a differentiator — it is the price of tier 2
whatever we build.

```yaml
work:
  validate_subset: "./scripts/hermetic.sh uv run pytest -n auto {tests}"
```

* Single required placeholder **`{tests}`**, replaced with shell-quoted, space-joined selectors
  bh holds (never pytest's `--lf`, which reads implicit `.pytest_cache` state keyed to nothing
  and invisible to bh — Evidence 6).
* A value without `{tests}` is rejected at `bh config set` (`config._validate`) and treated as
  **absent** at read time. Tier 2 is a convenience; it fails open to the full run, never closed.
* **Absent is fully supported and is the default.** Absent ⇒ no converge loop ⇒ the phase runs
  whole ⇒ today's behaviour.
* **It is never consulted on the run that writes an attestation.** Settled decision 1 makes the
  confirming run mandatory, so tier 2 saves the gate exactly nothing — it shortens a developer's
  red-to-knowing-why loop and produces the retry history that makes flakes visible. An
  implementer who wires it into the gate reintroduces the laundering hazard this epic exists to
  prevent.

**Key placement, and why not `work.validate.subset`.** `work.validate` is a free-form
`dict[str, str]` keyed by *phase* (`config_schema.WorkConfig.validate_overrides`), so it cannot
validate its own keys — a non-phase member would need a reserved-word guard in `validate_cmd()`
forever, and a typo'd phase would still pass silently. `work.validate_subset` is a declared field
on a `extra="forbid"` model, sits beside `work.validate_cmd` (string beside string), and needs no
mechanism at all. Same layering, one less rule.

### Tier 3 — per-test coverage attribution: **no key, struck from v1**

Not configurable because not present. Outside Python it is not a slower path but a different
execution model (process-per-test plus O(n) artifacts), so it cannot degrade gracefully — a
direct conflict with the tier contract, which is a stronger reason than "rare". Backlog, next to
`bh-acvq`.

### What a hive that configures nothing gets

`just check`, run whole, at every boundary. `BH_TEST_REPORT_DIR` exported (newly, for every hive —
bh creates the directory per run) and ignored ⇒ no report ⇒ rc-only ledger entry. No converge
loop. No coverage map. Verdicts reusable for `P1D` on exact tree match.

**Scoped to the provider, that is byte-for-byte today's behaviour**: the command bh runs and the
work performed are unchanged. It is *not* a claim that the whole epic is behaviour-preserving —
today's ledger keys on commit sha, and re-keying it to the tree (so a `--no-ff` merge onto an
unmoved main reuses where it previously re-ran) is `bh-ku9n9.3`'s business and the epic's entire
thesis, not the provider's. `P1D` does equal the shipped 24h. The tier contract is honoured here,
not a degraded mode.

## Consequences for the filed beads

* **`bh-ku9n9.6`** (results) may assume: `BH_TEST_REPORT_DIR` is exported and empty at exec;
  ingest is stdlib `xml.etree`, JUnit XML only; the three binding constraints above are its
  acceptance, not hardening for later. It must **drop `contexts.json` and `coverage.xml`** —
  tier 3 is struck. Storage splits *verdict* rows (digest-only, existing ledger file, existing
  `_MAX_ENTRIES`/TTL) from *triage* detail (per-tree files, red or retried runs only): per-test
  records in the existing ledger would make its 200-entry cap worth ~96 MiB per hive
  (Evidence 9).
* **`bh-ku9n9.8`** (converge loop) owns `work.validate_subset`, including its `_validate`
  placeholder check, and must assert that no attestation is ever written from a run that
  consulted it.
* **`bh-ku9n9.9`** (impact selection) may assume **no coverage map exists in v1**. Its
  `NO MAP FOR THIS TREE → RUN EVERYTHING` branch is therefore the whole of its v1 behaviour, so
  as filed it has no v1 content and should be deferred behind a future tier-3 bead rather than
  implemented against a producer that was struck. **This is a dispatcher/operator call, not this
  bead's to make** — flagged, not actioned.
* **`bh-ku9n9.3` / `bh-ku9n9.5`** are unaffected: no new key, `push-main` is a value in the
  existing phase map.
* Nothing here needs `bh-mpk77` or `bh-ab5e7` to land first — the seventh settled decision
  (establish-from-tree) closed that dependency, and this ADR adds no environment fingerprint.

## Non-goals, stated so a later reader does not re-derive them

No plugin registry. No per-framework adapters. No framework detection driving behaviour
(precedent: `validate_probe.py`'s tri-state resolver and `bh-d0kb`'s knowledge-only `toolchain` —
detection may inform and suggest, **never enable**). No CTRF (TypeScript ecosystem, excluded, and
its own on-ramp is `junit-to-ctrf` — the format we would already be parsing). No libtest JSON
(unstable upstream since 2019). No new binary, no new release artifact, no `deps.py` row. No
`work.results.path` / `work.results.format` — Option C's keys are not shipped.
