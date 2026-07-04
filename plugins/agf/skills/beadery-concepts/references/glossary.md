# Glossary — the routing entrypoint

One-line definition of every Beadery / AGF concept term, alphabetical, each pointing to the
cluster file that covers it in depth. Start here for any term; follow the pointer for detail.
Terms tagged _(roadmap)_ are settled design on the near-term roadmap.

- **AGF (Agentic Git-Flow)** — the abstract, tracker-independent methodology Beadery implements;
  Beadery is the factory that runs AGF on beads. → [agf-and-planes.md](agf-and-planes.md)
- **auto** — dispatch mode that collapses an epic only within `auto_budget` and single tier/gate,
  else fans out. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **backend** _(roadmap)_ — the pluggable storage engine (`bd`/`br`/`bw`/`nodb`) behind a common
  jsonl interchange, chosen with `bdry beads switch`. → [storage-model.md](storage-model.md)
- **batch group** — several beads run in one shared worktree and merged once, subject to the
  scheduler's four guards. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **bead** — the atomic unit of work; a single issue driven through its lifecycle by `bdry work`.
  → [bead-lifecycle.md](bead-lifecycle.md)
- **collapse (collapsed)** — dispatch mode: one epic-coordinator works every ready bead
  sequentially in one shared worktree, merged once. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **container branch** — the `wt/bead/<type>/<id>` branch of an epic (`wt/bead/epic/<id>`) that
  its children fork from and land on. → [bead-lifecycle.md](bead-lifecycle.md)
- **contribution plane** _(roadmap)_ — the sibling-to-integration plane that fork-and-PRs work
  upstream over external rigs behind a human-only gate. → [agf-and-planes.md](agf-and-planes.md)
- **contributor** _(roadmap)_ — the persistent, rig-scoped seat that owns a target-repo dossier
  and drives gated upstream PRs. → [roles-and-seats.md](roles-and-seats.md)
- **control plane** — the commissioning plane where the superintendent stands up and configures
  rig sites. → [agf-and-planes.md](agf-and-planes.md)
- **coordinator** — the seat (overseer) that dispatches ready beads to developers, watches gates,
  and serializes merges. → [roles-and-seats.md](roles-and-seats.md)
- **developer** — the seat (polecat) that takes one assigned bead to a reviewable state in its
  worktree, then submits. → [roles-and-seats.md](roles-and-seats.md)
- **external rig** _(roadmap)_ — a virtualized `kind=external` view of a repo outside the factory
  boundary, contributed to by fork-and-PR. → [storage-model.md](storage-model.md)
- **Factory HQ** — the durable cross-rig beads store at `~/.ws/hq`, queried with `bdry hq`;
  subsumes the hub. → [storage-model.md](storage-model.md)
- **fanout** — the default dispatch mode: each ready bead gets its own developer sub-agent and
  worktree, run in parallel. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **Gas Town** — the nickname layer that names five seats only (polecat, overseer, the Refinery,
  the cartographer, the pit crew). → [roles-and-seats.md](roles-and-seats.md)
- **Head Office** — the workspace registry at `~/.ws/config.yaml → managed_repos`, one entry per
  rig. → [storage-model.md](storage-model.md)
- **hub** — the internal, disposable read-cache aggregation mechanism (`~/.ws/hub`) that powers
  Factory HQ; `bdry hub` is a deprecated alias. → [storage-model.md](storage-model.md)
- **integration plane** — the execution plane where coordinator → developer → merger land a
  molecule on an always-green line. → [agf-and-planes.md](agf-and-planes.md)
- **merger** — the seat (the Refinery) that serializes approved beads onto the integration branch,
  `--no-ff`, preserving history. → [roles-and-seats.md](roles-and-seats.md)
- **molecule** — an epic plus its child issues plus their dependency DAG (see swarm). →
  [bead-lifecycle.md](bead-lifecycle.md)
- **planner** — the seat (the cartographer) that turns a raw idea into a gated molecule. →
  [roles-and-seats.md](roles-and-seats.md)
- **planning plane** — the upstream plane where the planner decomposes an idea into a gated
  molecule. → [agf-and-planes.md](agf-and-planes.md)
- **prefix** — a rig's short, stable issue handle, derived from `org` + `repo` and excluding the
  provider. → [storage-model.md](storage-model.md)
- **release plane** _(roadmap)_ — the deliberate, gated plane that cuts a release (version
  determination + `cz` gate), distinct from integration. → [agf-and-planes.md](agf-and-planes.md)
- **reviewer** — the seat that walks an approved branch and resolves or bounces its review gate.
  → [roles-and-seats.md](roles-and-seats.md)
- **rig** — one repo's beads DB, embedded as Dolt under its gitignored `.beads/`. →
  [storage-model.md](storage-model.md)
- **role** — the abstract archetype of a job (duties, skill, tools, model), instanced as a seat.
  → [roles-and-seats.md](roles-and-seats.md)
- **scheduler** — `bdry work schedule`, which forms groups (child epics, planner batches, private
  chains, singletons) under four guards. → [dispatch-and-scheduling.md](dispatch-and-scheduling.md)
- **seat** — a role instance bound to a session, identity (`coord/<name>`, `crew/<name>`),
  worktree, and rig. → [roles-and-seats.md](roles-and-seats.md)
- **superintendent** — the control-plane seat that commissions and configures rigs; the one seat
  that does not pair with `work`. → [roles-and-seats.md](roles-and-seats.md)
- **swarm** — the beads-primitive term for a molecule (epic + children + dep DAG). →
  [bead-lifecycle.md](bead-lifecycle.md)
- **triplet** — the `provider:` / `org:` / `repo:` labels that carry a rig's identity on every
  issue. → [storage-model.md](storage-model.md)
- **workstream** — an epic-of-epics: an `issue_type=epic` bead whose children are themselves
  epics. → [bead-lifecycle.md](bead-lifecycle.md)
