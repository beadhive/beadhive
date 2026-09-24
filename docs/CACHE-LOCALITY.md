# Framework cache locality

Beadhive resolves package manager caches against the dependency target's actual filesystem
device. Managed worktrees on `/tmp` or another `tmpfs`/`ramfs` mount use
`/tmp/bh-cache/<application>` by default. `BH_TMPFS_CACHE_DIR` may select another absolute,
host-local root. Custom worktree roots are classified from the configured root and its actual
mount type; the string `tmpfs` in a path has no meaning.

Each application gets a normalized child directory. The shipped adapters are:

| Application | Cache control | Same-device import | Copy fallback |
|---|---|---|---|
| uv | `UV_CACHE_DIR` | `UV_LINK_MODE=hardlink` | `UV_LINK_MODE=copy` |
| pnpm | `PNPM_CONFIG_STORE_DIR` | `PNPM_CONFIG_PACKAGE_IMPORT_METHOD=hardlink` | `PNPM_CONFIG_PACKAGE_IMPORT_METHOD=copy` |

The adapter contract is open: another application supplies its name, dependency directory,
cache variable, and supported hardlink/copy controls without a Beadhive config schema change.

## Selection and diagnostics

An explicit framework cache variable remains authoritative. If it is on another device,
Beadhive preserves its path and exports explicit copy mode. Ephemeral worktrees try the tmpfs
tier. Main checkouts and persistent worktrees try `BH_DURABLE_CACHE_DIR/<application>`, then
`$XDG_CACHE_HOME/beadhive/frameworks/<application>`. A managed tier is admitted only after its
owner, mode, path type, target device, available bytes, and available inodes pass. Defaults keep
512 MiB and 10,000 inodes free; `BH_CACHE_MIN_FREE_BYTES` and `BH_CACHE_MIN_FREE_INODES` are
host overrides.

Every provisioning run reports the tier, cache and target devices, free bytes/inodes, and link
method. Unsafe ownership, a symlink, low capacity, separate mounts, or no supported adapter
produces a visible durable or native-copy fallback. Beadhive never turns those cases into uv's
repeated implicit hardlink warning.

Command detection is conservative: an unrecognized command receives no invented environment
variables and runs with its framework-native durable/copy behavior. API callers that know the
unrecognized command is a caching framework use `unsupported_cache_fallback` to obtain the same
device/capacity report and a visible `unsupported-copy` decision. `adapter_for_application`
raises a clear unsupported-application error, while `adapter_for_command` returns `None` for an
ordinary non-cache command; this keeps unrelated validation commands on their existing fast path.

Worktree creation and verify-checkout init pass the selected environment to `uv sync` and pnpm
rules. `scripts/pants_cache.py` applies it to Pants subprocesses. `scripts/hermetic.sh` resolves
the host path before its private `/tmp` mount, then binds that exact cache writable after the
mount and exports uv's controls inside the fence. The fence also binds uv's managed Python
toolchain read only so the environment created before entry keeps its selected interpreter.

## Trust and lifecycle

Shared caches are for mutually trusted runs by one host user. Roots and application directories
are owned by that uid with mode 0700. Initialization uses a bounded advisory lock; interruption
or owner death releases the kernel lock. Beadhive does not recursively inspect, copy, repair, or
prune uv, pnpm, or third-party cache contents. Cleanup must run only while users are idle and use
the framework's supported cache command. The default tmpfs tier is volatile and disappears on
reboot or when the host discards that mount. Persistent cache retention belongs to the host
operator.

There is deliberately no Beadhive prune implementation: `prune_cache` refuses the operation.
That makes concurrent use and pruning non-racing by construction; package-manager maintenance
must be scheduled while all users are idle. Concurrent directory initialization is idempotent
under the bounded kernel lock, which is released automatically on interruption and owner death.

## Reproducible benchmark

Run at least three repetitions for each checkout class and application. Each repetition gets a
fresh resolver-owned root, followed immediately by a warm run against the same root. The JSON
records repeated medians, wall time, bytes/inodes consumed, cache-hit text, devices, link method,
and warnings. Each repetition runs in a fresh detached worktree at `HEAD`, so its dependency
target starts cold while the source checkout and any existing `.venv`/`node_modules` remain
untouched; the warm run reuses that same target. It retains benchmark cache roots so Beadhive
never deletes opaque cache internals.

Capacity is sampled before and after each phase for both cache and dependency target filesystems.
When they share a device the report labels the target `same_device_as: cache` and records the one
filesystem observation only under `cache` (the target deltas are null); cross-device runs retain
two distinct pressure measurements. Before installation creates the dependency directory, its
filesystem capacity is sampled from the nearest existing parent on the same device; the JSON
records the path used for each sample.

```console
uv run python scripts/benchmark_cache_locality.py \
  --application uv --checkout /tmp/bh-worktrees/github/acme/api/verify-demo \
  --checkout-class ephemeral --cache-root /tmp/bh-cache-benchmark/uv \
  --repetitions 3 -- uv sync

uv run python scripts/benchmark_cache_locality.py \
  --application pnpm --checkout /tmp/bh-worktrees/github/acme/web/verify-demo \
  --checkout-class ephemeral --cache-root /tmp/bh-cache-benchmark/pnpm \
  --repetitions 3 -- pnpm install --frozen-lockfile
```

Repeat with the main checkout, `--checkout-class persistent`, and a same-device durable
`--cache-root`. Compare median cold and warm times; keep an optimization disabled where the
repeated median improvement is noise.

To measure an explicit cross-device override, keep the checkout on its actual filesystem and
pass `--native-cache-root` on another device. The benchmark gives every repetition a fresh
application child under that root, then records the preserved cache path and explicit copy
mode. This exercises the same safe fallback used when a user-owned native cache is on another
mount; it does not inspect or clean the cache contents.

The xdist worker-matrix harness also records cache devices, but its direct pytest runs include
offline wheel-build tests. Before timing, it copies the configured uv cache into a writable
directory under the external benchmark scratch root and points the child test process at that
copy. This allows offline builds to use the existing backend cache when the host cache is
read-only. The report records both source and staged paths and devices; this staging is a
benchmark control, not a change to the host's configured cache. The cache-locality harness above
separately measures the resolver's same-device hardlink and cross-device copy behavior.

The current host tuning remains two globally admitted validations and sixteen xdist workers
inside each gate. Cache locality changes storage placement only; it does not increase either
concurrency setting.
