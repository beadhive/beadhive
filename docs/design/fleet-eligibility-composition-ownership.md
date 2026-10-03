# Exact Fleet composition ownership review

Reviewed exact ownership for bh-38h7p, actor dev/fleet-eligibility. Independent root scanner
reproduced each exact edge. The operator explicitly approved these 26 ownership records and
matching snapshot on 2026-10-02; full lifecycle review remains gated on validation.

Remaining exact edges: 26. Counts: {'introduced edge or changed exact symbols': 10, 'preexisting
edge promoted into SCC': 16}.

The architecture ADR docs/design/modular-dependency-and-test-closure-adr.md:132 explicitly
allows a named steward or successor bead. The required ledger field successor therefore records
an ongoing steward: "Fleet composition steward: dev/fleet-eligibility". bh-38h7p records
introduction/review, not an invented successor or ownership that silently expires when this
implementation closes. Retained composition is explicit, with no cleanup successor filed.
Concrete expiry: remove each exact edge when its named production consumer is deleted or is
migrated behind an injected capability port; reassignment of the named steward requires explicit
reviewed ledger amendment. No checker or ADR waiver is needed.

The new intake check makes HQ concrete adapters reachable from legacy guard/lease. Those
adapters already depend on the process launcher/config/manifest types. SCC classification
records this composition reachability rather than newly introduced mutual module imports. Moving
all factories into a new bootstrap mechanism would change caller APIs, compatibility patch seams
and dependency injection across existing CLI/daemon/passthrough paths, exceeding U3 scope and
risking divergent intake enforcement. The pure predicate and authenticated HQ contract remain
separately testable.

Avoidable publication diagnostic dependency on bd facade was removed by importing its exact
package helper. Existing helper has no behavior difference and returns str. No product behavior
or baseline waiver is proposed.

| Exact importer -> module | Origin | Reason |
|---|---|---|
| `beadhive.frame_eligibility -> beadhive.config` (beadhive.config) | introduced edge or changed exact symbols | Selected HQ backend and host dispatch/lease settings belong at this application composition boundary; inventory cannot replace authority. |
| `beadhive.frame_eligibility -> beadhive.host` (beadhive.host) | introduced edge or changed exact symbols | Resolve local executor identity for authoritative hive ownership. |
| `beadhive.frame_eligibility -> beadhive.host_heartbeat_core` (beadhive.host_heartbeat_core.VerifiedObservation) | introduced edge or changed exact symbols | Retain exact authenticated observation type shared by production assessor and pure predicate. |
| `beadhive.frame_eligibility -> beadhive.hosts` (beadhive.hosts) | introduced edge or changed exact symbols | Load validated manifest capabilities and incarnation for the pure predicate. |
| `beadhive.frame_eligibility -> beadhive.hq_control_plane` (beadhive.hq_control_plane.control_plane, beadhive.hq_control_plane.verified_anchor_role) | introduced edge or changed exact symbols | Compose the explicit authority provider and verified anchor classification, preserving fail-closed enrollment and current remote reads. |
| `beadhive.frame_eligibility -> beadhive.registry` (beadhive.registry) | introduced edge or changed exact symbols | Resolve canonical hive requirements before authorizing intake. |
| `beadhive.guard -> beadhive.frame_eligibility` (beadhive.frame_eligibility) | introduced edge or changed exact symbols | Every bd write requires the same frame intake check before legacy primary guard; removing this call restores unsafe bypass. |
| `beadhive.host_heartbeat_core -> beadhive.config` (beadhive.config) | preexisting edge promoted into SCC | Existing observer receipt storage defaults to local host home; this is composition, never authoritative replay-floor reset. |
| `beadhive.host_heartbeat_core -> beadhive.gitref` (beadhive.gitref) | preexisting edge promoted into SCC | Existing signed Git heartbeat publication and signature verification use the bounded shared Git carrier primitives. |
| `beadhive.host_heartbeat_core -> beadhive.hosts` (beadhive.hosts) | preexisting edge promoted into SCC | Shared manifest release schema and typed incarnation data preserve authentication identity. |
| `beadhive.host_heartbeat_core -> beadhive.run` (beadhive.run.run) | preexisting edge promoted into SCC | Existing bounded Git subprocesses keep inherited launcher and telemetry behavior. |
| `beadhive.host_lease -> beadhive.frame_eligibility` (beadhive.frame_eligibility) | introduced edge or changed exact symbols | Adopt and renew check candidate eligibility and authenticated incumbent eviction at the actual CAS boundary. |
| `beadhive.host_lease -> beadhive.host` (beadhive.host) | introduced edge or changed exact symbols | Resolve current holder identity before choosing legacy Git or protected frame lease. |
| `beadhive.host_lease -> beadhive.hq_control_plane` (beadhive.hq_control_plane.control_plane) | introduced edge or changed exact symbols | Select the protected lease publication/read adapter for enrolled frames; legacy path remains separate. |
| `beadhive.hq_control_plane -> beadhive.config` (beadhive.config) | preexisting edge promoted into SCC | Existing explicit backend factory and local binding configuration select the protected provider. |
| `beadhive.hq_control_plane -> beadhive.gitref` (beadhive.gitref) | preexisting edge promoted into SCC | Existing signed policy, authority and config carriers retain encoding, CAS and bounded Git primitives. |
| `beadhive.hq_control_plane -> beadhive.host` (beadhive.host) | preexisting edge promoted into SCC | Existing publication uses enrolled holder identity and local key reference, never exports private key content. |
| `beadhive.hq_control_plane -> beadhive.host_heartbeat_core` (beadhive.host_heartbeat_core, beadhive.host_heartbeat_core.AuthoritySnapshot, beadhive.host_heartbeat_core.HeartbeatLease, beadhive.host_heartbeat_core.ObservationAuthority, beadhive.host_heartbeat_core._authority_matches, beadhive.host_heartbeat_core.ref_name) | preexisting edge promoted into SCC | Existing provider implements trusted snapshots, authenticated observations, heartbeat publication and epoch matching using shared types. |
| `beadhive.hq_control_plane -> beadhive.hosts` (beadhive.hosts, beadhive.hosts.HostManifest) | preexisting edge promoted into SCC | Existing provider validates exact registration manifest and authenticated membership schemas. |
| `beadhive.hq_control_plane -> beadhive.hq_fleet_config` (beadhive.hq_fleet_config.GitFleetConfigRevisionStore) | preexisting edge promoted into SCC | Existing provider constructs the committed Git config store for the revision port. |
| `beadhive.hq_control_plane -> beadhive.hq_manifest_publication` (beadhive.hq_manifest_publication) | preexisting edge promoted into SCC | Existing provider delegates exact own-manifest publication without staging unrelated HQ changes. |
| `beadhive.hq_control_plane -> beadhive.run` (beadhive.run.run) | preexisting edge promoted into SCC | Existing authority provider retains protected bounded Git execution through the established launcher. |
| `beadhive.hq_fleet_config -> beadhive.gitref` (beadhive.gitref) | preexisting edge promoted into SCC | Existing revision store uses the same canonical encoder for signed immutable configuration carriers. |
| `beadhive.hq_manifest_publication -> beadhive.gitref` (beadhive.gitref.GIT_TIMEOUT) | preexisting edge promoted into SCC | Existing publication shares Git timeout bounds; does not read authority from application state. |
| `beadhive.hq_manifest_publication -> beadhive.hosts` (beadhive.hosts) | preexisting edge promoted into SCC | Existing exact registration publication validates manifest identity and preserves schema. |
| `beadhive.hq_manifest_publication -> beadhive.run` (beadhive.run.run) | preexisting edge promoted into SCC | Existing isolated Git publication uses the established launcher to preserve subprocess environment. |

Required evidence: publication tests, import-boundary contract tests, final just check-native,
existing actual signed takeover/legacy/passthrough closure. Each exception must name these
actual consumers and preserve no externally claimed API beyond existing CLI/backend seams.

Independent review reproduced and approved the exact edge inventory. The operator explicitly
approved applying the matching snapshot
f6726a4fb391925da04f6d72d5658ca108214cc40f721d69a0ffa49f4eda2b42 (8 components, 58 modules, 151
edges, 174 symbols). No nonexistent successor or wildcard.

Historical capability closeout evidence is pinned to SOURCE_REVISION and its ledger; its
original 38-exception assertion remains unchanged. No test assertion or historical proof change
is proposed.

## Validation and review provenance

The qualified helper checkpoint is 0f050a206af6b5a66365244a5c41a3b13dfd077c. Publication
coverage passed three tests; naming hooks passed thirteen tests. The operator approved the exact
inventory and matching snapshot after independent scanner reproduction. The approval covers
retained composition ownership; it does not replace clean-tree validation or lifecycle review.
Historical closeout tests and metrics remain unchanged.

## Shared Dolt binding exact-graph amendment (`bh-v0k3i`)

The 26-edge inventory above is the historical U3 review, not the current exact graph.
The binding extracts the manifest model into neutral `host_manifest_contracts` while
`hosts` retains the same public class identity. Four former cyclic edges to `hosts`
(`cycle-fleet-composition-004`, `-010`, `-019`, `-025`) are now marked removed in the
ledger. The retained `hq_control_plane -> host_heartbeat_core` composition edge
(`-018`) additionally names `VerifiedObservation`, which the SQL public-read path
uses; its existing Fleet composition steward and expiry rule remain in force.

The config facade's existing `config -> config_store` import is promoted into a cyclic
component by the explicit HOST backend selector. The new
`config_store -> hq_control_plane.attach_fleet_config` import is the narrow bootstrap
composition that attaches the selected revision store; it does not add a second
effective-config resolver or import SQL runtime into config domain code. These exact
edges are part of the existing owned component after removal of the reviewed feedback
edges and require no new cycle exception or fabricated cleanup successor.

Independent exact review for this source found 8 cyclic components, 58 modules,
147 edges and 170 symbols, digest
`a3b51f4f4bacd3a0f1ea72e161e9d4a9d15b947f523539420a2dba392ffa5685`.
The count falls from the U3 151 edges/174 symbols, with no unowned component.
The import-boundary checker passes on the amended exact ledger; final frozen-tree
structural validation remains required.
