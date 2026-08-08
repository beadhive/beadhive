---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: first-hive
  title: Onboard ONE hive and prove the loop with bh work ready
  requires: [hq-init]
  performer: either
  action:
    type: prompt
    prompt: |
      Onboard exactly ONE repo, then prove the loop answers from it.

      0. PROBE FIRST — run:
           bh hive list

         If the repo the user names is already registered, report ALREADY
         SATISFIED and go straight to step 3. `bh hive onboard` refuses an
         already-configured hive without --force, and --force is NOT the
         answer here: re-registering can change the derived prefix, and the
         prefix is baked into every bead id already minted.

      1. Get the triplet. `bh hive onboard` REQUIRES a
         {PROVIDER/ORG/REPO} argument — there is no bare form and no
         inference from cwd. Ask for it if the user has not given it:
           bh hive onboard github/<org>/<repo>

         Add --clone-url <url> only when the target directory is absent and
         the repo must be cloned down first.

      2. Offer the command. Zero-footprint is the DEFAULT: nothing is
         committed into the repo unless a furnishing flag is passed. Name
         the ones that change that, so a flag is a choice and not a
         surprise:
           --furnish    tracked in-repo AGF furniture
           --claude     .claude/ settings + sandbox grant
           --skills     copy the bundled role skills into ./skills
           --agents     an AGENTS.md AGF hint stanza
           --opencode   furnish for OpenCode — this is the OpenCode path,
                        and the reason step 065's Claude plugin is optional
         Default to none of them unless the user asks.

      3. Prove it. From inside the onboarded repo:
           bh work ready

         Answering is the goal. An EMPTY ready list is a PASS — a fresh
         hive has no beads yet. What is being proved is that the command
         resolves the hive, reads its store, and answers; not that there is
         work in it. Say that explicitly, or an empty list reads as a
         failure.

      4. Announce the finish: "you now have a running factory on rung 1."
         Then stop. Everything from 090 on is optional and is entered only
         if the user asks for it.

      ONE hive, not all of them. Surveying a machine's repos and
      bulk-onboarding is a different job with different failure modes;
      point at `bh hive onboard` for the rest and leave it there.
  verify:
    type: agent_judgment
  interactions:
    - id: which-repo
      when: before
      kind: text
      prompt: |
        Which repo should become your first hive? Give it as
        provider/org/repo — for example `github/beadhive/beadhive`. One is
        enough: this is about proving the loop works, and you can onboard
        the rest with the same command afterwards.
    - id: approve-onboard
      when: before
      kind: confirm
      prompt: |
        `bh hive onboard <provider>/<org>/<repo>` registers that repo as a
        hive, initialises its bead store and syncs it into HQ. It is
        zero-footprint by default — nothing is committed into the repo
        unless you ask for a furnishing flag. Approve?
  on_failure:
    strategy: ask
  effect: reversible
  terminates_at: rung-1-reached
  estimated_duration_minutes: 5
  tags: [configure, hive, rung-1]
---

This is the step that turns a configured machine into a working one. It ends the Guide's goal
path: `bh work ready` answering from a real hive is rung 1 of `docs/ADOPTION.md`, and the run
is finished.

## One hive, and one only

Onboarding *every* repo on the machine is a different job — it needs a survey, a per-repo
decision about furnishing, and it fails in ways that have nothing to do with whether the setup
works. This step's job is narrower and more useful: prove the loop once, end to end, on a repo
the user actually cares about.

For the rest, the answer is the same command again. Say so and move on.

## The triplet is required

`bh hive onboard` takes `{PROVIDER/ORG/REPO}` as a required argument. There is no bare form,
and it does not infer the repo from the working directory. Ask for it rather than guessing —
a guessed org is a hive registered under the wrong prefix, and the prefix is baked into every
bead id afterwards.

## Zero-footprint by default

Nothing is committed into the target repo unless a furnishing flag asks for it. That default is
the right one for a first hive: the user is evaluating, and an install that writes files into
their repo before they have decided anything is hard to forgive.

`--opencode` is worth naming out loud here, because it is the answer to the harness question
step 065 deliberately left open: OpenCode is supported through hive furnishing, not through a
Claude plugin. A user on OpenCode who skipped 060 and 065 is not missing anything — this is
where their harness is wired.

## An empty `bh work ready` is a pass

A newly onboarded hive has no beads in it. `bh work ready` will answer with an empty list, and
that is exactly what success looks like: the command found the hive, read its store, and
replied. Say that before showing the output. Otherwise the user's first experience of a working
factory is a blank screen that looks like a failure.

## This is the finish

The step declares `terminates_at: rung-1-reached` — the Guide's 1.0 end state. Tell the user in
those words: **you now have a running factory on rung 1.**

Then stop. Steps 090, 091 and 092 are rung *transitions*, all optional, and none of them is on
the path to a working loop. Offer them only if asked. A user who wanted rung 1 and nothing else
is done, and has not missed anything.
