# Bead backend abstraction — pluggable engines over the JSONL interchange (design)

> Status: **design / intent.** Nothing here is built. It turns the roadmap line in the
> beadhive-concepts storage model — *"a rig selects its engine with `beads switch <bd|br|nodb>`
> while every other verb stays identical"* — into a phased plan, and defines the
> **permissions reference** a remote factory identity needs to pull/push bead state.
> Model comparison lives in [BEAD-BACKENDS](../BEAD-BACKENDS.md); how bd state moves today is
> [BEADS-SYNC](../BEADS-SYNC.md).

## Goal

1. Let a rig run on any beads-compatible engine — `bd` (Dolt), `br` (in-branch JSONL), `bw`
   (orphan branch), `nodb` (bd JSONL-only) — with `bh` verbs unchanged.
2. Keep Factory HQ working regardless of a rig's engine, by making the **JSONL interchange**
   the only thing the hub depends on.
3. Document, per engine, the exact credentials a **remote factory** (its own git identity, no
   human keychain) needs to pull and push bead state.

**Non-goals:** reimplementing any tracker; migrating this rig off bd; live cross-engine
mirroring of one rig (an engine is a per-rig choice, switched explicitly); syncing engines'
extra-schema data (Dolt history, bw intent log) across engines — the interchange is issues
only.

## The seam

Every bead operation in `bh` today is a literal subprocess call — `run(["bd", ...])` in
`src/beadhive/bd.py` (passthrough/create/import), `hub.py` (export, repo add/sync, bootstrap),
`report.py` (create, set-state, dolt commit/push), `work.py` (lifecycle verbs), `plan.py` /
`adopt.py` (import). There is no backend indirection; `bd` is also a fixed entry in
`setup.py`'s probe table.

The precedent to copy is `dolt.py`'s container backend: a config key (`dolt.backend =
colima|docker|podman|none`) selecting a thin dispatch — "a few lines, no new file" per
backend, not a plugin framework.

**Proposed:** an `Engine` protocol with exactly the operations `bh` itself needs — not a
wrapper for every tracker verb:

```python
class Engine(Protocol):
    name: str                                   # bd | br | bw | nodb
    def passthrough(cwd, args): ...             # issue management (create/list/dep/close)
    def export_jsonl(cwd, path): ...            # → interchange (hub hydration, adopt)
    def import_jsonl(cwd, path): ...            # ← interchange (plan/adopt/report birth)
    def push_state(cwd): ...                    # publish authoritative state
    def pull_state(cwd): ...                    # refresh from authoritative state
    def bootstrap(cwd): ...                     # fresh-clone hydration
    def state_channel(cwd) -> str: ...          # e.g. "refs/dolt/data" | "<code branches>" | "beadwork"
```

Selection is per-rig: a `beads.engine` key (new `beads:` section in `config.py`
`KNOWN_SECTIONS`, default `bd`) and/or a field on the rig's `managed_repos` entry, mirroring
how `kind` is already per-rig. The engine binary joins the setup probe table per configured
engine. Passthrough (`bh bd …`) stays spelled `bh bd` — it routes to the rig's engine; the
identity-triplet injection in `bd.py:augment_labels` moves into the shared import path so it
applies to every engine.

Two federation ops joined the seam (bh-wty3.1): `federation_status(cwd, *, timeout)` — the
read-only per-peer sync picture (`bd federation status --json`: reachability, ahead/behind,
conflicts; a real network fetch, callers own when to pay it) — and `sync_state(cwd, *, peer,
strategy, timeout)` — bidirectional sync (`bd federation sync`), which with conflicts and no
strategy reports `paused` plus the conflicted tables. Both return frozen result dataclasses
(`FederationStatus`/`SyncOutcome`), parse bd's JSON defensively, and never coerce a failure
or an unreachable peer into looking in-sync.

Mapping per engine:

| Op | bd | br | bw | nodb |
|---|---|---|---|---|
| push_state | `bd dolt push` | `git add .beads/ && commit` + push (the ONLY place bh runs git for br) | `bw sync` | git add/commit/push |
| pull_state | `bd dolt pull` | git pull + `br sync --import-only` | `bw sync` | git pull + noop |
| bootstrap | `bd bootstrap` (probes `refs/dolt/data`) | clone has it (tracked file) → `br sync --import-only` | fetch `beadwork` ref | clone has it |
| export/import | `bd export` / `bd import` | `br sync --flush-only` / `--import-only` | `bw export` / `bw import -` + **field-map shim** | file *is* the interchange |

## Phase 1 — interchange interop (no engine adapters yet)

JSONL is already the contract `bh sync` uses (`hub.py`: per-rig `bd export -o
.beads/issues.jsonl` → `bd repo add/sync`). Phase 1 hardens that contract so a rig running a
foreign engine is at least *visible* to the factory:

1. **Interchange schema doc** — pin the JSONL fields bh relies on (id, title, status, labels
   incl. the `provider:`/`org:`/`repo:` triplet, deps, parent, assignee, timestamps), which
   engines emit them natively, and what is lossy. Home: `docs/schemas/`.
2. **bw field-map shim** — a pure translation (owner↔assignee, issue_type↔type,
   created_at↔created, deps flattening) applied on import/export when the peer is bw. Reuse
   `bd.py:augment_labels` for triplet injection.
3. **Hub hydration from any engine** — `bh sync` learns: if a rig's `.beads/issues.jsonl` is
   tracked (br/nodb) or exportable (bw), hydrate the hub from that instead of assuming
   `bd export`/`refs/dolt/data`. Uncloned-rig fetch gains "fetch the `beadwork` ref" and
   "blobless fetch of the tracked JSONL" alongside today's `refs/dolt/data` path.
4. **Guardrail for in-branch state** — `bh` warns when bead state is a *tracked* file inside a
   `wt/` worktree (br/nodb rigs), since lifecycle writes there entangle with the code diff
   (see [BEAD-BACKENDS §4](../BEAD-BACKENDS.md#4-agf-fit)).

Phase 1 exit: an HQ `ready` view that includes a demo br rig and a demo bw rig.

## Phase 2 — per-rig engine selection

1. **The `Engine` seam** above, with `bd` as the first (extraction-only) implementation —
   behavior identical, all call sites routed through it.
2. **`bh beads switch <bd|br|bw|nodb>`** — the roadmap verb: export via current engine →
   init target engine → import → update rig config → verify counts. Refuses on dirty state.
3. **br / bw / nodb adapters** per the mapping table.
4. **Lifecycle mapping for `bh work`** — which verbs each engine honors: bd/bw have a state
   channel so assign/claim/submit can push/pull it; br/nodb degrade to "local-only until the
   branch lands" with the guardrail warning. This depends on the pre-existing
   [BEADS-SYNC gap](../BEADS-SYNC.md#what-exists-vs-gaps): state push/pull is not yet wired
   into `bh work` even for bd — wire it through `Engine.push_state/pull_state` so every
   engine gets it at once.
5. **Cross-worktree contract per engine** — bd: shared main-clone DB (`.beads/redirect`);
   bw: shared object DB (nothing to do); br/nodb: document divergence, no shared live view.

## Permissions & credentials reference (remote factory)

A factory seat on a remote host has **no human keychain**: it authenticates as its own git
identity (deploy key or fine-grained PAT) injected at provision time. What that identity needs,
per engine:

| | Pull state | Push state | Narrowest viable credential |
|---|---|---|---|
| **bd / Dolt (git remote)** | fetch `refs/dolt/data` → repo **read** | push `refs/dolt/data` → repo **write** (Contents: read/write) | deploy key or fine-grained PAT on the one repo. **Cannot** be scoped below the whole repo — a token that pushes the Dolt ref can push branches. Branch protection does not cover `refs/dolt/data`; verify the forge permits non-standard ref namespaces (GitHub/Gitea do). |
| **bd / Dolt (alt remote)** | DoltHub creds / S3 `aws://` / GCS `gs://` ambient creds / `file://` | same | separates bead-state creds from code creds entirely — the option when the factory must sync issues but must NOT hold repo write. bd auto-materializes CLI remotes from shell creds. |
| **br / nodb** | repo read (state is repo content) | **push to code branches** — broadest surface; the identity can alter code by construction | none narrower than repo write; pair with forge push rules if available |
| **bw** | fetch `beadwork` branch → repo read | push `beadwork` only | repo write, but forge-side scoping works: `beadwork` is an ordinary branch, so push allowlists/protected-branch rules can pin the identity to exactly that branch — the narrowest of the git-remote options |

Mechanics that already exist and are reused, not reinvented:

- **Transport = git.** The rig's `sync.remote` (`.beads/config.yaml`, committed so fresh
  clones can bootstrap) rides normal git auth — whatever key/token the provisioned identity
  holds. No bespoke token handling in bh.
- **Dolt server secrets** (only if the optional [DOLT](../DOLT.md) server is in play):
  `BEADS_DOLT_PASSWORD` / `DOLT_ROOT_PASSWORD` via `~/.ws/.env` (`dolt.py`).
- **bd federation key**: `.beads/.beads-credential-key` — never committed; if federation is
  used it must be injected like the signing key.
- **Guard policy** (`guard.py`): today `bd github push/sync` is gated (contrib seat,
  single-issue only) and the hub is read-only, but **`bd dolt push` is ungated**. Proposal:
  keep state-push ungated for rig seats (it is the sanctioned channel) but add a
  `--readonly`/`--sandbox` default for untrusted worker sandboxes — bd already supports both
  flags; bh should set them when provisioning a sandboxed developer.
- **Unbuilt, inherited from BEADS-SYNC gaps**: remote key *injection* (paths are meaningless
  off-host) and per-rig developer bootstrap ("give this host just rig X's beads").

## Bead breakdown (the molecule to file)

Epic: **bead backend abstraction & permissions** (this doc). Children, `→` = depends-on:

| # | Bead | Phase |
|---|---|---|
| 1 | Interchange schema doc under `docs/schemas/` (fields, lossiness, per-engine support) | 1 |
| 2 | bw field-map shim (import/export translation + triplet injection) → 1 | 1 |
| 3 | Hub hydration from foreign-engine JSONL (tracked-file + `beadwork`-ref fetch paths) → 1 | 1 |
| 4 | Guardrail: warn on tracked bead state inside `wt/` worktrees | 1 |
| 5 | `Engine` seam: extract bd behind the protocol, `beads:` config section (no behavior change) | 2 |
| 6 | Wire state push/pull into `bh work` verbs via `Engine` (closes the BEADS-SYNC gap) → 5 | 2 |
| 7 | `bh beads switch` verb (export → init → import → reconfigure → verify) → 5 | 2 |
| 8 | nodb adapter → 5 | 2 |
| 9 | br adapter + in-branch lifecycle degradation → 5, 4 | 2 |
| 10 | bw adapter → 5, 2 | 2 |
| 11 | Permissions reference hardening: sandbox `--readonly`/`--sandbox` defaults in provisioning + factory-credential doc per forge → 6 | 2 |

## Open questions

- Does `bh` ever run git *for* a br rig (push_state), or does br's "git is your job" stance
  mean br rigs only sync when a human/merger lands the branch? Leaning: bh may commit/push
  `.beads/` on the **integration branch only**, never on `wt/` branches.
- Is bw's `labels` concept rich enough to carry the identity triplet natively, or does the
  shim encode it in the JSON payload?
- Should `beads.engine` live in Head Office (`managed_repos`) or in the rig
  (`.beads/`-adjacent, travels with the repo)? Leaning: the rig — engines are a property of
  the repo, like `sync.remote`.

See also: [BEAD-BACKENDS](../BEAD-BACKENDS.md) ·
[br-agf-fit-and-state-compat-layers](br-agf-fit-and-state-compat-layers.md) (br verdict, tier
envelope, and the State-Ref Contract that refines this design) · [BEADS-SYNC](../BEADS-SYNC.md) ·
[DOLT](../DOLT.md) · [HUB](../HUB.md) ·
`plugins/agf/skills/beadhive-concepts/references/storage-model.md` (the roadmap paragraph this
design implements).
