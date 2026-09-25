# `bh-k9wrw` Gateway work-item read handoff

This handoff adds `beadhive.work-items/v1` to the compact schema-version-1 Development snapshot.
It is additive to the immutable e482 handoff; the historical fixture and proof are unchanged.

The machine-readable sibling-consumer fixture is
[`frame-bridge-work-items-v1.json`](../../tests/fixtures/frame_bridge_gateway_work_items_v1/frame-bridge-work-items-v1.json).
The generated Gateway contract is
[`beadhive-gateway-projection-v1.json`](../../src/beadhive/schemas/beadhive-gateway-projection-v1.json).
The consumer baseline recorded for integration is Beadhive Gateway commit
`cf78b73fbcb231d0cce4034e242cc4b9b7ad3e64`; it is a baseline identity, not a claim that the
sibling has already shipped these routes.

Gateway must sign the canonical private target, preserve repeated normalized priority/label
parameters, map public `view` 1:1 to daemon `queue`, and relay 409 and 413 without widening them.
It must never concatenate hive pages. Public and private responses are exact schemas, and Core
validates the daemon response before rebuilding the public envelope.

The UI starts from the bounded compact seed, follows the capability revision for pages and exact
detail, and uses the existing single hive-wide invalidation stream. A revision invalidation
refetches only the active pane; filters do not create subscription or replay state.

Release acceptance requires the reviewed Core commit, generated contract digest, private signed
route conformance, public authenticated canonical/compatibility route conformance, the 1,344-item
Factory-scale seed test, and an independently reviewed sibling Gateway change before deployment.
