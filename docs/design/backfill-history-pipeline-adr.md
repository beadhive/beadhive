# ADR: Backfill history pipeline — parse, collate, upsert (record categories, phases, BeadLog projection)

- **Status:** Accepted 2026-11-11 (decisions D1-D6 accepted as recommended).
- **Date:** 2026-11-11
- **Decision owner:** Beadhive maintainers (operator-approved planning session)
- **Related work:** workstream `bh-65vja` (`wave:5-backfill-history`); requirements `bh-72oxm`
  (#144, more sources) and `bh-tseo2` (#143, create beads); `bh-l1m3p` (D1), `bh-ear6v`
  (explicit prefix), `bh-d89mc` (stringer, D5), `bh-9wpd.8` (deferred source strategies)
- **Amends:** [`backfill-platform-adr.md`](backfill-platform-adr.md), which gains an amendment
  note pointing here. The BeadLog format ([`beadlog-format-adr.md`](beadlog-format-adr.md)) is
  unchanged.
- **Grounded in:** main `0a3ff9b9` (0.35.11).

## Context

Each strategy (`git-commits`, `docs`, `planning`) goes **source → BeadLog events in one step**,
and each does its own parsing, its own ref resolution, and its own identity matching:

- `commits` resolves ids only against the hive's own baseline and only *links* (`git.commits`).
- `docs` parses `*.md` only and does its own bridge/judge flow; HTML is invisible.
- `planning` is the only creator, GSD-only, and mints ids from `modal_prefix()` (a prefix
  guess, which bh-ear6v forbids).
- No pass can see another's evidence. "This doc was added in the same PR as three feat commits"
  is a cross-source fact that no single strategy can state, and status conflicts (#143.5) and
  commit grouping (#143.1) both need it.

So adding sources to the current shape multiplies these single-purpose pipes. The fix is to
split the work into three phases with a typed hand-off between them.

## Decision

```text
 Phase A: PARSE (per source, independent, parallel, pure)
   git ─┐  docs(md/html) ─┐  planning(GSD/roadmap) ─┐  github snapshot ─┐  changelog… ─┐
        ▼                 ▼                          ▼                   ▼              ▼
                 Evidence corpus  (Artifacts + raw Refs, content-addressed JSONL)
                                         │
 Phase B: COLLATE (one engine, deterministic first, judge last)
   B1 index → B2 resolve refs → B3 cluster into WorkUnits → B4 attach docs
   → B5 identify vs baseline → B6 derive status, dates, conflicts
                                         │
                       Collation (units + verdicts + conflicts + residual), reviewable
                                         │
 Phase C: PROJECT + UPSERT (existing machinery)
   units → proposed .bdlg → reconcile (NEW/PRESENT/NOISE/DRIFT) → plan → verify → apply
   → optional gapfill on newly created beads
```

Phase C is mostly what exists today (`reconcile`, `BackfillEngine`: plan hash, drift gate,
pre-state, audit, rollback, idempotency proof). Most of the new code is in A and B, and the
safety envelope is reused unchanged.

### Phase A: Parse

**New contract** in `beadlog-sources` (neutral, no `beadhive` import):

```python
@dataclass(frozen=True)
class Ref:            # an un-interpreted reference found in an artifact
    kind: str         # "bead-token" | "sha" | "pr" | "issue" | "path" | "url" | "tag"
    raw: str          # exactly as written ("abc-12x", "#143", "a1b2c3d")
    where: str        # "subject" | "body" | "frontmatter" | "trailer" | "html-meta" ...

@dataclass(frozen=True)
class Artifact:
    category: str     # change | review | tracker-item | design-record | plan-item | release-marker
    kind: str         # "commit" | "merge" | "tag" | "release" | "doc" | "plan-item" | "pr" | "issue" | "changelog-entry"
    key: str          # stable source key: "git:commit/<sha>", "doc:docs/design/x.html", "gh:pr/123"
    title: str
    occurred_at: str | None
    attrs: Mapping[str, Any]   # normalized: status, declared_date, owner, cc_type, cc_scope, paths, parents, merged_at…
    raw_attrs: Mapping[str, Any]  # as written, kept for conflict display
    refs: tuple[Ref, ...]
    provenance: str   # observed | derived
    reader: str; reader_version: str

class SourceReader(Protocol):     # entry-point group "beadlog.readers"
    name: str; version: str
    def read(self, request: ReadRequest) -> Iterable[Artifact]: ...   # pure; never touches a store
```

The corpus is JSONL (`bdlg.evidence/1`) in the run directory, sorted by key, with one header
per reader (name, version, options, source cursor). It is **not** part of the BeadLog format,
so the format ADR stays untouched; it is an intermediate artifact like a compiler IR.

#### Source record categories (the shape layer)

Readers are per *source*. Records are typed by **category**, a small closed set of shapes that
sources with the same meaning share. Each `Artifact` carries `category` and `kind`. Collation
rules and the BeadLog projection are written **per category, never per source**. A new source
(GitLab MRs, Jira, AsciiDoc ADRs, Task Master) is then only a reader that emits an existing
category. It needs no new collate or projection code.

| Category | Typical sources (`kind`) | Required shape (normalized `attrs`) | Role in collation | Feeds BeadLog as |
|---|---|---|---|---|
| `change` (code history) | git commit, merge commit | sha, parents, author, committed_at, cc_type/cc_scope, paths, trailers | evidence; clustered into units (PR, then merge, then feat commit) | `set metadata.git.commits` (append); dates for `metadata.lifecycle.*`; `evidence: commit` entries |
| `review` (change requests) | GitHub PR, GitLab MR, review-tool export | number, state, opened_at, merged_at, commits[], closes[] | **primary work-unit boundary** | `create` (closed) with `external_ref gh:pr/N`; `phase` close at merged_at |
| `tracker-item` (issue trackers / other ledgers) | GitHub issue, foreign-ledger bead, Jira, Linear | id, state, labels, assignee, opened_at, closed_at | identity anchor; can absorb units that close it | `create` or `metadata.xref.*` (foreign); status from tracker state; `comment` events opt-in |
| `design-record` (design docs) | md/html ADR, design, architecture, spec | declared_status (normalized vocab + raw), declared_date, owner, supersedes | attached to units; **source of declared status** for conflict checks | `set external_ref` stamp or `create` (decision/feature); `metadata.backfill.conflict` |
| `plan-item` (planning) | GSD plan, roadmap/implementation-plan item, checklist | parent, depends_on, done_marker, order | already a unit (structure is stated, not inferred) | `create` epic/issue + `edge_add parent-child/blocks` + `phase` close |
| `release-marker` (releases) | tag, GitHub release, CHANGELOG entry | version, released_at, commit range | never a bead by default; gives dates, can close epics | `metadata.release`, `occurred_at` of closes |

The existing beads (the `bd` baseline and foreign ledgers through `LedgerDirectory`) are the
**target side**, not a category. Readers never emit them.

**How every category feeds BeadLog.** One projection contract applies to all categories:

- every emitted event's `source` is the unit's key, and its `evidence[]` lists the artifact keys
  that support it (a commit, a PR, a doc), so any field on a bead can be traced back to its
  records;
- `provenance` is set by how the fact was reached, not by its category: **observed** (the record
  states it, like a PR merged_at or a doc status line), **derived** (computed by a rule, like
  commits clustered by PR or an epic by scope), or **inferred** (judge, threshold or LLM; carries
  `confidence` and the recorded verdict);
- `occurred_at` comes from the category's own clock (committed_at, merged_at, released_at,
  declared_date). When two clocks disagree, B6 reports a conflict and does not pick one;
- the event kinds used stay inside the BeadLog v0.1 vocabulary (`create`, `set`, `label_add`,
  `edge_add`, `phase`, `comment`), so no format change is needed.

Readers (each its own bead or sub-molecule):

| Reader | Produces | Notes |
|---|---|---|
| `git` | commit, merge, tag, release(annotated tag) | one `git log --name-status --parents` + one `for-each-ref`; extracted from `commits.py`/`git_dates.py`/`_sources.first_adds`; parses Conventional Commit type/scope, trailers, merge first-parent |
| `docs` | doc | **one shared parser** with a `DocFormat` port: `markdown` (today's frontmatter/heading logic) and `html` (stdlib `html.parser`: `<title>`/first `<h1>`, `<meta name=status/date/author>`, a "Status:" line, body text for refs). Layout presets: `docs/adr`, `docs/architecture`, `docs/design/*.html`, `docs/*.html`, review-tool exports; configurable globs. Metadata normalized to `proposed/draft/accepted/delivered/superseded/rejected/deprecated` + raw kept. MADR/Nygard `## Status` sections are covered here (overlaps deferred bh-9wpd.8). |
| `planning` | plan-item (epic/issue/dep) | `PlanningFormat` port: `gsd` adapter (port of today's code) + `roadmap` adapter (headed checklists / implementation-plan docs). |
| `github` | pr, issue | **two steps**: `fetch` writes a snapshot JSON (network, auth via existing tracker-github transport/secret store), `read` parses the snapshot (pure). Re-extraction stays deterministic and offline. Lives in the `beadhive-tracker-github` plugin, registered as a `beadlog.readers` entry point. |
| `changelog` (later) | changelog-entry | cheap once the port exists; leave in bh-9wpd.8 |

Readers do **no** resolution. A bead-looking token is recorded as `Ref(kind="bead-token")`
whether it is local, foreign or noise. That one rule is what lets every source share one
resolver.

### Phase B: Collate

One engine (`beadlog_sources.collate`), input = evidence corpus + baseline BeadLog + a
`LedgerDirectory`, output = `Collation` (JSON, `bdlg.collation/1`). Steps:

- **B1 Index.** sha→commit, merge→children (first-parent), tag→commit range, PR→commits,
  path→commits that touched it, doc→add commit, key→artifact.
- **B2 Resolve refs.**
  - `bead-token`: baseline (local ledger) → `LedgerDirectory` (foreign ledgers) → `unresolved`.
    `LedgerDirectory` is a **port**. Adapters: the registered bd routes of the workspace, and
    the HQ hive registry (prefix to hive, by registration, never by guessing per bh-ear6v).
    It answers "does `<token>` exist in ledger L?" from that ledger's exported id set, cached
    per run. A foreign hit becomes a cross-ledger link recorded in `metadata.xref.*` on the
    local bead or unit (cross-hive *edges* stay out of scope, bh-1k1p).
  - `sha` / `pr` / `issue` / `path` / `tag`: resolve to artifacts via B1 indexes.
- **B3 Cluster into WorkUnits** (candidate beads). Ordered, configurable rules. The first
  rule that claims an artifact wins:
  1. plan-item → unit (it already *is* a bead)
  2. explicit local bead id → attach to that existing bead (today's `git.commits` behavior)
  3. GitHub PR → unit (commits of the PR)
  4. first-parent merge commit (`chore(merge)`/merge) → unit (its second-parent commits)
  5. leftover commits → unit per `feat` commit (default), with types fix/docs/chore/test
     skipped by default (configurable `include_types`)
  6. epics: units grouped by CC scope, else top-level path area, else release window,
     with a per-hive override map (`backfill.collate.areas`)
  Release tags never make beads by default. They give dates and can close epics.
- **B4 Attach docs** to units: explicit ref → same commit/PR added both doc and code → shared
  paths → lexical. A doc with no unit stays a doc-unit (today's docs behavior).
- **B5 Identify vs baseline.** For each unit, try in order: `external_ref`/source-key match
  → explicit id → deterministic lexical engine → **judge** (if enabled) → **threshold** (if
  configured: below `new_threshold` fuzzy score ⇒ NEW, labelled and still reviewed) →
  `residual` for a human. Every verdict is recorded with engine/model/confidence.
  - **Resume** comes almost free: verdicts already key on query hash and are reused from a
    `prior` archive. Make the run directory persistent and auto-feed the last collation as
    `prior`. The cap then just stops the run, and the next run continues where it stopped.
  - **Cost preview**: B5 first counts the residual, prints `N judge calls ≈ $X` (per-call
    estimate from config), and asks for `--yes` when N > `max_calls_per_run`.
- **B6 Status, dates, conflicts.**
  - unit status: merged/committed ⇒ closed; plan-item status per its adapter.
  - dates: created = first commit / PR opened / doc add; closed = merge / PR merged / tag.
  - **conflicts** (#143.5), all reported with both sources and none auto-resolved: doc says
    draft/proposed but code landed in the same commit/PR; doc "design only" with ≥1 attached
    feat commit; doc accepted after the last release with no attached code; plan-item open but
    summary exists. They show in `inspect` and annotate the bead (`metadata.backfill.conflict`).
    They are never silently picked.

### Phase C: Project + upsert

- **Projector** (neutral) maps units to BeadLog events: `create` (type, title, closed `phase`,
  occurred_at from B6), `external_ref` = unit source key (`gh:pr/123`, `doc:path`), labels
  `source:<kind>` + the creation label (see D1), `git.commits` appends, `parent-child` to the
  area epic, `metadata.xref.*`, `metadata.lifecycle.*` from B6.
- **Ids**: `mint_bead_id(prefix, group, unit.key)`. `prefix` must be passed explicitly. The
  hive wiring supplies it from hive config. Remove the `modal_prefix()` fallback from
  `docs`/`planning` too (bh-ear6v alignment).
- **Idempotency**: unit key → `external_ref` → B5 step 1 makes a re-run PRESENT. Existing engine
  second-dry-run proof covers it.
- **Upsert**: unchanged `reconcile` → `Plan` → `BackfillEngine.apply`. NEW creates closed beads;
  DRIFT appends links/stamps; nothing is ever overwritten.
- **Gapfill on new beads** (#143.6): after apply, optionally run timeline gapfill scoped to the
  created ids (`backfill.timeline.gapfill.on_created=false` default), and document when to use it.

### Surfaces

- CLI (hive wiring in `src/beadhive/backfill_cli.py`):
  - `bh backfill history [--sources git,docs,planning,github] [--until parse|collate|plan|apply]`
    is the single entry and writes stage outputs to a persistent run dir.
  - `bh backfill collate` / `bh backfill inspect <run>` show the review table (units,
    NEW/PRESENT/NOISE/residual, conflicts, xrefs, cost).
  - `bh backfill judge setup` installs the extra, wires the key from the secret store, and
    runs a one-call probe.
  - Existing `commits|docs|planning` passes stay working, untouched in phase 1.
- Neutral CLI: `beadlog read <reader>`, `beadlog collate`, `beadlog extract` work in a bh install:
  register the entry points in the **bh wheel's** metadata too, plus a static built-in registry
  fallback in `cli.py` so discovery never depends on which dist is installed. Document it and
  install the `beadlog` console script.
- Skill: `bh:backfill` calls `bh backfill history`, and `reconcile.sh` is deleted (cross-repo
  change in the skills repo).

### Config (all default off / inert)

```toml
[backfill.history]      enabled = false; sources = ["git", "docs", "planning"]
[backfill.sources.docs] formats = ["markdown"]  # add "html"; layouts = [...]
[backfill.sources.github] enabled = false
[backfill.collate]      create = "off"          # "propose" = make closed beads from units
                        include_types = ["feat"]; areas = {}; new_threshold = null
[backfill.judge]        (existing) + cost_per_call_usd, resume = true
[backfill.timeline.gapfill] on_created = false
```

### Third-party extraction tools (adopt vs. defer)

These were surveyed 2026-11-11 by an analyst pass against PyPI and GitHub (V = verified, I = inferred).
None of the tools named here were existing repo choices before this survey. Rule: adopt where a maintained,
permissively licensed parser removes real work; keep `beadlog-sources`' base dependency set at
`beadlog` only, so adoptions come in as **optional extras with a stdlib fallback**.

| Need | Decision | Why |
|---|---|---|
| Markdown structure (headings, tokens, sections such as `## Status`) | **Adopt** `markdown-it-py` (MIT, active, V) as extra `beadlog-sources[md]`; regex fallback when absent | Gives a CommonMark token stream with one small dependency. Replaces ad-hoc heading regexes in the shared docs parser. |
| YAML frontmatter | Hand-parse the `key: value` subset; PyYAML only inside `[md]` if needed | `python-frontmatter` only wraps PyYAML |
| HTML title / h1 / meta / status line | **stdlib `html.parser`** (≈40 lines); `selectolax` (MIT, V) as a later extra only if real-world docs need it | The docs are the repos' own pages, so there is no boilerplate to strip. trafilatura/justext would be overkill. |
| Git history | Keep **one `git log -z` subprocess** | GitPython still shells out; pygit2 is GPL+linking-exception (flagged); `dulwich` (Apache-2.0, V) is a later adapter only if the git binary must go |
| Conventional Commits + grouping | **Write** (a one-regex spec; grouping is ours) | commitizen/semantic-release are heavy CLIs. Optional later: a `git-cliff --context` JSON reader (Apache-2.0, V). |
| ADR / MADR / Nygard status | **Write** on top of the md parser | pyadr is stale (2022, V). adr-tools and log4brains are CLIs, not parsers. |
| GitHub PRs/issues | **No new library**: tracker-github transport / `gh … --json` produces the snapshot | PyGithub is LGPL (I) and adds nothing |
| PDF/DOCX → text | **Defer**: MarkItDown (MIT, V) as a future optional reader | No PDF or DOCX input in scope. docling/unstructured are heavy. pandoc is GPL. |
| Backlog seeding | **Defer** stringer (MIT, Go, V) | It extracts forward-looking debt (TODOs, churn), not history. At most a later `debt-seed` reader of its JSON output. |

Never adopt: pygit2 (GPL+exception), pandoc (GPL), PyGithub (LGPL, inferred).

### Stretch (future work, not in this workstream): LLM-assisted extraction with BAML + Jev

For unstructured content the deterministic readers can't shape (status stated in prose, implied
deliverables in a design narrative, informal roadmap docs), add a **`semantic` reader adapter**:

- **BAML** (BoundaryML; Apache-2.0, `baml-py` with no listed dependencies, V)
  declares each category shape (the Source record categories table) as a typed BAML class, and
  an LLM fills
  `attrs`/`refs` for one artifact at a time;
- **Jev** (TypeSafe System One, local plugin MIT v0.6.2, V) sits behind the existing
  `JudgmentPort` and answers narrow typed questions about the extraction ("is this doc's status
  *delivered*?", "does this paragraph describe a separate deliverable?") with calibrated
  probabilities. Deterministic code keeps the decision table;
- outputs are `Artifact`s with `provenance: inferred` + `confidence`, **cached by content hash**
  and recorded in the evidence corpus, so re-extraction is replayable and offline (the same rule
  as judge verdicts today);
- off by default, network-requiring, opt-in extra; it reuses the category shapes unchanged, so
  nothing downstream changes.
Proposed as a follow-on spike (GO/NO-GO on extraction quality against the M5 trial hives) after
the workstream lands; not filed now.

## Decisions (accepted 2026-11-11)

| # | Question | Decision (accepted) |
|---|---|---|
| D1 | Label on created beads, given bh-l1m3p (`origin:` read as "adopted report") | Land the bh-l1m3p fix first and keep `origin:backfill`. `planning` already writes it, so a new label splits the corpus. Make bh-l1m3p a blocker of the Phase C create bead only. |
| D2 | Judge bundling | Keep `[haiku]` an extra. Ship `bh backfill judge setup` (install + secret-store key + probe) as one step. The port is already Jev-shaped, so bundling buys little and changes the dependency posture. |
| D3 | Evidence IR outside the BeadLog format | Yes: a separate `bdlg.evidence/1` JSONL, so the format stays stable. |
| D4 | Converge old passes now? | No. The new pipeline lands alongside. A final molecule rewires `commits/docs/planning` onto readers with golden-equivalence tests. |
| D5 | stringer (bh-d89mc) as an alternative seed | Don't depend on it. If adopted, it becomes one more reader adapter later. |
| D6 | GitHub reader home | `beadhive-tracker-github` plugin (it owns auth/transport), as a snapshot-then-parse pair. |

## Decomposition (workstream)

Shaped as sub-epics under a parent epic, each landing on main.
Critical path: **M0 → (M1 ∥ M2 ∥ M4) → M3 → M5**.
M2 can build against synthetic evidence fixtures as soon as M0 lands, without waiting for M1.

### M0: Contracts and discovery (small; unblocks everything; land first)

- Artifact/Ref/SourceReader/ReadRequest + `bdlg.evidence/1` schema, JSONL I/O, fixtures kit
- `LedgerDirectory` port + in-memory fake
- Entry-point registration in the bh wheel + static registry fallback; `beadlog` console script; docs
- Explicit-prefix requirement: drop the `modal_prefix` fallback, with hive wiring passing the prefix

### M1: Readers (parallel beads, each independent after M0)

- M1a git reader (commits, merges, tags/releases, CC parse)
- M1b docs reader: `DocFormat` port, markdown adapter (port), html adapter, metadata
  normalization, layout presets
- M1c planning reader: `PlanningFormat` port, gsd adapter (port), roadmap adapter
- M1d github snapshot fetch + reader (tracker-github plugin)
- M1e LedgerDirectory adapters: bd routes, HQ hive registry

### M2: Collation engine (sequential-ish beads in one package)

- B1+B2 index + resolve (local / foreign / unresolved)
- B3 clustering rules + area grouping + per-hive override
- B4 doc attachment
- B5 identity: bridges → judge → threshold → residual; auto-prior resume; cost preview
- B6 status/date derivation + conflict detection

### M3: Projection and upsert (blocked by M2; create bead blocked by bh-l1m3p per D1)

- Projector units → events; external_ref keys; xref/lifecycle/conflict metadata
- `create = propose` mode wired through reconcile/engine; empty-hive allowed in this mode
- `bh backfill history` / `collate` / `inspect` CLI + config models + CONFIGURATION.md
- Gapfill `on_created`

### M4: Judge ergonomics (parallel with M2)

- `bh backfill judge setup` (secret-store key, probe), cost-per-call config, `inspect` verdict
  view (NEW/PRESENT/NOISE)

### M5: Convergence and release

- Rewire `commits/docs/planning` passes onto readers, with golden equivalence against current
  outputs
- Skills repo: `bh:backfill` calls `bh backfill history`; delete `reconcile.sh`
- End-to-end trials on three fixture hives mirroring the reports: commit-only (expect ≈ epics +
  feat issues), docs-heavy judge-off (threshold > 0 NEW), HTML-documented (11 docs found).
  Then a minor release (feat).

## Consequences

- New code concentrates in Phase A (readers) and Phase B (collate); the Phase C safety
  envelope is reused unchanged.
- New sources need only a reader emitting an existing category; no new collate or projection code.
- The evidence corpus (`bdlg.evidence/1`) is an intermediate artifact, so the BeadLog format stays stable.
- Existing `commits|docs|planning` passes keep working until the M5 convergence step (D4).
