---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: preflight
  title: Probe the machine — read-only, and ask nothing yet
  performer: agent
  action:
    type: script
    script: scripts/preflight.sh
    timeout_seconds: 120
  verify:
    type: script
    script: scripts/preflight.sh
    success_exit: 0
    output_schema: json
  interactions: []
  on_failure:
    strategy: abort
  effect: read-only
  estimated_duration_minutes: 1
  tags: [probe, read-only]
---

Detects the OS and architecture, which package managers are available, which of `bh`, `bd`,
`dolt`, `gh`, `git-workspace`, `git`, `nix` and `claude` are already installed and at what
version, which harness is present, and whether `~/.beadhive` has already been scaffolded. It
emits all of that as one JSON object on stdout.

## Why this step asks nothing

`interactions: []` is deliberate. There is nothing worth asking before you know what the
machine already has — every question here would be one whose answer is sitting on disk. Asking
first is how a guided install becomes an interrogation, and it is also how you end up asking a
user to choose a route their hardware cannot run.

## Why it mutates nothing

`effect: read-only`. The script writes no file, creates no directory and refreshes no cache. In
particular it does **not** call `bh setup check`, which writes
`~/.beadhive/setup-state.json` — a probe is not allowed to change the thing it is probing, and
`bh` may not even be installed yet.

## What later steps read from it

Every later step consumes this JSON rather than re-probing. The keys that carry decisions:

| Key | Read by | For |
|---|---|---|
| `managed_route.supported` | 020 | false on Intel macOS → the route is **forced** to PyPI |
| `managed_route.nix_present` | 020, 091 | absent → offer the nix installer, never run it |
| `package_managers.*` | 030 | which PyPI installer command to offer |
| `tools.bh` | 030, 040 | the version *before* the install, to compare against after |
| `harness` | 060, 065 | not `claude-code` → those two steps skip, they do not fail |
| `config.config_yaml` | 050 | already scaffolded is the normal case, not an error |
| `config.hq` | 070 | ditto for Factory HQ |

Re-probing in a later step is the mistake to avoid: two probe implementations is exactly how
this Guide and `bh setup check` start disagreeing about one machine. Collapsing them into a
single shared implementation is filed as `bh-0olv9.7`; until it lands, `scripts/preflight.sh`
is this Guide's only prober.

## Verify

The verify re-runs the same script with `output_schema: json`, which is what records the probe
as machine-readable state for the walk. Running a read-only probe twice costs a second and
nothing else — and it means the state the walk carries forward is the state that was *checked*,
not a side effect of the action.

The script always exits 0. "nix is absent", "no `bh` yet", "not Claude Code" are all **answers**
— a probe that fails on a perfectly ordinary machine is a gate wearing a probe's clothes.

## What can go wrong

A nonzero exit here means the probe itself could not run — no POSIX shell, or `uname`
unavailable. That is not a machine this Guide can install onto, so `on_failure` is `abort`;
nothing has been touched, which is the `aborted-clean` end state.
