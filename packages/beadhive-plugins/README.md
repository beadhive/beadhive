# beadhive-plugins

The stdlib-only capability-slot, provider-binding, and lifecycle event contracts described in
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md) (section 4).
This package depends on nothing — not even another library package — so root and other
library/plugin packages may import its public `__all__` surface statically, unconditionally.

Importing it describes metadata and typed ports; it never discovers plugins, reads
configuration, inspects the host, or imports an integration. Discovery, the lifecycle
dispatcher, and telemetry stay in `src/beadhive` — they are composition machinery that binds
concrete providers to the contracts here, not part of the contracts themselves.

## Modules

- `beadhive_plugins.contracts` — the plugin manifest schema, capability-slot identity
  (`CapabilityRef`, `CapabilityKey`), discovery diagnostics, and the `build.impact` /
  `build.verify` capability declarations.
- `beadhive_plugins.binding` — `bind_application_port`, the one function a bootstrap
  composition root calls to resolve a selected provider into a typed port for explicit
  consumer injection.
- `beadhive_plugins.lifecycle` — the typed, framework-independent lifecycle event catalog
  (`ALL_LIFECYCLE_EVENTS`, `LifecycleEvent`, `SubscriberBinding`, `DeliveryPolicy`) shared by
  every lifecycle family, including the `WORKTREE` events.

## Root compatibility facades

Root keeps forwarding facades at the old `beadhive.kernel.plugins.contracts`,
`beadhive.kernel.plugins.binding`, and `beadhive.kernel.lifecycle.contracts` import paths so
existing plugins and consumers (including `beadhive-pants`) keep working unchanged — every name
previously importable from those three modules still resolves to the identical object defined
here (`docs/MODULES.md` principle 8: compatibility facades are deliberate migration tools).
Migrating those 11 external consumers onto this package directly is a separate, later bead.
