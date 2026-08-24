# Operator SSE → UI decoder conformance proof

On 2026-08-24, an `entity-upsert` SSE frame was emitted by
`beadhive.operator_sse.OperatorEventRelay` through the production snapshot projection. The
captured frame is checked in at
`tests/fixtures/operator-event-ui-conformance.sse` with SHA-256
`0dd82547b2539bdf54deba70e1b45c4516bb848db8eff0dd5354f8fa22d813cc`.

The frame was then decoded using the actual sibling UI source—not a copied decoder—from
`/home/bees/workspace/github/beadhive/beadhive-ui/packages/operator-contract/src/codec.ts`
at sibling commit `656b3c3d04948f8cea5dcc757a9580d0d11cc2ec`. That decoder file has SHA-256
`888680f1f5515bc531fa841581f3a18e95e9043975c243405ae2335957a64585`, byte-identical to
the decoder at merged UI epic commit `cd1c71e54c3655442a2067cf6ffe1818775225a5`.
The checked-in harness parsed the SSE envelope, called `decodeOperatorEvent(JSON.parse(data),
{ expectedHiveId })`, and separately checked that the SSE `id` equaled
`producerEpoch:sequence` and that `baseSequence === sequence - 1`.

The core SSE test runs the checked-in deterministic generator through the production
source/projection/feed/relay path and requires its bytes to equal the pinned fixture. Reproduce
that evidence from a Beadhive checkout with:

```text
UV_CACHE_DIR=/tmp/uv-cache uv run python \
  tests/proof/generate_operator_sse_fixture.py /tmp/bh-operator-event.sse
cmp tests/fixtures/operator-event-ui-conformance.sse /tmp/bh-operator-event.sse
sha256sum /tmp/bh-operator-event.sse
```

The cross-repository proof remains manual because the core runtime intentionally has no UI
toolchain dependency. Point the checked-in harness at the actual sibling decoder from a UI
checkout with its dependencies installed:

```text
BEADHIVE_REPO=/path/to/beadhive
cd /home/bees/workspace/github/beadhive/beadhive-ui
node --import tsx \
  "$BEADHIVE_REPO/tests/proof/decode_operator_sse_fixture.mts" \
  packages/operator-contract/src/codec.ts \
  "$BEADHIVE_REPO/tests/fixtures/operator-event-ui-conformance.sse"
```

Observed result:

```json
{"ok":true,"event":"operator-event","idMatches":true,"payloadKind":"entity-upsert","hiveId":"github/beadhive/beadhive","decoderSha256":"888680f1f5515bc531fa841581f3a18e95e9043975c243405ae2335957a64585"}
```

This is test/proof evidence only. The Beadhive runtime has no dependency on the sibling UI
repository or its toolchain.
