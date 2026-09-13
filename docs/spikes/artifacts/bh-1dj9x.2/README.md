# Replayable Pants prototype

This non-active patch is pinned to baseline `3fd757827c29a48e6cab581bad0d9bd4b1b215e1`.
Apply it only in a disposable worktree, set `PANTS_WORKDIR` to a worktree-specific directory,
and pass host-cache paths through `PANTS_LOCAL_STORE_DIR` and `PANTS_NAMED_CACHES_DIR`.

```sh
git apply --check docs/spikes/artifacts/bh-1dj9x.2/pants-prototype.patch
git apply docs/spikes/artifacts/bh-1dj9x.2/pants-prototype.patch
PANTS_WORKDIR=/tmp/pants-work-a pants generate-lockfiles --resolve=beadhive
PANTS_WORKDIR=/tmp/pants-work-a pants test tests/unit/modules/config/test_resolution.py
```

The patch intentionally disables remote reads/writes and does not contain the generated secondary
lock. It captures the verified Pants 2.32.1/uv/coherent-pytest graph and the reversible fixture
split seam. It is not activation-ready: stateful consumers must first be inventoried and attached
narrowly, third-party module mappings completed, and installed-console-script/full-only routes
kept on native validation.
