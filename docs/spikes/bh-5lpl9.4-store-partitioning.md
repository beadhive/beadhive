# Spike bh-5lpl9.4 — partitioned internal/external bead stores per repository

**Verdict:** **GO** · **Date:** 2026-10-16 · **Decided in:**
[two-store-scope-adr.md](../design/two-store-scope-adr.md) (`bh-lniwf.1`)
**Probes:** `bd` 1.3.0 (`f45b249ce`), throwaway scratch repositories, a `file://` Dolt remote
standing in for an object store. No live store, remote, bucket or credential was touched.

## Question

Can one code repository carry two bead stores — an **internal** store holding the full
Beadhive planning and execution corpus, private, and an **external** (public) store that is a
lossy projection of public discourse such as GitHub Issues — without forking `bd`, without
losing the replication safety a single `refs/dolt/data` store has today, and without losing
bead history? The seven sub-questions are in the bead; each is answered below.

## Method

Each answer was checked against the real `bd` binary in a scratch directory. The commands are
reproducible; `$S` is a fresh `git init` directory.

```sh
cd $S && bd init --prefix prb --quiet                   # internal store at $S/.beads
mkdir ../outside && cd ../outside && git init -q && bd init --prefix prbpub --quiet
cd $S && BEADS_DIR=$S/../outside/.beads bd create "public one" --json   # -> prbpub-dj2
cd $S && bd list --json                                  # -> prb-198, prb-pan only
cd $S && BEADS_DIR=$S/../outside/.beads bd list --json   # -> prbpub-dj2 only
cd $S && BEADS_DIR=$S/missing/.beads bd list --json      # -> prb-* (silent fallback!)
cd $S && BEADS_DIR=$S/pub/.beads bd init --prefix x      # -> "already initialized", aborts
cd $S && bd config set routing.mode explicit             # -> "(in config.yaml)"
bd dolt remote add private file://$R && bd dolt push --remote private
cd ../host2 && git init -q && bd init --prefix prb --remote file://$R   # second host
bd history prb-pan --json                                # host2: every version present
```

## Evidence and answers

| # | Question | Answer | Evidence | Classification |
|---|---|---|---|---|
| 1 | Can bd serve two stores for one working repo? | **Yes**, through `BEADS_DIR`, not `--db`/`--database`. | `BEADS_DIR` selected the detached store from the code cwd; reads and writes stayed separated. `--database` is documented "proxied-server mode only". Two hazards: a `BEADS_DIR` that does not exist **silently falls back** to cwd discovery, and `bd init` refuses to create a store inside an already initialized workspace. | Implemented here (`store_target.py`, `bh-lniwf.3`): bh verifies the target exists before running, and keeps detached stores outside the code checkout. |
| 2 | Can two stores share a prefix? | **No — use a second prefix.** | Ids are `<prefix>-<hash>` per store with independent allocation; nothing in an id says which store minted it, so a shared prefix makes every lookup by id ambiguous and every cross-store collision silent. | Implemented here (`bh-lniwf.2` validation: one prefix per store, unique fleet-wide). Namespace cost answered in the ADR §4 (`bh-vcfsb`). |
| 3 | What do hub / hydration do with two stores? | Hydrate **internal only**; `source_repo` stays meaningful. | `bd repo add` registers a path; the hub adds the hive's internal store path only, so a public record never appears as a second, dispatchable copy. | Implemented here (`hub.hydration_source`, `bh-lniwf.7`). Public-store provenance in the hub is deferred. |
| 4 | Migration of an existing store without losing history? | **No split is needed — GO.** The existing store *becomes* the internal store in place; the public store is a new projection filled from GitHub. Moving the internal store to a private remote is a full-history push/clone. | Pushed a store with a closed bead to a `file://` remote and cloned it on a second directory with `bd init --remote`; `bd history` shows every version on the clone. No bead is rewritten, renamed or exported. | Remote move: `bh-ksmt0` (out of scope tonight). No split: implemented by design here. |
| 5 | Default endpoint scheme? | **None blessed** in the store layer; a store names an HQ **remote descriptor** (`bh-b1scz`, `dolt_remote`). `git-origin` is reserved for "ride the code origin's `refs/dolt/data`". | Bucket paths must be distinct per database (beads' own rule); `bh-b1scz` owns URL shape, templating and credential boundary. | By `bh-b1scz`. The store side (a name, never a URL) is implemented here. |
| 6 | GitHub Issues fidelity? | Lossy by design: title, body, state, labels and the issue identity survive; bead types beyond bug/feature/task, closed dimensions, gates, dependencies and molecules do not. That is why public is a *projection*, never operational. | `bd github sync --pull-only` exists (`bd github sync --help`), keyed by `external_ref`. | Deferred to `bh-lniwf.6` (recorded fixtures). |
| 7 | Does any of this require forking bd? | **No.** Configuration plus bh-side plumbing. | Every mechanism above is stock `bd` 1.3.0. The one upstream gap is per-bead privacy, which partitioning works around. | Implemented here (no fork). |

## Verdict

**GO.** Partitioning is configuration plus bh plumbing. The single-store mode stays the
default and stays coherent: a hive with no `stores:` block behaves exactly as before, and a
partitioned hive's internal store *is* its previous store.

## Recommendation

Adopt the [two-store ADR](../design/two-store-scope-adr.md). Close this spike GO and let
`bh-5lpl9.6` link the implementation molecules `bh-lniwf` (stores), `bh-b1scz` (remotes) and
`bh-ksmt0` (remote migration).
