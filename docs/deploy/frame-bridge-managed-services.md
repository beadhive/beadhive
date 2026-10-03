# Managed host and Frame Bridge services

The system units in `deploy/systemd` share
`BH_HOME=/var/lib/beadhive/host`. Config resolution reads `config.yaml` there;
local fallback identity reads `host.yaml` there. The daemon's runtime state is
also stored under that home. Provision enrollment/config and local identity
before starting either service. These files must be readable by the selected
service users; keep private daemon verifier material readable only by `bees`.
The Factory bridge obtains its separate bearer through `LoadCredential`.

Install the application and its dependencies at the stable system prefix
`/opt/beadhive-frame-bridge`. The prefix must provide `bin/python`,
`bin/beadhive-frame-bridge`, and `bin/bh-host-daemon`; it cannot resolve into a
protected user home. Create the `bees` and `beadhive-frame-bridge` users and the
`beadhive-gateway` group before starting the units. The host unit owns its state
directory as `bees:beadhive-gateway`, mode 0750. Install the Development example
as `beadhive-frame-bridge-dev.service`, removing the `.example` suffix.

Example host config (enrollment supplies the identity fields):

```yaml
host:
  daemon:
    enabled: true
    bind: 127.0.0.1
    port: 8737
    auth:
      credential_file: /var/lib/beadhive/host/daemon-verifiers.json
  frame_bridge:
    host_id: frame-one
    instance_id: frames/one
    factory_id: factory-one
    primary_hive_id: github/acme/first-hive
```

The verifier file must meet the daemon's existing credential schema and mode
0600 requirements. The bridge's root-owned Gateway verifier document and its
bearer credential remain required at the paths declared in the bridge unit.
Provision the Development service's Clerk and subject credentials separately.
No service starts with authentication disabled.

When omitted, `host_id` comes from `host.yaml`; `factory_id` defaults to that
host ID and `instance_id` to `frames/<host_id>`. IDs must satisfy the private
upstream contract. `primary_hive_id` must be explicit: a bridge must never
silently register a different operator's hive. Registration digest includes all
identity fields and changes whenever one changes. The host epoch persists
across service restarts independently of enrollment identity.

Both profiles connect to the typed `host.daemon.bind`/`port` settings, defaulting
to `http://127.0.0.1:8737`. Literal loopback addresses are required for bearer
custody. Direct TLS selects HTTPS and uses the normal client certificate trust.
Configuration paths and identities fail closed if malformed.

## Related bead reconciliation for review

`bh-qa7vc` fixes private Factory identity, daemon port configuration, and the
missing host unit. Keep `bh-76pls` (UV-managed launchers under ProtectHome) open:
this change specifies a supported stable system prefix, but does not install or
upgrade that prefix from the user's UV environment or verify a live systemd
startup. Its remaining packaging, upgrade, health, and credential provisioning
acceptance still needs that bead. The operator approved reconciliation on
2026-09-30: `bh-76pls` now references and depends one-way on `bh-qa7vc` for unit
ownership and port wiring, retaining installation and live startup proof.

Keep `bh-ym56t` (transport/bootstrap/Herdr consolidation) and its daemon/bridge
contract and adapter leaves open. This fix changes configured production
identity without moving transport ownership or replacing its adapter boundary.
The same approved reconciliation records a one-way dependency on `bh-qa7vc` for
enrollment identity and port behavior when freezing private bridge contracts.
The remaining consolidation scope and all broader acceptance are preserved;
neither related bead is closed by this change.

## Verification scope

`systemd-analyze verify` can validate the three units together after the example
is renamed and the declared executable paths exist. A source checkout without
an installed `/opt` prefix requires an isolated verification root and executable
placeholders. That verifies unit syntax and dependency resolution, not service
startup, executable contents, credential provisioning, or deployment health.
