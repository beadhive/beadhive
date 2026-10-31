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

- **Archive** — `write_archive(dest, events, groups=..., generator=...)` writes a reproducible
  `.bdlg` (same events, same bytes: `mimetype` first and STORED, sorted entries, fixed mtime and
  mode, size-partitioned parts, manifest digests computed while streaming, ZIP64 only when a
  member needs it). `ArchiveReader` is the streaming, untrusted-input reader: it verifies
  `mimetype`, the manifest and every part's size, SHA-256 and record count (before any event of
  the part is yielded), refuses unknown `capabilities.required`, zip-slip names, oversized
  entries and entry floods (`ReadLimits`), and supports ZIP64. Each refusal has its own error
  type under `ArchiveError`. `read_archive(source)` loads a small archive in one call.
- **Conformance kit** — `beadlog.testing`: `conformance_cases()` (export, load, re-export
  equivalence; idempotent reload; refusal of unknown required capabilities and of tampered
  archives; loss-report completeness), the `InMemoryStore` reference store and the
  `load_archive` driver. A store-loader package runs the cases against its own store:

  ```python
  @pytest.mark.parametrize("case", beadlog.conformance_cases(), ids=lambda c: c.name)
  def test_conformance(case):
      case.run(make_empty_store)
  ```

The two published schemas an archive embeds are byte copies under
`src/beadlog/archive/schemas/`; a test pins them to `docs/schemas/beadlog/v0.1/`.

## Tests

`uv run --package beadlog pytest packages/beadlog/tests` from the repository root, or
`just pkg beadlog check`. The tests read the published id vectors and the worked example archive
under `docs/schemas/beadlog/v0.1/examples/`; JSON Schema validation of those files stays in the
root suite (`tests/test_beadlog_schemas.py`), so `jsonschema` is never a dependency here.
