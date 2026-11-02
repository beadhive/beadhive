# beadhive-beadlog-xtdb

The opt-in XTDB BeadLog store **plugin** for Beadhive: capability `beadlog.store` API 1
([format ADR amendment A6](../../docs/design/beadlog-format-adr.md#a6-two-package-xtdb-loader-and-the-beadlogstore-capability)).
It binds the [`beadlog-xtdb`](../beadlog-xtdb/README.md) loader library into
`bh backfill apply <plan.bdlg> --store xtdb` and `bh backfill export --store xtdb`, and provides
`bh plugin beadlog-xtdb status` and `doctor`.

- **Off by default, twice.** Install it with the `beadhive[xtdb]` extra (`uv sync --extra xtdb`
  in a checkout); installing enables nothing until `plugins.beadlog-xtdb.enabled` is `true` on
  the host. Root imports this package lazily, after manifest selection, and this package imports
  the library (and psycopg, LGPL-3.0) only inside functions, so `import beadhive` and every `bh`
  command work without it.
- **bh never runs the node.** The operator starts, stops, backs up and upgrades the XTDB 2.2.x
  container (loopback only, `XTDB_PASSWORD`, `--memory 2g`, a real-disk data directory, a
  GET-based health check and a pinned image digest: see the
  [operator runbook](../../docs/XTDB-STORE.md) and
  [`bh-cntr0`](../../docs/spikes/bh-cntr0-xtdb-operational-fit.md)).
- **No secret in configuration.** The password comes from the host's fnox secret broker through
  `plugins.beadlog-xtdb.credential` (`config_path`, `profile`, `key`); a DSN that embeds one is
  refused, and no status, doctor row or error repeats it.

## Configuration (`plugins.beadlog-xtdb`, host-owned)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | The plugin's own gate |
| `dsn` | `host=127.0.0.1 port=5432 user=xtdb dbname=xtdb` | libpq string or `postgresql://` URI of the loopback pgwire port |
| `credential` | unset | fnox lookup of the node's `XTDB_PASSWORD` |
| `image` | the validated XTDB 2.2.0 digest | digest-pinned image the operator runs (doctor warns on another digest) |
| `data_dir` | unset | host directory mounted at `/var/lib/xtdb`, for doctor's file-system check |
| `health_url` | `http://127.0.0.1:8080/healthz/alive` | loopback healthz URL, probed with `GET` |

## Doctor

`bh plugin beadlog-xtdb doctor` is read-only and loopback-only. Each failure mode is a row with
its remedy: a `tmpfs`/`ramfs` data directory (refused), a wrong XTDB version (`>=2.2.0,<2.3`),
an unhealthy node (`GET` health probe: the stock image `HEALTHCHECK` sends `HEAD`, which XTDB
answers `405`), no password, a node that accepts a password-less login, and a non-loopback bind.
While the plugin is disabled it checks only the configuration and the data directory and exits
0; enabled, it exits 1 on any error.

## Apply and export

`--store xtdb` runs the same backfill safety envelope as the bd store: the corpus-hash check
against the store, a whole-plan dry run, a `.bdlg` of the store's pre-apply history as the
recovery artifact (`<plan>.pre-state.bdlg` by default), per-bead writes with audit records,
rollback on failure and the post-apply verification. On a fresh database the first written unit
takes the loader's monotone initial load and later units load incrementally; each unit's loss
report says which. `export --store xtdb` reads the store's current state for the hive's bead
group through the as-of reader (`<hive>.xtdb.bdlg` by default); `--history` reads the bd events
table and is refused with `--store xtdb`.
