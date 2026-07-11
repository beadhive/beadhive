# Glossary — the routing entrypoint

One-line definition of every Beadhive / Beadflow concept term, alphabetical, each pointing to the
cluster file that covers it in depth. Start here for any term; follow the pointer for detail.
Terms tagged _(roadmap)_ are settled design on the near-term roadmap.

- **analyst** — the seat (`analyst/`) that does read-only codebase + web research for the planner;
  never implements. → [roles-and-seats.md](roles-and-seats.md)
- **assurance plane** _(proposed)_ — the cross-cutting gate layer where the warden attaches
  security + policy gates at pre-merge / pre-cut / pre-publish. → [agf-and-planes.md](agf-and-planes.md)
- **auto** — dispatch mode that collapses an epic only within `auto_budget` and single tier/gate,
  else fans out. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **backend** _(roadmap)_ — the pluggable storage engine (`bd`/`br`/`bw`/`nodb`) behind a common
  jsonl interchange, chosen with `bh beads switch`. → [storage-model.md](storage-model.md)
- **batch group** — several beads run in one shared worktree and merged once, subject to the
  scheduler's four guards. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **bead** — the atomic unit of work; a single issue driven through its lifecycle by `bh work`.
  → [bead-lifecycle.md](bead-lifecycle.md)
- **Beadflow** — the bead-based process this plugin drives; Beadhive is the factory that runs
  it on beads (see this repo's top-level process doc for how it relates to the abstract,
  tracker-independent process it instantiates). → [agf-and-planes.md](agf-and-planes.md)
- **collapse (collapsed)** — dispatch mode: one collapsed dispatcher works every ready bead
  sequentially in one shared worktree, merged once. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **container branch** — the `wt/bead/<type>/<id>` branch of an epic (`wt/bead/epic/<id>`) that
  its children fork from and land on. → [bead-lifecycle.md](bead-lifecycle.md)
- **contribution plane** _(roadmap)_ — the sibling-to-integration plane that fork-and-PRs work
  upstream over external rigs behind a human-only gate. → [agf-and-planes.md](agf-and-planes.md)
- **contributor** _(roadmap)_ — the persistent, rig-scoped seat (`contrib/`) that owns a target-repo
  dossier and drives gated upstream PRs. → [roles-and-seats.md](roles-and-seats.md)
- **control plane** — the governing plane where the four control seats (supervisor · director ·
  custodian · controller) run the factory itself. → [agf-and-planes.md](agf-and-planes.md)
- **controller** — the control seat (`ctrl/`) that reads factory telemetry/efficiency; read-mostly,
  no lifecycle mutation. → [roles-and-seats.md](roles-and-seats.md)
- **custodian** — the control seat (`cust/`) that owns config + secrets + repo provisioning +
  cleanup; the only control seat touching key material. → [roles-and-seats.md](roles-and-seats.md)
- **delivery plane** _(roadmap)_ — the sequential release → deploy → running plane where the
  operator reconciles desired-state. → [agf-and-planes.md](agf-and-planes.md)
- **developer** — the seat (`dev/`) that takes one assigned bead to a reviewable
  state in its ephemeral worktree, then submits. → [roles-and-seats.md](roles-and-seats.md)
- **director** — the control seat (`dir/`) that owns intake + fleet work routing and the interface
  to the per-rig dispatchers. → [roles-and-seats.md](roles-and-seats.md)
- **dispatcher** — the Integration seat (`disp/`; was `coordinator`) that delivers
  an epic; one seat parameterized by scope × mode (fanout/collapsed). → [roles-and-seats.md](roles-and-seats.md)
- **external rig** _(roadmap)_ — a virtualized `kind=external` view of a repo outside the factory
  boundary, contributed to by fork-and-PR. → [storage-model.md](storage-model.md)
- **Factory HQ** — the durable cross-rig beads store at `~/.ws/hq`, queried with `bh hq`;
  subsumes the hub. → [storage-model.md](storage-model.md)
- **fanout** — the default dispatch mode: each ready bead gets its own developer sub-agent and
  worktree, run in parallel. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **Gas Town** — the retired, non-normative nickname layer that maps aliases onto canonical
  seats; kept only as historical reference in
  [docs/design/gas-frameworks-comparison.md](../../../../../docs/design/gas-frameworks-comparison.md).
  → [roles-and-seats.md](roles-and-seats.md)
- **Head Office** — the workspace registry at `~/.ws/config.yaml → managed_repos`, one entry per
  rig; partitioned across the control seats. → [storage-model.md](storage-model.md)
- **hub** — the internal, disposable read-cache aggregation mechanism (`~/.ws/hub`) that powers
  Factory HQ; `bh hub` is a deprecated alias. → [storage-model.md](storage-model.md)
- **integration plane** — the execution plane where dispatcher → developer → merger land a
  molecule on an always-green line. → [agf-and-planes.md](agf-and-planes.md)
- **merger** — the seat (`merge/`) that serializes approved beads onto the
  integration branch, `--no-ff`, preserving history. → [roles-and-seats.md](roles-and-seats.md)
- **molecule** — an epic plus its child issues plus their dependency DAG (see swarm). →
  [bead-lifecycle.md](bead-lifecycle.md)
- **operator** _(roadmap)_ — the Delivery seat (`ops/`) that reconciles a release + IaC/gitops
  desired-state into a deployed system. → [roles-and-seats.md](roles-and-seats.md)
- **planner** — the seat (`plan/`) that turns a raw idea into a gated
  molecule. → [roles-and-seats.md](roles-and-seats.md)
- **planning plane** — the upstream plane where the planner decomposes an idea into a gated
  molecule. → [agf-and-planes.md](agf-and-planes.md)
- **prefix** — a rig's short, stable issue handle, derived from `org` + `repo` and excluding the
  provider. → [storage-model.md](storage-model.md)
- **releaser** _(roadmap)_ — the Release seat (`release/`) that cuts a release (version + changelog
  + tag). → [roles-and-seats.md](roles-and-seats.md)
- **release plane** _(roadmap)_ — the deliberate, gated plane that cuts a release (version
  determination + `cz` gate), distinct from integration. → [agf-and-planes.md](agf-and-planes.md)
- **reviewer** — the seat (`rev/`) that walks an approved branch and resolves or bounces its review
  gate. → [roles-and-seats.md](roles-and-seats.md)
- **rig** — one repo's beads DB, embedded as Dolt under its gitignored `.beads/`. →
  [storage-model.md](storage-model.md)
- **role** — the abstract archetype of a job (duties, skill, tools, model), instanced as a seat.
  → [roles-and-seats.md](roles-and-seats.md)
- **scheduler** — `bh work schedule`, which forms groups (child epics, planner batches, private
  chains, singletons) under four guards. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **seat** — a role instance bound to an identity + permission archetype (`disp/<name>`,
  `dev/<name>`), a worktree, and a rig. → [roles-and-seats.md](roles-and-seats.md)
- **session** — a running loop (agent or human) that MAY hold multiple seats over its life, but
  wields exactly one seat's permissions per action. → [roles-and-seats.md](roles-and-seats.md)
- **supervisor** — the control seat (`super/`) that governs the whole
  factory + policy and launches the other control seats. → [roles-and-seats.md](roles-and-seats.md)
- **swarm** — the beads-primitive term for a molecule (epic + children + dep DAG). →
  [bead-lifecycle.md](bead-lifecycle.md)
- **triplet** — the `provider:` / `org:` / `repo:` labels that carry a rig's identity on every
  issue. → [storage-model.md](storage-model.md)
- **verifier** _(lens)_ — the Assurance lens (`verify/`) for acceptance/e2e (developer-check +
  reviewer-demo + CI); a seat only when e2e needs its own identity. → [roles-and-seats.md](roles-and-seats.md)
- **warden** — the Assurance seat (`warden/`) that gates security + policy only (secret-scan, SBOM,
  policy-as-code); read + block, no writes. → [roles-and-seats.md](roles-and-seats.md)
- **workstream** — an epic-of-epics: an `issue_type=epic` bead whose children are themselves
  epics. → [bead-lifecycle.md](bead-lifecycle.md)
