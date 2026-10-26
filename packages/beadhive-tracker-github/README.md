# beadhive-tracker-github

The GitHub tracker adapter **plugin** (`tracker-github`, capability `tracker.adapter` v1).
Design: [tracker-adapter-plugin-adr.md](../../docs/design/tracker-adapter-plugin-adr.md) and
[tracker-adapter-port-adr.md](../../docs/design/tracker-adapter-port-adr.md).

> **Skeleton.** This distribution currently carries the package, its `plugin.json` manifest and a
> factory that refuses to build an adapter. The read path (githubkit mapping, retry layer,
> identity probe) is a later bead; no GitHub request can be made from it.

## Shape

| Path | Holds |
|---|---|
| `src/beadhive_tracker_github/plugin.json` | The manifest. Byte-identical to `src/beadhive/kernel/plugins/manifests/tracker-github.json` (drift-tested) |
| `src/beadhive_tracker_github/factory.py` | The one call root's lazy row makes: `build(settings, token_source, *, project, sleep)` |
| `src/beadhive_tracker_github/adapter/` | The wiring-free port implementation. The **only** place `githubkit` may be imported, and only inside functions |

## Install and enablement

Install with the `beadhive[github]` extra. **Installation never implies enablement and enablement
never installs.** The plugin is off unless `plugins.tracker-github.enabled` is `true` on the host;
a store binding (`managed_repos[].stores.public.adapter`) never enables it; a hive with no binding
is unaffected. githubkit is declared here, not in the root distribution, so `import beadhive` never
needs it and never imports it.

## Rules enforced by this package's tests

* **P1** githubkit is imported only lazily and only under `adapter/`.
* **G4** the package imports no `beadhive.kernel.*` and, from `beadhive_plugins`, only
  `beadhive_plugins.credentials`.

## Licence

githubkit is MIT. The transitive `hishel` declares only the generic BSD trove classifier, so the
root `osv-scanner.toml` carries a time-boxed override for it (see the entry for the date it was
re-verified and when it lapses).
