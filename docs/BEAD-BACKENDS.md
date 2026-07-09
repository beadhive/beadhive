# Bead backends — bd (Dolt), br (in-branch JSONL), bw (orphan branch), nodb

*A reference comparing the storage/sync models of the beads-compatible trackers, and how each
tolerates AGF. Companion to [BEADS-SYNC](BEADS-SYNC.md) (how `ws` moves bd state today) and
the multi-backend design doc at [design/bead-backend-abstraction.md](design/bead-backend-abstraction.md).*

Sources: upstream beads docs (`steveyegge/beads` — DOLT.md, SYNC_CONCEPTS.md, GIT_INTEGRATION.md,
WORKTREES.md, CONFIG.md, CHANGELOG.md), `Dicklesworthstone/beads_rust` (README, SYNC_SAFETY.md,
VCS_INTEGRATION.md), `jallum/beadwork` (README, design.md, migration.md); fetched 2026-07-07.
Local ground truth: this rig's `.beads/` on bd 1.0.5 embedded Dolt.

All four engines share (or map to) the **JSONL interchange** — a line-per-issue export that is
the stable contract between them. Where they differ is *where authoritative state lives* and
*how it travels*.

---

## 1. The four models

### bd — Dolt on a hidden git ref (what this rig runs)

Upstream beads stores issues in **Dolt** (a versioned SQL database with git-like commits/refs).
Two modes: **embedded** (default since v1.0 — in-process engine under `.beads/embeddeddolt/`,
single writer) and **server** (`bd dolt start`, multi-client, `.beads/dolt/`). Neither is ever
committed to git.

State travels on **`refs/dolt/data`** — a ref namespace on the *same git remote as the code*,
disjoint from `refs/heads/*`. Upstream: *"Dolt stores data under `refs/dolt/data`, separate from
standard Git refs"*; *"beads does not commit to any Git branch, so protected branch workflows are
not affected."* This is the "hidden partition": it rides the repo's remote and its mirrors, but
`git clone`/`git pull` don't fetch it — a fresh clone runs `bd bootstrap`, which probes origin
for the ref and clones the database from it. Sync is explicit: `bd dolt push` / `bd dolt pull`
(the auto-sync daemon was removed in v0.59).

Supported Dolt remotes: `git+ssh://` / `git+https://` (same repo as the code — this rig's
`sync.remote`), DoltHub (`doltremoteapi.dolthub.com`), S3 (`aws://`), GCS (`gs://`), and local
`file://` remotes. `bd init` auto-configures a Dolt remote named `origin` when a git origin
exists.

**JSONL's role: export only.** Upstream CONFIG.md: *"Older releases briefly made
`.beads/issues.jsonl` look like the default git-tracked source of truth; current releases treat
it as an optional export for viewers, interchange, and issue-level migration."* Exports are
lossy — no Dolt branches, commit history, or non-issue tables.

**Transition history** (why version matters for compatibility):

| Version | Change |
|---|---|
| ≤ 0.49 | "Classic": SQLite cache + git-committed JSONL source of truth, daemon, merge driver, hooks |
| 0.50 | Dolt backend introduced |
| 0.57 | SQLite backend removed — Dolt only |
| 0.59 | Daemon fully removed; minimum for documented Dolt sync |
| 0.62 | JSONL auto-export/auto-staging become opt-in (default off) |
| 0.63.3 / 1.0 | Embedded Dolt is the default everywhere; no server lifecycle needed |
| 1.0.5 | Dolt "primary datastore"; `issues.jsonl` "an optional export" (this rig's version) |

### br — beads_rust: in-branch JSONL, git ops are yours

`br` deliberately **freezes the classic pre-Dolt architecture**: gitignored SQLite cache
(`.beads/beads.db`) + **committed** `.beads/issues.jsonl` as the collaboration surface, plus
`.beads/beads.base.jsonl` (a merge-base snapshot for three-way merge). Its documented best
practice commits issue state as ordinary tracked files on whatever branch you're on:

```sh
br sync --flush-only                 # optional final export check before git commit
git add .beads/ && git commit -m "Update issues"
```

The philosophy is explicit non-involvement: *"br keeps its state in `.beads/` and leaves git
handoff to you. It never commits, pushes, pulls, installs hooks, or runs as a background
service."* Every sync direction is an explicit flag: `--flush-only` (DB→JSONL), `--import-only`
(JSONL→DB), `--merge` (three-way against the base snapshot), `--import-only --rebuild` (JSONL
authoritative). Importing a file containing unresolved git conflict markers is hard-blocked with
no override, and path validation refuses to write outside `.beads/`.

So with br, **issue state rides the code branch by default** — the same commit can atomically
carry a change *and* its bead update, and diverges per-branch exactly like code does.

### bw — Beadwork: an orphan branch, intent replay

`bw` (jallum/beadwork; the closest match to "BeadWorks") puts *all* issue state on a dedicated
**git orphan branch named `beadwork`**, manipulated directly in the git object database via
go-git: *"Nothing touches your working tree or index."* Each issue is one JSON file; status,
labels, dependencies, and parent links are **zero-byte marker files** in a directory hierarchy
(`issues/bw-a1b2.json`, `status/open/bw-a1b2`, `blocks/<blocker>/<blocked>`, …). Every
operation is an atomic commit whose structured message doubles as a replayable **intent log**.

Sync: *"`bw sync` fetches, rebases, and pushes. If rebase conflicts, it replays intents from
commit messages against the current remote state. No merge drivers, no lock files, no custom
conflict resolution."* Concurrency is also designed away at the data layer — one file per
issue plus marker files means *"two agents working on the same repo never touch the same file."*

bd compatibility is an **interchange mapping, not the same schema**: `bd export | bw import -`
preserves IDs, dependencies, and parent-child links but renames fields (`owner`→`assignee`,
`issue_type`→`type`, `created_at`→`created`) and flattens dependencies into
`blocks`/`blocked_by`/`parent`.

### nodb — bd's JSONL-only mode

bd's own degenerate engine: `no-db: true` in `.beads/config.yaml` makes `.beads/issues.jsonl`
the only local store — no Dolt at all. Storage-wise it behaves like br's committed-JSONL model
(state is a tracked file), with bd's CLI surface. Useful as the zero-infrastructure floor and
as the reference implementation of "the interchange *is* the store."

---

## 2. Comparison matrix

| Axis | **bd / Dolt** | **br** | **bw** | **nodb** |
|---|---|---|---|---|
| Authoritative store | Dolt DB (`.beads/embeddeddolt/`, gitignored) | committed `.beads/issues.jsonl` (+ local SQLite cache) | JSON/marker files on orphan branch `beadwork` (object DB only) | committed `.beads/issues.jsonl` |
| On-disk footprint | full versioned history (~tens of MB; blobless clone of just the ref is ~tens of MB/rig) | JSONL ~1 MB/1.2k issues (this rig) + SQLite cache | one blob per issue + near-free markers; history = commits on one ref | JSONL only |
| Sync mechanism | explicit `bd dolt push/pull` of `refs/dolt/data` | none — ordinary `git add/commit/push` by you | `bw sync` = fetch → rebase → push of its branch | ordinary git, like br |
| Bi-directional sync | ✓ (push/pull; cell-level Dolt merge) | ✓ via git merge of JSONL + `br sync --merge` | ✓ (rebase + intent replay) | ✓ via git merge |
| Sync issues **without** code | ✓ — ref-only; never touches branches | ✗ — state rides code commits (issue-only commits possible but same branch) | ✓ — separate branch, fully independent | ✗ — same as br |
| Atomic code+issue commit | ✗ by design | ✓ — the model's core benefit | ✗ by design | ✓ |
| Conflict model | Dolt cell-level merge; hash IDs prevent collisions | git line-merge on JSONL ("keep both lines"), then 3-way vs `beads.base.jsonl` with explicit `--force-*` policy | deterministic intent replay from commit messages | git line-merge; `bd merge` driver exists for JSONL |
| Daemon / hooks | none since v0.59 (bd hooks exist but optional) | none, ever; hooks strictly manual | none documented | none |
| Runs git itself | Dolt transport only (never commits to branches) | never | yes — its own branch only | never |
| Permissions to pull state | git **read** on the repo (fetch `refs/dolt/data`), or read on the alt remote (DoltHub/S3/GCS) | git read (state is repo content) | git read | git read |
| Permissions to push state | git **push to a non-branch ref** — see caveats below | push to the **code branches themselves** (broadest) | push to **one named branch** (`beadwork`) — narrowest scopeable | push to code branches |
| Cross-worktree behavior | one DB in the main clone, shared via `.beads/redirect`; state never on branches → worktrees always agree | each worktree checks out its branch's JSONL + builds its own SQLite → **state diverges per branch** until git-merged | single state in the shared `.git` object DB → all worktrees see identical state (cleanest) | same as br |
| JSONL compat with bd | native (`bd export`/`import`) | same schema as *classic* bd — direct file copy works | mapped fields (owner→assignee, …) via import/export | native |
| Multi-writer story | embedded = single writer; server mode for true concurrency | single local writer; concurrency = git branches | file-per-issue → concurrent agents rarely conflict | single writer |

**Ref-push caveat (bd).** `refs/dolt/data` is outside `refs/heads/*`, so branch protection
doesn't apply to it — but some forges/tooling restrict non-standard ref namespaces, and
GitHub fine-grained PAT "Contents: write" is repo-wide (you cannot scope a token to only the
Dolt ref). A deploy key or PAT that can push the Dolt ref can also push branches. bw is the
opposite: its state ref is an ordinary branch, so forge-side rules (protection, push
allowlists) can scope it precisely.

---

## 3. Edge cases

The "beads don't cleanly map to a branch every time" intuition is real; here is where each
model bleeds:

- **br: bead state dies with its branch.** Close a bead on a feature branch that is later
  abandoned or reverted, and the close is lost (or resurrected by the revert). Update the same
  bead on two branches and you get a real merge decision — br punts it to git plus an explicit
  `--force-db|--force-jsonl|--force` policy. Cross-cutting beads (an epic touched from five
  feature branches) have no single home until all branches land.
- **br: worktree divergence.** Under AGF each `wt/bead/<id>` worktree checks out its own JSONL
  and builds its own SQLite, so "what's in progress?" has a different answer per worktree until
  merge. There is no shared live view.
- **bd: orphaned parents.** Deleting a parent whose children still reference it breaks
  hydration ("parent issue bd-abc does not exist"); upstream's fix is auto-resurrecting deleted
  parents as closed. Legacy artifacts also linger: pre-0.59 sync-branch worktrees under
  `.git/beads-worktrees/` and stale hooks need manual cleanup.
- **bd: JSONL export is lossy.** Exports capture issue rows only — no Dolt history, branches,
  or working-set state. `bd import` of a stale JSONL cannot detect deletions; upstream warns
  against using import as a substitute for `bd dolt pull`.
- **bw: rebase-replay is last-writer-wins per intent.** Replay is deterministic, but two
  agents editing the same field of the same issue resolve by intent order, not by a merge
  policy you choose. Attachments/comments (separate files) are safe; scalar field races are
  silent.
- **All: the interchange is the lowest common denominator.** Anything an engine stores beyond
  the JSONL schema (Dolt audit history, bw labels, br base snapshots) does not survive a
  round-trip through another engine.

---

## 4. AGF fit

AGF's storage invariant ([BEADS-SYNC](BEADS-SYNC.md)): **two disjoint git-native channels** —
the `wt/bead/<id>` branch carries the change, a state channel carries the bead — so lifecycle
writes (assign/claim/submit/close) never collide with code merges, and the dispatcher/developer
handoff is "push a ref, pull a ref."

- **bd/Dolt — fits today.** `refs/dolt/data` *is* the state channel; the assign → claim →
  submit → merge choreography in BEADS-SYNC is built on it. Worktrees share the main clone's
  DB, so every seat sees one truth locally, and remote seats pull the ref.
- **bw — fits structurally.** The `beadwork` branch plays exactly the `refs/dolt/data` role
  (independent state ref, syncs without code, one shared state across worktrees), with a
  *narrower* permission surface and ordinary-git observability (the state is `git log`-able).
  The gaps are schema mapping (no identity-triplet labels natively — though bw adds a `labels`
  concept bd's classic schema lacks) and no gate/lifecycle vocabulary — `ws work`'s verbs
  would need mapping onto bw's status markers.
- **br — conflicts with the invariant.** Claim/submit/close mutate a tracked file, so every
  lifecycle step either dirties the developer's `wt/` worktree (bead state entangled with the
  code diff — the atomic-commit benefit, but now the *dispatcher's* assign writes and the
  *developer's* claim writes race on the same line of the same file across branches) or
  requires a dedicated issue-only commit lane. Mitigations if a rig chooses br anyway:
  issue-only commits on the integration branch (serialize through the merger), or a dedicated
  sync branch for `.beads/` (recreating classic bd's `--branch beads-metadata` pattern —
  effectively hand-rolling bw's model). The refinery must also expect `.beads/issues.jsonl`
  conflicts at merge time; they are line-mergeable but they are *its* problem now.
- **nodb — same posture as br**, minus the SQLite cache. Acceptable for a single-seat
  prototype rig; not for multi-seat AGF.

**Bottom line:** for AGF the ranking is bd (built) ≥ bw (structurally sound, needs mapping) ≫
br/nodb (in-branch state fights the flow; usable single-seat or with a sync-branch pattern).
For *interchange* — hub hydration, migration, viewers — all four meet at the JSONL contract.

See also: [BEADS-SYNC](BEADS-SYNC.md) · [DOLT](DOLT.md) ·
[design/bead-backend-abstraction.md](design/bead-backend-abstraction.md) (the plan to support
these engines per-rig, and the permissions reference for factory credentials) ·
[design/br-agf-fit-and-state-compat-layers.md](design/br-agf-fit-and-state-compat-layers.md)
(the br by-design verdict, limited-capacity tiers, and invented compat layers).
