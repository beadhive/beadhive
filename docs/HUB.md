# Hub — the cross-hive view

The **hub** is one aggregated beads DB holding a unified view of every registered hive, so you
can ask "what's ready anywhere?" — and so `bh` works on a machine with **no repos cloned**
(module: `hub.py`).

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

## `bh hq`

Query the HQ aggregate (the operator-facing surface; `bh hub` is a deprecated alias):

```sh
bh hq bd ready         # actionable work across all hives
bh hq bd list
bh hq intake           # director's fleet-wide untriaged-intake inbox
```

It errors with "run `bh sync` first" if the aggregate store isn't initialized.

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

Once a [Factory HQ](HQ.md) is registered, `bh sync` targets `~/.beadhive/hq` instead of the
hub for this same aggregation role — and HQ, unlike the hub, *can* be durable and shared
across hosts (its `fleet.yaml`/`workspace.toml` and `hq`-prefixed beads are genuinely
authoritative once pushed). See [HQ — Hub vs HQ](HQ.md#hub-vs-hq) for the full distinction.

## Everyday loop (even with nothing cloned)

```sh
bh sync              # pull every hive's beads into the HQ store (data, not code)
bh hq bd ready       # actionable work across the whole workspace
bh hq intake         # untriaged intake inbox across all hives
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
