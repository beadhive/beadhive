# Hub — the cross-hive view

The **hub** is one aggregated beads DB holding a unified view of every registered hive, so you
can ask "what's ready anywhere?" — and so `bh` works on a machine with **no repos cloned**
(module: `hub.py`).

## The contract {#contract}

The hub is a **per-host DERIVED aggregate**. Five properties, and they are load-bearing rather
than descriptive — three other beads build on them, and `bd`'s own rule that a remote path
carries exactly ONE database is what makes them non-negotiable:

1. **Never authoritative.** Every bead in it arrived hydrated from some hive; that hive's own
   store is the truth. Nothing here is the last copy of anything.
2. **Never pushed, no remote of its own.** It is not a replica and has no peer. `bd dolt
   push`/`pull` against it are refused, not merely pointless.
3. **Rebuildable.** `rm -rf ~/.beadhive/hub && bh sync` reproduces it exactly, from every
   hive's own git remote. Losing it costs one sync. This is pinned by a test
   (`tests/test_hub_rebuild.py`) — it is the only thing that proves nothing authoritative is
   hiding in there.
4. **It ISSUES NO IDS.** An aggregate never creates a bead. The store's bd prefix is therefore
   deliberately not a plausible hive prefix: it is `_HUB_ISSUES_NO_IDS`, so a leaked id
   (`_HUB_ISSUES_NO_IDS-7`) self-identifies as a bug on sight. `bd init` demands *some* prefix
   string, so the enforcement that matters is the write-guard (`guard.guard_hub`), not the
   string. A punctuation sentinel is not available — measured against bd 1.1.0, `bd init
   --prefix '!hub'` is refused outright ("Database names must start with a letter or
   underscore…"), so the sentinel shouts inside bd's own alphabet instead. Hive prefixes are
   lowercase slugs derived from repo names, so a collision is impossible by construction.
5. **One-way hydration, not federation.** The hub is `bd repo add` / `bd repo sync` multi-repo
   HYDRATION: N different databases read one-way into one, each from its own derived
   `.beads/issues.jsonl`. bd's *hub-and-spoke* is a different, unrelated thing — full
   replication of the SAME database across peers. Same word, unrelated machinery. **Do not
   "upgrade" the hub to bd federation peers**: that would replicate databases that were never
   meant to converge.

The scope of the write-guard is the **hub**, not `~/.beadhive/cache/` generally. A per-hive
fetch cache under `cache/` is a legitimate place for *that one hive's* ids to be minted — `bh
report` against an uncloned hive creates a bead in its cache and pushes it back to the hive's
own remote, which is correct and stays working. It is only the aggregate, where a created bead
would belong to no hive at all, that issues nothing.

### Migrating an existing host

Nothing to do by hand, and **no Dolt surgery**. A hub minted before this contract carries the
plausible prefix `hub`; the next `bh sync` moves it aside to `~/.beadhive/hub.legacy-<epoch>`
and rebuilds prefix-less from every hive's own remote (`hub._retire_legacy_hub`). It is
renamed rather than deleted — bh does not remove an operator's data unprompted, even data it
considers disposable — so delete the moved copy when you want the disk back. A host whose
aggregate had moved into HQ is covered by [HQ](HQ.md#hub-vs-hq) instead.

## Where it lives

`~/.beadhive/hub/` (override `BH_HUB`, legacy alias `WS_HUB`), with minimal-clone caches at
`~/.beadhive/cache/` (override `BH_CACHE`, legacy alias `WS_CACHE`). It's a dedicated beads DB
the CLI owns — not tied to any code repo —
initialized on first `bh sync` (`hub.ensure_hub`).

## `bh sync`

Builds/refreshes the hub from `managed_repos`. For each hive:

- **cloned** (its `.beads/` exists under `$GIT_WORKSPACE`) → added by **local path**.
- **uncloned** → fetched into a **minimal-clone cache** and added by that path:
  `git clone --filter=blob:none --no-checkout <url>` (no working tree, blobless) +
  `bd bootstrap` (pulls `refs/dolt/data`) → just the beads data (~tens of MB/hive).
- then `bd repo sync` hydrates the unified view.

URLs for uncloned hives come from the git-workspace lock (exact; `gitworkspace.repo_urls`) or
are derived for github/gitlab (`git@<host>:<org>/<repo>.git`); a hive with neither is skipped
with a warning. Output summarizes `N cloned, M remote-cached, K skipped`.

`bh sync` itself (the standalone top-level command above) stays fully synchronous — it's an
explicit, operator-invoked refresh, so blocking until it finishes is the point. What changed
(bh-d5jhc.1) is the two places that used to run this SAME fleet-wide walk as a side effect of
an unrelated command:

- **`bh hive onboard`** exports + registers the ONE hive it just onboarded synchronously (that
  export is what a furnished hive's scaffold commit captures), then defers the fleet-wide `bd
  repo sync` aggregation to a best-effort background thread — the same in-process
  daemon-thread shape `metadata.py` uses for its own background reload (see
  [METADATA-CACHE.md](METADATA-CACHE.md)). `--hub-sync` opts back into waiting for the full
  refresh synchronously; `--no-hub-sync` skips the hub step entirely.
- **`bh hq push`** used to refresh the aggregate too, behind `--no-sync`/`--git-only` escape
  hatches. It no longer does anything of the kind (bh-89wxf.2), and both flags are gone with
  the walk they existed to dodge: publishing HQ has nothing to hydrate.

A deferred/background sync is best-effort by design: the work only needs to **start** before
the CLI process exits, a thread that dies with a short-lived process is fine, and a later `bh
sync` reconciles — the hub aggregate is always derived, never authoritative.

## `bh hub`

Query the hub — this host's derived cross-hive aggregate:

```sh
bh hub bd ready        # actionable work across all hives
bh hub bd list
bh hub intake          # director's fleet-wide untriaged-intake inbox
```

It errors with "run `bh sync` first" if the hub isn't initialized.

`bh hq bd …` is a DIFFERENT store — HQ's own authoritative `hq-`prefixed beads, not this
aggregate. The two were one code path until bh-89wxf.2 split them; see
[HQ — Hub vs HQ](HQ.md#hub-vs-hq).

## The hub is derived — never sync it directly

The hub holds **no authoritative state of its own**. It has no git remote, and every run of
`bh sync` treats it as disposable: wipe-and-rebuild from each hive's own `refs/dolt/data`, not
a merge. Do **not** `bd dolt push`/`pull` (or hand-edit) anything under `~/.beadhive/hub/`
expecting it to persist or propagate — it will not, and the next `bh sync` silently overwrites
it out from under you. If a hive's issues look wrong in the hub, the fix is always on the
hive's own remote, then `bh sync` again.

(The write-guard's allowance for `bh hq bd dolt push`/`status`/`remote list` — bh-ohx2 — does
not change this: those verbs are legitimate only because, once HQ is registered, `bh hq bd …`
targets `~/.beadhive/hq`, a REAL remote with real authoritative content, not the disposable
hub. Against a bare disposable hub with no remote configured, `bd dolt push` simply fails —
there is nothing to push to.)

Aggregation lands **here**, always — never in HQ. HQ's Dolt database carries only its own
authoritative `hq`-prefixed beads plus the git half; see [HQ — Hub vs HQ](HQ.md#hub-vs-hq).

### Why the incremental watermark stays (measured)

`bh sync` skips `bd export` for a hive whose Dolt HEAD commit is unchanged since the last
successful hydration (`hub._load_watermarks`). Whether to keep that at all was an open call,
because bd's own sync-concepts page is pointed about JSONL — it "cannot infer that records
absent from an export were deleted, pruned, or simply never exported" — which is exactly why
the hub must be *rebuildable* rather than incrementally trusted.

Measured on this host, 2026-08-19, across all 14 cloned hives (7 700 issues total):

| | total | per hive |
|---|---|---|
| `bd vc status --json` (what the watermark pays) | **3.6 s** | ~0.26 s, flat |
| `bd export` (what the watermark skips) | **5.9 s** | 0.18 s – 1.41 s, scales with issue count |
| net saving | **2.2 s** | |

**2.2 seconds is not a reason to keep a cache** — on that number alone the watermark would go.
It stays for a different, measured reason: suppressing the export leaves
`.beads/issues.jsonl`'s **mtime** untouched, which is what lets `bd repo sync`'s own mtime skip
fire for the hives the bulk fast path does not cover. That is the expensive one — `bd repo
sync` re-imports per issue with a recursive-CTE ancestry check per edge, measured at 4 212
issues in **655 s** (bh-z4z52). The watermark is load-bearing for the non-co-located remainder,
not for the 2.2 s.

It is also **sound in a way bd's mtime check is not**: it compares the hive's Dolt HEAD *commit
hash*, so "unchanged" is content-addressed rather than a filesystem guess, and the
JSONL-can't-express-deletion problem is downstream of it either way.

What DID change (bh-89wxf.1): the watermark file is keyed on the aggregate's `project_id` as
well as its path. Path alone could not see a hub wiped and re-minted under the same directory —
precisely the `rm -rf` property 3 invites — and would then report every hive "unchanged"
against a store holding nothing. That only survived before because bd's own downstream mtime
memory lived in the primary store and died with it; correctness resting on a second,
unrelated mechanism's failure is not correctness.

## Everyday loop (even with nothing cloned)

```sh
bh sync              # pull every hive's beads into the hub (data, not code)
bh hub bd ready      # actionable work across the whole workspace
bh hub intake        # untriaged intake inbox across all hives
```

To work on a hive for real, clone it (via git-workspace) and `bh sync` again — that hive
switches from the cache to its live checkout automatically.

## Why this shape

- **No central server.** The hub is beads multi-repo hydration over local DBs — a read cache;
  authoritative data stays in each hive. The [Dolt server](DOLT.md) is unrelated and optional.
- **Remote-only browsing.** The minimal-clone cache fetches a hive's issues without its code,
  which is what makes a no-clone workflow possible.
- **Distribution is git-native.** Hives publish via `bd dolt push` to `refs/dolt/data` on their
  own remotes; refresh with `bh -a bd dolt pull` (cloned) — `bh sync` re-bootstraps caches.

See [DESIGN](DESIGN.md#the-hub-a-cross-hive-view-without-a-server) for rationale,
[INTEGRATIONS.md](INTEGRATIONS.md#lifecycle-roadmap-design-intent-not-yet-built) for the
planned remote-only → clone-down → release lifecycle, and [HQ](HQ.md) for the durable,
shareable store the hub hands its aggregation role off to once one is registered.
