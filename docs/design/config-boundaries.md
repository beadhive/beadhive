# Config dependency map

Status: implemented by `bh-1jhk4.4`–`.6`

`beadhive.config` remains the only compatibility surface used by callers. It owns stable names,
shared constants, and forwarding functions; behavior belongs to the modules below.

```text
callers
  ↓
config.py (composition + compatibility facade)
  ├─→ config_paths.py          environment, machine paths, packaged assets
  ├─→ config_store.py          YAML I/O, host/fleet merge, partition guards, atomic writes
  ├─→ config_edit.py           dotted get/set/unset, coercion, write-time validation
  ├─→ config_policy.py         persisted migrations and operator warnings
  ├─→ config_services.py       runtime/host/telemetry/service typed accessors
  ├─→ config_work_settings.py  work/routing/validation/dispatch/identity policy
  └─→ config_release.py        release-order and Claude plugin policy

config_edit.py ─→ facade storage/schema seams
typed policy modules ─→ facade load/layered/path seams
config_store.py ─→ config_partition.py
config_edit.py ─→ config_schema.py
```

The implementation modules never import one another through the facade at module-import time.
Typed policy modules resolve the facade dynamically after its core seams exist. This keeps the
dependency direction acyclic while preserving old-module monkeypatch points such as
`config.load`, `config_path`, `load_host`, `save`, and `gh_login`.

## Placement rules

- Add environment variables, filesystem locations, and bundled-resource resolution to
  `config_paths.py`.
- Add parsing, serialization, layer reconciliation, partition enforcement, or write durability
  to `config_store.py`. Host/fleet writes use same-directory atomic replacement; dotted
  read-modify-write transactions also hold the per-file process/thread lock.
- Add dotted mutation payloads, CLI literal coercion, or write-time schema dispatch to
  `config_edit.py`. Schema facts remain in `config_schema.py`; partition facts remain in
  `config_partition.py`.
- Add one-time persisted transformations or invocation-time warnings to `config_policy.py`.
- Add typed getters to the cohesive domain module that consumes them: services/runtime, work
  policy, or release/plugin policy. Do not create one-function accessor modules.
- Export any caller-visible addition through `config.py`. New implementation code must call
  facade collaborators where an existing compatibility seam is patchable; a bare import-time
  alias would bypass those tests and is not compatible.

`KNOWN_SECTIONS` remains on the facade pending its schema-derived ownership work (`bh-1h9h`);
this extraction deliberately does not duplicate or redesign schema/alias policy.

## Validation and coverage evidence

The final hermetic `just check` passed with 5,751 tests and 9 skips. The periodic coverage run
measured the facade plus seven extracted config modules at 1,109/1,145 statements (96.8559%),
up from the frozen `config.py` baseline of 950/984 (96.5447%). Repository-wide statement
coverage was 24,013/26,771 (89.6978%), also slightly above the 89.6467% baseline.

The unfenced `just cov` command ended non-zero after collecting those measurements because
`test_the_demo_refuses_to_touch_real_state` observed four same-byte HQ manifest rewrites from
other live bh processes. Its diagnostic identified this as the known ambient-writer race and
recommended the hermetic fence. The identical test passed in the immediately preceding fenced
`just check`; there was no config failure or missing coverage data.
