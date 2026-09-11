# Conformance test kits

`beadhive.testing` is the supported, runner-neutral surface for testing implementations at a
Beadhive extension boundary. It is standard-library-only and performs no configuration read,
plugin discovery, telemetry initialization, network access, or process startup when imported.
Plugin and capability-module tests can consume it without importing repository-root runtime
fixtures.

## What conformance proves

A conformance suite proves that every supplied implementation satisfies the same named semantic
cases for one declared boundary:

- `PortCase` covers the substitutability rules of an outbound port;
- `LifecycleCase` covers ordering, failure policy, retry, idempotency, or compensation rules for
  lifecycle participants;
- `assert_plugin_conforms` validates a manifest through its owning validator, requires declared
  capability IDs to match live providers exactly, and runs additional `PluginCase` semantics; and
- `assert_schema_artifact_conforms` checks repeatable canonical JSON generation, stable identity,
  explicit version, and byte equality with the checked-in artifact.

Each case has a stable lowercase semantic ID. The runner wraps a failed assertion or exception in
`ConformanceFailure`, whose message includes that ID, the suite, the subject, and the original
diagnostic. A structural `Protocol` check can supplement port cases, but cannot replace them.
Port and lifecycle suites reject an empty case list, so structural compatibility cannot become a
vacuous semantic pass.

The in-memory example in `tests/unit/testing/test_conformance_testkit.py` applies an event-sink
contract to a recording adapter. The real-adapter example in
`tests/unit/testing/test_real_adapter_conformance_example.py` applies a state-stream semantic case
to `PollingStateStreamProvider`. The latter requests only its explicit config and plugin test
scopes, reflecting that adapter's current imports; neither example depends on the legacy aggregate
fixture.

## Writing a reusable contract

Keep contract cases with the public port or kernel contract, and give each case a fresh subject:

```python
from beadhive.testing import PortCase, assert_port_conforms


def retains_order(sink):
    sink.publish("prepared")
    sink.publish("committed")
    assert sink.events == ["prepared", "committed"]


def assert_event_sink_conforms(factory):
    assert_port_conforms(
        factory,
        [PortCase("event-sink.retains-publish-order", retains_order)],
    )
```

An adapter test imports `assert_event_sink_conforms` and passes its own factory. Module-owned test
fixtures stay beside that adapter or module; the shared kit neither provides nor discovers them.

Plugin conformance takes a factory because every common and plugin-specific case must receive a
new `PluginSubject`, manifest mapping, capability mapping, and capability provider objects:

```python
from beadhive.testing import PluginSubject, assert_plugin_conforms


def plugin_subject():
    return PluginSubject(
        manifest=build_manifest(),
        capabilities={"launch": LaunchCapability()},
    )


assert_plugin_conforms(
    plugin_subject,
    manifest_validator=validate_plugin_manifest,
    subject="example-plugin",
)
```

The runner rejects a factory that reuses those mutable objects, preventing one semantic case from
contaminating the next.

Canonical schema serialization is strict RFC JSON: non-finite numeric constants such as `NaN`
and `Infinity` are rejected. Artifact IDs must be non-empty strings, artifact versions must be
exact integers or strings, and the generated document must carry the same identity and version
with the same types. In particular, JSON `true` never satisfies integer version `1`.

## What remains integration or system coverage

Conformance does not prove that bootstrap selected the implementation, configuration and secrets
resolve in a deployed environment, a real external service honors its protocol, multiple modules
compose correctly, transport surfaces preserve their public behavior, or startup/shutdown works
for a complete process. Those remain adapter integration, boundary-crossing integration, and
installed system tests. A conformance-green plugin can still fail those layers, so the kit does
not replace `just check`, `just check-all`, compatibility-facade tests, or registered system
smokes.
