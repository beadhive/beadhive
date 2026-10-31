# beadlog

The BeadLog (`.bdlg`) format package: content-addressed bead history events and the operations
every producer and consumer must agree on. The bytes are specified by the
[format ADR](../../docs/design/beadlog-format-adr.md) and the JSON Schemas under
[`docs/schemas/beadlog/v0.1/`](../../docs/schemas/beadlog/v0.1/); the packaging decision is the
[backfill platform ADR](../../docs/design/backfill-platform-adr.md) (section 2).

A **library** package
([package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md)): standard
library only, no runtime dependencies, never imports `beadhive`. Import it only through
`beadlog.__all__`.

## Surface

- **Events** — `BeadEvent` (frozen) with `BeadRef`, `Source`, `Evidence`, `Edge`, and the
  `EventKind` / `Provenance` / `Precision` vocabularies; `from_json` / `to_json` for one JSONL
  line.
- **Ids** — `canonical_json` (RFC 8785 over the admissible domain: no non-integer numbers),
  `identity_projection`, `event_id`, `mint_event`. `event_id` reproduces
  `docs/schemas/beadlog/v0.1/examples/id-vectors.json` byte for byte.
- **Order** — `ordering_key` (`occurred_at`, `recorded_at`, id, with null as +infinity and
  timestamps compared as instants) and archive `part_order`.
- **Merge** — `union_merge(logs)`: union by id, id collisions refused, `extracted_at` minimum,
  `confidence` maximum, evidence set union, annotations smallest-JCS per key, redaction joined,
  tombstones absorbing. Commutative, associative and idempotent.
- **Fold** — `fold(events, at=..., recorded_as_of=...)` to per-bead `BeadState`:
  last-writer-wins per facet, label and edge sets, comments, existence, retirement, erasure,
  checkpoint seeding.
- **Diff** — `diff(proposed, baseline)`: the proposed events whose ids the baseline lacks, plus
  per-bead deltas (baseline, proposed and merged folds and the changed facets). Reconcile
  classifies on top of it.
- **Manifest** — `Manifest`, `Capabilities` (`require()` refuses unknown required
  capabilities), `Capability`, `LoaderCapability`, `FORMAT_VERSION`, `MEDIA_TYPE`.

Archive I/O (the ZIP container) and the loader-conformance kit (`beadlog.testing`) are later
work.

## Tests

`uv run --package beadlog pytest packages/beadlog/tests` from the repository root, or
`just pkg beadlog check`. The tests read the published id vectors and the worked example archive
under `docs/schemas/beadlog/v0.1/examples/`; JSON Schema validation of those files stays in the
root suite (`tests/test_beadlog_schemas.py`), so `jsonschema` is never a dependency here.
