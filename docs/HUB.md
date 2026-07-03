# Hub — the cross-rig view

The **hub** is one aggregated beads DB holding a unified view of every registered rig, so you
can ask "what's ready anywhere?" — and so `ws` works on a machine with **no repos cloned**
(module: `hub.py`).

## Where it lives

`~/.ws/hub/` (override `WS_HUB`), with minimal-clone caches at `~/.ws/cache/` (override
`WS_CACHE`). It's a dedicated beads DB the CLI owns — not tied to any code repo —
initialized on first `ws sync` (`hub.ensure_hub`).

## `ws sync`

Builds/refreshes the hub from `managed_repos`. For each rig:

- **cloned** (its `.beads/` exists under `$GIT_WORKSPACE`) → added by **local path**.
- **uncloned** → fetched into a **minimal-clone cache** and added by that path:
  `git clone --filter=blob:none --no-checkout <url>` (no working tree, blobless) +
  `bd bootstrap` (pulls `refs/dolt/data`) → just the beads data (~tens of MB/rig).
- then `bd repo sync` hydrates the unified view.

URLs for uncloned rigs come from the git-workspace lock (exact; `gitworkspace.repo_urls`) or
are derived for github/gitlab (`git@<host>:<org>/<repo>.git`); a rig with neither is skipped
with a warning. Output summarizes `N cloned, M remote-cached, K skipped`.

## `ws hq`

Query the HQ aggregate (the operator-facing surface; `ws hub` is a deprecated alias):

```sh
ws hq bd ready         # actionable work across all rigs
ws hq bd list
ws hq intake           # superintendent's fleet-wide untriaged-intake inbox
```

It errors with "run `ws sync` first" if the aggregate store isn't initialized.

## Everyday loop (even with nothing cloned)

```sh
ws sync              # pull every rig's beads into the HQ store (data, not code)
ws hq bd ready       # actionable work across the whole workspace
ws hq intake         # untriaged intake inbox across all rigs
```

To work on a rig for real, clone it (via git-workspace) and `ws sync` again — that rig
switches from the cache to its live checkout automatically.

## Why this shape

- **No central server.** The hub is beads multi-repo hydration over local DBs — a read cache;
  authoritative data stays in each rig. The [Dolt server](DOLT.md) is unrelated and optional.
- **Remote-only browsing.** The minimal-clone cache fetches a rig's issues without its code,
  which is what makes a no-clone workflow possible.
- **Distribution is git-native.** Rigs publish via `bd dolt push` to `refs/dolt/data` on their
  own remotes; refresh with `ws -a bd dolt pull` (cloned) — `ws sync` re-bootstraps caches.

See [DESIGN](DESIGN.md#the-hub-a-cross-rig-view-without-a-server) for rationale and
[INTEGRATIONS.md](INTEGRATIONS.md#lifecycle-roadmap-design-intent-not-yet-built) for the
planned remote-only → clone-down → release lifecycle.
