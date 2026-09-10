# Official contract-schema release

Beadhive publishes one complete, language-neutral v1 contract bundle inside the installed Python
package at `beadhive/schemas/contracts/v1.0.0`. The bundle is a projection of the canonical
domain and transport owners; it does not become a second registry, start a server, discover
external plugins, or generate client code.

## Install and look up an artifact

Pin a Beadhive package release through the normal Python package mechanism, then pin both the
contract release and stable artifact ID in the consuming application. A consumer that needs a
JSON document can use the checksum-verifying lookup directly:

```python
from beadhive.contract_release import RELEASE_VERSION, load_artifact, release_root

assert RELEASE_VERSION == "1.0.0"
config_schema = load_artifact(
    "urn:beadhive:wire-schema:config:1",
    release_root(),
)
```

`load_artifact` validates canonical bytes, the inventory path, artifact identity, version, and
SHA-256 before returning a document. Consumers that are not written in Python can copy the
installed `beadhive/schemas/contracts/v1.0.0` directory as a release unit, read
`inventory.json`, verify each listed digest, and resolve conformance cases through
`conformance.json`. No hosted registry or generated client is required.

## Ownership and contents

The inventory records a source owner, compatibility policy, conformance example, and checksum
for every artifact. The release assembler only invokes or snapshots these existing authorities:

| Family | Canonical owner |
| --- | --- |
| Config | `beadhive.modules.config.application.schema_artifacts` |
| Plugin manifests | the published plugin-manifest v1 wire schema |
| Plugin config fragments | `beadhive.modules.config.application.schema_artifacts` |
| Operation catalog | `beadhive.kernel.operations` |
| CLI and MCP projections | `beadhive.transport_inventory` |
| OpenAPI | `beadhive.daemon_openapi` |
| Gateway wire families | `beadhive.gateway_wire_contracts` |
| Lifecycle events | `beadhive.kernel.lifecycle.contracts` |
| Telemetry event envelope | `beadhive.kernel.telemetry` |
| Seat, launch, workspace, prepare/commit/abort, and receipts | `beadhive.modules.agents.domain` |

JSON Schema artifacts use the `json-schema-additive-v1` policy. Catalogs are append-only within
v1, and OpenAPI changes must be additive. Removal or rename, new required fields, closed-union
drift, operation-identity drift, and projection-privilege drift require a new major release.
The executable policy baseline is the pinned, package-owned bundle at
`beadhive/schemas/contracts/baselines/v1.0.0`. It is deliberately separate from both the
generated release directory and current source owners: ordinary generation never rewrites it,
and its complete snapshot digest is fixed in `beadhive.contract_release`. Advancing that
baseline is a publication decision and must be reviewed explicitly.

## Reproduce and check

Generation is deterministic and local-only:

```console
uv run python scripts/generate_contract_release.py --write
uv run python scripts/generate_contract_release.py --check
```

Both `--check` and `--write` first compare current canonical-owner output with that immutable
prior-published baseline. The gate applies the inventory row's declared policy: JSON Schema
shape cannot be removed or narrowed, catalog mappings and lists are append-only without reorder,
and OpenAPI routes, methods, parameters, request bodies, responses, media types, and schemas
cannot be removed or narrowed. Optional schema properties, catalog tail entries, and new OpenAPI
routes/methods remain compatible additive changes. `--check` is read-only and is part of
`just wire-schema-compat`. Two clean `--write` runs must produce byte-identical files.
OpenAPI access metadata follows the same rule at document, path, and operation scope: anonymous
access cannot become restricted, named security schemes or required scopes cannot be tightened,
and every previously accepted security alternative must remain available. Removing requirements
or adding an equal-or-less-restrictive alternative is compatible. Existing CLI, MCP, and gateway
catalog members likewise cannot acquire a new required scope or privilege within v1.
Extension metadata on an existing public member is fail-closed: unknown `x-*` keys and extension
map members must remain exact through nested objects, arrays, and type changes, including
additions and removals hidden in a new ordinary container. Extension keys use the exact lowercase
ASCII `x-` prefix. Case/width variants, single-code-point NFKC equivalents of Latin `x`, explicit
Cyrillic-`x` lookalikes, Unicode dash characters, and U+2212 MINUS SIGN are rejected rather than
normalized. Classification never rewrites contract-visible key bytes, and names in standard HTTP
header and JSON Schema property maps remain data rather than extension metadata. Only explicitly
recognized Beadhive access keys at an actual OpenAPI or catalog member use the semantic
tightening/loosening comparison. OpenAPI `security` is semantic only at an actual access-bearing
document, path, operation, or named scheme, never inside generic extension metadata. Standard
fields such as `title`, `summary`, and `description` remain non-contract annotations.
The JSON Schema comparison walks nested definitions and applicators, rejects newly restrictive
types, required fields, bounds, patterns, formats, divisibility, uniqueness, dependencies, and
compositions, and fails closed when an unclassified assertion keyword changes. Removing a proven
constraint is compatible, but closed-union membership remains intentionally fixed within v1.

Publication is transactional: `--write` renders into a sibling staging directory, validates its
complete canonical file set, then replaces the target through an atomic rename with rollback of a
prior target if the publish rename fails. The target must contain exactly the candidate inventory's
files and their required directories; stale files, directories, symlinks, and other non-regular
entries fail `--check` and disappear only when a complete staged bundle is committed. Root and
ancestor symlink or traversal targets are refused, and validation never follows nested symlinks.
