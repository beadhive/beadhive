# Report Channel — a "report-to" self-description protocol

**Status:** Accepted (spec only) · **Scope:** design deliverable — descriptor + discovery
forms. Consumption / auto-routing is deliberately **out of scope** and deferred to a later
bead.

## Context

Beadflow already has an *intake* terminal: `bh report <hive> "<title>"` files a bug/feature/chore
into a hive we own, landing it as untriaged intake (`beadhive/report.py`). What's missing is the
other half — a way for **any** tool or service to *declare*, machine-readably, **where and how
to report issues about it**. Today that knowledge lives in READMEs and human memory; an agent
that hits a broken CLI or MCP server has nowhere structured to look.

This is the same problem `security.txt` solved for one narrow case. **RFC 9116**
(`/.well-known/security.txt`) lets a site publish a fixed *security* contact. We want to
generalize that from "security contact" to "**any** report channel" — bugs, features, chores —
and to make it discoverable across the three shapes a tool ships as in this ecosystem: a CLI, an
MCP server, and an HTTP service.

The descriptor is intentionally the *only* thing this bead delivers. Nothing here reads,
resolves, or routes a descriptor — that consumption layer (and its coupling to
`bh report` / triage) is a separate, later bead.

## Decision

Define a small, extensible **`report_channel` descriptor** plus a **discovery document** that
carries one or more of them, and pin three discovery forms (one per tool shape). A normative
JSON Schema ([`schemas/report-channel.schema.json`](schemas/report-channel.schema.json))
defines the wire shape; a worked example lives in
[`schemas/report-channel.example.json`](schemas/report-channel.example.json) and is validated
against the schema by `tests/test_report_channel.py` (so `just check` covers it).

### The `report_channel` descriptor

A descriptor is one place issues can go. Minimal on purpose — two load-bearing fields, two
optional hints:

| Field    | Req? | Meaning |
|---|---|---|
| `kind`   | yes  | Channel type — one of `beads-rig`, `github-issues`, `url`, `email`. Fixes how `target` is read. |
| `target` | yes  | The address, interpreted per `kind` (see below). |
| `verb`   | no   | How-to hint: a copy-pasteable command / one-line instruction for filing here. Advisory only. |
| `labels` | no   | Labels/tags a filer SHOULD apply to reports on this channel. |

`kind` → how `target` is interpreted:

| `kind`          | `target` is… | Example |
|---|---|---|
| `beads-rig`     | a hive identity — a `<provider>/<org>/<repo>` triplet (or hive id) filed via `bh report` | `github/beadhive/beadhive` |
| `github-issues` | an `owner/repo` slug | `beadhive/beadhive` |
| `url`           | an absolute https intake endpoint or form | `https://example.com/report` |
| `email`         | an addressee | `bugs@example.com` |

`beads-rig` is the native, highest-fidelity channel in this ecosystem: it lands a triage-ready
bead. The other kinds are the interop fallbacks so a *non-Beadflow* consumer (or a tool we don't own)
can still be described.

### The discovery document

A discovery form returns a small envelope, not a bare descriptor, so a tool can list channels in
preference order:

```json
{
  "$schema": "https://raw.githubusercontent.com/beadhive/beadhive/main/docs/schemas/report-channel.schema.json",
  "version": "1",
  "channels": [
    {
      "kind": "beads-rig",
      "target": "github/beadhive/beadhive",
      "verb": "bh report github/beadhive/beadhive \"<title>\"",
      "labels": ["intake:untriaged"]
    }
  ]
}
```

`channels` is ordered **most-preferred first**; a consumer picks the first channel it can drive.
`version` is `"1"` and bumps only on a breaking shape change.

### Minimal + extensible

The descriptor and document both set `additionalProperties: false` — a typo in a core field is a
validation error, not silent data loss. Forward-compatible extensions ride on
reverse-DNS-style **`x-` prefixed** keys (e.g. `x-com.example.priority`), which the schema
admits without loosening core validation. This mirrors how HTTP headers and `security.txt`
absorb extensions. New *first-class* fields are a deliberate, versioned change; `x-` keys are the
escape hatch until then.

## Discovery forms (one per tool shape)

### CLI — `report-target` subcommand and/or manifest field

A CLI SHOULD expose its channels via a stable subcommand:

```console
$ <tool> report-target --json
{"version":"1","channels":[{"kind":"beads-rig","target":"github/beadhive/beadhive"}]}
```

`--json` emits exactly one discovery document on stdout. A tool that can't add a subcommand MAY
instead ship the same document as a **manifest field** — e.g. a `report_channel` key in
`package.json` / `pyproject.toml` tool table — so the descriptor is discoverable statically
without executing the tool.

### MCP — carried on `serverInfo`, primarily via `_meta`

**Research finding (cited).** The Model Context Protocol has **no dedicated field for an issue /
report channel.** Reviewing the MCP schema:

- The `initialize` result returns an `Implementation` object (`serverInfo`). In revision
  **`2025-06-18`** it carries only `name`, `title?`, `version` (via `BaseMetadata`). The
  **draft** revision adds `description?`, `websiteUrl?`, and `icons?` to `Implementation`
  (`schema/draft/schema.ts`, `interface Implementation extends BaseMetadata, Icons`). None of
  these is a report/issue endpoint — `websiteUrl` is a homepage and `description` is free prose.
- Tool `annotations` (`ToolAnnotations`: `title`, `readOnlyHint`, `destructiveHint`,
  `idempotentHint`, `openWorldHint`) are *behavior* hints, not contact metadata.
- MCP has **no `.well-known` concept**; resources are server-defined URIs.
- The spec's sanctioned extensibility mechanism is **`_meta`** — "a `_meta` field, which clients
  and servers use to attach additional metadata to their interactions." Its key-naming rules
  **reserve** any second label of `modelcontextprotocol` / `mcp` for MCP itself and direct other
  parties to namespace with a reverse-DNS prefix (e.g. `com.example.mcp/…`;
  `schema/draft/schema.ts`, the `MetaObject` / `_meta` documentation).

  *Sources:* `modelcontextprotocol/modelcontextprotocol` schema —
  `schema/2025-06-18/schema.ts` (`Implementation` / `BaseMetadata`) and `schema/draft/schema.ts`
  (`Implementation`, `ToolAnnotations`, and the `_meta` reserved-prefix rules), verified against
  the published spec.

**Decision for MCP.** Since there is no native field, an MCP server declares its channels under a
reverse-DNS-namespaced `_meta` key on the `serverInfo` returned by `initialize`, whose value is a
discovery document:

```json
{
  "serverInfo": {
    "name": "bh",
    "version": "0.1.0",
    "_meta": {
      "dev.bh.agf/report-channel": {
        "version": "1",
        "channels": [{ "kind": "beads-rig", "target": "github/beadhive/beadhive" }]
      }
    }
  }
}
```

A server MAY additionally expose the same document as a **resource** at the conventional URI
`report-channel://self` for clients that browse resources rather than inspect `serverInfo`. The
`_meta` form on `serverInfo` is primary because it's available immediately at handshake, needs no
extra round-trip, and rides the one extension mechanism the spec actually sanctions. If a future
MCP revision adds a first-class report/issue field, this spec should defer to it.

### HTTP service — `/.well-known/report-channel.json`

Directly analogous to `security.txt`. An HTTP service serves the discovery document at:

```text
GET /.well-known/report-channel.json
Content-Type: application/json
```

Same envelope, same schema. This is the `security.txt` pattern generalized: a well-known,
unauthenticated, cacheable JSON document at a fixed path.

## Prior art

- **RFC 9116 — `security.txt`** (`/.well-known/security.txt`): a well-known file for a site's
  *security* contact. This spec generalizes its "publish where to reach us" idea from a fixed
  security purpose to any report channel, and adopts its `/.well-known/` discovery path for the
  HTTP form.
- **`.well-known` URI registry (RFC 8615)**: the mechanism behind the HTTP form.
- **MCP `_meta` extension convention**: the reverse-DNS-namespaced metadata channel used for the
  MCP form (see the research finding above).

## Consequences

- One descriptor shape describes bug/feature/chore report channels for CLIs, MCP servers, and
  HTTP services, with `beads-rig` as the native high-fidelity channel and `github-issues` /
  `url` / `email` as interop fallbacks.
- The schema is the single source of truth and is exercised in CI (`just check`), so the example
  and any future producer can be validated against it.
- **Deferred (not in this bead):** reading/resolving a descriptor, and routing a resolved channel
  into `bh report` / triage. No consumption or auto-routing code is introduced here.

## References

- Schema: [`docs/schemas/report-channel.schema.json`](schemas/report-channel.schema.json)
- Example (validated in CI): [`docs/schemas/report-channel.example.json`](schemas/report-channel.example.json)
- Validation test: `tests/test_report_channel.py`
- Intake terminal this will eventually feed: `src/beadhive/report.py` (`bh report <hive>`)
