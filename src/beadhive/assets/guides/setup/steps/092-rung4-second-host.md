---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: rung4-second-host
  title: "Rung 4 — a second host (POINTER: the command runs on the other machine)"
  requires: [rung2-hq-remote]
  accepts_skipped: true
  performer: either
  action:
    type: prompt
    prompt: |
      OPT-IN ONLY, and this step EMITS a command rather than running one.
      Rung 4 runs on a DIFFERENT machine. A step that pretended otherwise
      would either hang waiting for a host it cannot see, or claim a
      provision it never performed.

      1. CHECK THE PREREQUISITE FIRST, BEFORE THE ROLE CHOICE — run:
           scripts/check-hq-remote.sh

         Rung 4 HARD-REQUIRES rung 2. A worker joins by CLONING HQ, and you
         cannot clone an HQ that exists only on one laptop. There is no way
         around this one.

         exit 1 (no remote) or exit 3 (wired but unpublished): STOP HERE.
         Send the user to step 090 and finish it on THIS machine first.
         Do not present the role choice, do not emit the command. This is
         exactly what docs/ONBOARDING.md's "Adding a second machine" opens
         by warning about — otherwise the user meets the requirement as a
         provisioning failure halfway through setting up the new machine,
         which is the same lesson learned twice and the expensive way.

      2. Only once the probe exits 0, walk the ROLE decision. A role says
         how readily and how long a host holds a hive's host lease. That
         is the whole axis — it is NOT a permission grade.

         executor   holds the lease 4x the TTL (2h on the 30-minute
                    default). For an always-on machine that owns repos;
                    the mature shape is one executor per repo. This is the
                    role for the VM you are adding.
         transient  baseline TTL, releases on exit. CI-runner shaped —
                    spun up per task.
         viewer     never primary, by definition. For human laptops:
                    cannot claim, submit or merge, and `bh host lease
                    adopt` refuses before touching either remote. Filing
                    still works — `bh bd create` for a top-level bead
                    needs no lease at all.

      3. EMIT the command to run THERE, on the new machine, after
         installing bh on it (steps 010-040 of this Guide):

           bh host provision --role executor

         Substitute the chosen role. It clones HQ from the wired remote and
         then adopts the host — setup check, config init, git-workspace
         update, hq.remote, hq clone, host init, per-hive bead sync,
         permission fix, verify, each probing before it acts.
         `--dry-run` prints the ordered plan and changes nothing; offer it
         first.

      4. READ OUT RUNG 4'S GAP NOTE, verbatim. Three lines, from
         docs/ADOPTION.md. A user must not discover advisory-only lease
         enforcement by hitting it:

           - Lease enforcement is advisory until the epoch fence fires
             again — bh-ban1j.
           - A provisioned host cannot run an agent seat until provision
             installs the plugin — bh-tx2hp.
           - The file-here / execute-there cycle is unproven until the E2E
             runs — bh-i7ws9.

      5. Back on THIS machine, `bh host list` shows every host with its
         role and staleness. Two hosts, neither stale, is the rung-4
         "you are here" probe.
  verify:
    type: agent_judgment
  interactions:
    - id: want-rung-4
      when: before
      kind: confirm
      prompt: |
        Optional. Rung 4 adds a Linux executor: a machine that keeps
        working while your laptop sleeps, with this laptop supervising.
        It needs rung 2 first (the new host joins by cloning HQ), a VM to
        keep running, and one lease decision per hive. Walk it now?
      required: false
    - id: host-role
      when: after
      kind: choice
      prompt: |
        Which role for the new host? This says how readily and how long it
        holds a hive's lease — it is not a permission grade.
      choices:
        - "executor — holds the lease 4x the TTL (2h on the 30-minute default). An always-on machine that owns repos; the mature shape is one per repo."
        - "transient — baseline TTL, releases on exit. CI-runner shaped, spun up per task."
        - "viewer — never primary, by definition. A human laptop: cannot claim, submit or merge, but can still file top-level beads and read everything."
  on_failure:
    # `reason` is a kebab-case LABEL, matched against the runtime's
    # `step.failed.fields.reason`; the argument for each is in the body.
    - reason: rung-2-not-reached
      strategy: abort
    - reason: hq-status-inconclusive
      strategy: ask
  effect: none
  terminates_at: rung-1-reached
  estimated_duration_minutes: 5
  tags: [rung-transition, optional, pointer]
---

`docs/ADOPTION.md`'s rung 4: **a Linux executor, adopted into HQ.** The laptop stops executing
and starts supervising.

## A pointer, not a walk

`effect: none` — this step changes nothing. Rung 4 runs on a machine this Guide is not on, and a
step that pretends otherwise will either hang waiting for a host it cannot see or report a
provision it never performed. What it *can* do, and does: check the prerequisite here, walk the
role decision here, and hand over the exact command to run there.

The full step-by-step walkthrough is `docs/ONBOARDING.md`'s "Adding a second machine — the daily
driver stays HQ". This step decides *whether* and *with which role*; that section is how it is
actually done.

## The prerequisite is checked BEFORE the role choice

This ordering is the point of the step. Rung 4 hard-requires rung 2 — a worker joins by
**cloning HQ**, and you cannot clone an HQ that exists only on one laptop. There is no way
around it.

So `scripts/check-hq-remote.sh` runs first, and a `1` (no remote) or `3` (wired but one half
unpublished) stops the step before anything else is presented. The user is sent back to step
090, on *this* machine, and returns afterwards.

Getting that order wrong is the failure `docs/ONBOARDING.md` opens its second-machine section
by warning about: you meet the requirement as a provisioning failure halfway through setting up
the new machine, with a half-configured VM in front of you. Same lesson, learned the expensive
way.

`accepts_skipped: true` is what lets a machine that was *already* on rung 2 reach this step
without walking 090 — the dependency on rung 2 is declared, and the probe rather than the walk
history is what proves it.

## Failure routing — this is the one step that genuinely aborts

`on_failure`'s `reason` is a **kebab-case label**: the runtime matches it verbatim against
`step.failed.fields.reason`, so a clause labelled with a paragraph can never be selected and its
declared routing never fires. The argument goes here.

**`rung-2-not-reached` (probe exit 1 or 3) → `abort`, and it stays an `abort`.** HQ has no
remote, or one half is unpublished. This is the only clause in the whole Guide where continuing
is *impossible* rather than merely worse: rung 4 hard-requires rung 2, because the new host joins
by **cloning HQ**, and there is no version of "accept the gap and carry on" that ends with a
second machine. Routing it into the rescue Guide for symmetry with 040/060/065/091 would be
exactly wrong — there is no absence to accept, only a prerequisite to go and satisfy. Nothing has
been done at this point either (`effect: none`), so nothing is left half-finished: the step stops
before the role choice and sends the user to 090 on *this* machine.

**`hq-status-inconclusive` (probe exit 2) → `ask`.** `bh hq status` could not be read, so the
prerequisite is unproven rather than known-absent. Do not emit a provisioning command against an
unverified HQ. `ask` is right here for the same reason it is right at 090: the answer it can
offer — fix the cause and retry, or stop — is the answer this clause actually wants. That is the
test for an honest `ask`, and it is what the four clauses at 040/060/065 failed.

## Roles are a tenure axis, not a permission grade

| Role | Tenure | For |
|---|---|---|
| `executor` | 4× the lease TTL (2h on the 30-minute default) | an always-on machine that owns repos; one per repo is the mature shape |
| `transient` | baseline TTL, releases on exit | CI-runner shaped, spun up per task |
| `viewer` | never primary, by definition | human laptops — cannot claim, submit or merge; `bh host lease adopt` refuses before touching either remote |

A `viewer` laptop is still useful, and it is worth saying why: **filing is not executing.** A
top-level `bh bd create` needs no lease from any host, and neither does any read. The lease is
required for `claim`, `assign`, `submit`, `merge`, `bh plan file`, and any `--parent` create —
because a child id comes from a per-parent counter and two hosts allocating one concurrently
mint the same id.

## The command runs there

On the new machine, with `bh` installed (steps 010–040 of this Guide):

```sh
bh host provision --role executor
```

It clones HQ from the wired remote and then adopts the host, probing before each step.
`--dry-run` prints the ordered plan with zero mutation and is worth offering first.

## Rung 4 is not finished yet — say so before they hit it

Three known gaps, carried from `docs/ADOPTION.md` so a user meets them as a stated limitation
rather than as a bug:

- Lease enforcement is advisory until the epoch fence fires again — `bh-ban1j`.
- A provisioned host cannot run an agent seat until provision installs the plugin — `bh-tx2hp`.
- The file-here / execute-there cycle is unproven until the E2E runs — `bh-i7ws9`.

The second one in particular changes what the user should expect on day one: a provisioned host
is adopted into HQ but cannot yet run an agent seat.

## Confirming it landed

Back on this machine, `bh host list` renders every host with its role and staleness. Two hosts,
neither stale, is rung 4's own "you are here" probe. One host means you are still on rung 1 or 2.
