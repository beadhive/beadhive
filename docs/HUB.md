# Hub — the cross-hive view

The **hub** is one aggregated beads DB holding a unified view of every registered hive, so you
can ask "what's ready anywhere?" — and so `bh` works on a machine with **no repos cloned**
(module: `hub.py`).

## Where it lives

`~/.ws/hub/` (override `WS_HUB`), with minimal-clone caches at `~/.ws/cache/` (override
`WS_CACHE`). It's a dedicated beads DB the CLI owns — not tied to any code repo —
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

See [DESIGN](DESIGN.md#the-hub-a-cross-hive-view-without-a-server) for rationale and
[INTEGRATIONS.md](INTEGRATIONS.md#lifecycle-roadmap-design-intent-not-yet-built) for the
planned remote-only → clone-down → release lifecycle.
