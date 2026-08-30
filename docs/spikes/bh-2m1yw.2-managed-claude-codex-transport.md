# bh-2m1yw.2 — Managed Claude and Codex transport

## Question

Can the launcher start an external Claude Code or Codex process with an exact Beadhive
developer, dispatcher, or beadless planner seat, model, effort, worktree, and observable launch
receipt? Separately, can it intercept native Claude Task or Codex collaboration children with the
same guarantees?

## Method

The probe used the prerequisite core contract at merge `91785683` and the installed harnesses:

```text
$ claude --version
2.1.251 (Claude Code)
$ codex --version
codex-cli 0.147.0
```

It combined four kinds of evidence:

1. Resolve the six core profiles (three seats times two harnesses) with
   `resolve_agent_launch_profile`, and inspect their allowlisted argv and receipt.
2. Inspect `bh role --help`, `claude --help`, and `codex exec --help` to compare those argv with
   the installed transports.
3. Run each installed harness hermetically from `/tmp`, with read-only/no-persistence settings and
   a synthetic receipt. The prompt could only print `BH_AGENT_LAUNCH_RECEIPT`; it could not inspect
   repository files. (The Claude developer/dispatcher follow-up probes were denied by the host
   egress policy, so this document does not turn the successful planner transport into evidence
   for those seats.)
4. Compare the native child-spawn APIs exposed to this session with the acceptance threshold: a
   callable API must carry cwd, environment/receipt, exact identity/seat, and an observable
   lifecycle handle.

No product source was changed and no probe claimed, assigned, submitted, or merged a synthetic
bead.

## Evidence

### Core profile matrix

The resolver enforces the intended bead policy before constructing argv:

| Harness | Seat | Binding accepted | Resolved argv | Exact seat carried by argv? |
| --- | --- | --- | --- | --- |
| Claude | developer | managed `bh-probe1` | `claude --agent developer --model sonnet --effort low` | Yes |
| Claude | dispatcher | managed `bh-probe1` | `claude --agent dispatcher --model sonnet --effort low` | Yes |
| Claude | planner | beadless | `claude --agent planner --model sonnet --effort low` | Yes |
| Codex | developer | managed `bh-probe1` | `codex --model gpt-5.4 --config model_reasoning_effort=\"low\"` | **No** |
| Codex | dispatcher | managed `bh-probe1` | same | **No** |
| Codex | planner | beadless | same | **No** |

For Claude, `role._profile_harness_argv` scopes the agent to the installed plugin
(`bh:developer`, `bh:dispatcher`, or `bh:planner`) unless an explicit local override exists.
`--agent` is an installed Claude option, so the external process has a provider-native seat
selection mechanism. The model and effort are also provider-native flags.

For Codex, the current adapter deliberately emits no seat-bearing argument, developer
instructions, profile, or equivalent. `BH_ROLE` is attribution/status context, not agent
instructions, and the receipt is evidence, not authority. Therefore all three Codex exact-seat
rows are rejected even though model and effort transport correctly.

The same profile resolution fixes binding and workspace independently of harness argv:
developer/dispatcher require `managed_bead=true` plus an exact bead, while planner accepts the
beadless form. `bh role --bead` attaches the bead worktree and uses it as cwd; `--hive` selects a
beadless hive root. The child process is external and launcher-owned in either case.

### Receipt observation

`role.launch` scrubs any inherited parent receipt, serializes
`AgentLaunchReceipt.from_resolved(...)`, and supplies the new JSON as
`BH_AGENT_LAUNCH_RECEIPT` in the child environment.

The real Claude planner probe used `--agent bh:planner --model sonnet --effort low`, plan mode,
no session persistence, and `/tmp`. Claude ran `printenv BH_AGENT_LAUNCH_RECEIPT` and returned the
synthetic receipt unchanged, including `current_seat=planner`, `managed_bead=false`, harness,
model, and effort. It completed successfully (session
`e66f642a-7bb2-4b94-99eb-08140272aec2`). This proves observation for the external Claude
transport and exact beadless planner loading; it does not prove developer/dispatcher loading
because those real follow-up calls were blocked by the execution host's data-egress policy.

The real Codex probe used `codex exec --skip-git-repo-check --sandbox read-only`, `/tmp`, model
`gpt-5.4`, and reasoning effort `low`. Its startup report confirmed workdir, model, read-only
sandbox, and effort. Its exact `printenv` command returned the synthetic planner receipt unchanged
(session `01a050f1-82df-7c33-a58f-b15952a2ee88`). Thus Codex can observe the receipt, but receipt
observation does not load the planner seat. The same environment channel is seat-agnostic, so it
can carry developer and dispatcher receipts too; without a Codex instruction/profile mechanism,
those values remain evidence only.

### External process versus native child

The managed route owns an OS process: it chooses cwd and environment before spawn, retains a
process/session handle, captures stdout, classifies the seat result, and can cancel/reap the
process group. That is the lifecycle boundary on which a receipt is meaningful.

Native Claude Task and Codex collaboration children are different. The callable Codex
collaboration spawn surface accepts task text, context forking, model, and reasoning effort; it
does not accept cwd, environment, Beadhive identity, or a launcher-owned process handle. No
callable Claude Task bridge exposed here supplies all four either. Provider UI/transcript
attribution, inherited `BH_ROLE`, or the fact that some internal process eventually starts does
not fill those missing controls. Consequently native interception is **unsupported**, separately
from the external-process result.

### Acceptance accounting

- Required document structure: present.
- Claude developer: core/argv supports exact loading; real external invocation not completed due
  host egress policy, so empirical proof is rejected rather than inferred.
- Claude dispatcher: same rejected empirical status.
- Claude beadless planner: demonstrated by real installed harness.
- Codex developer, dispatcher, beadless planner: exact seat loading rejected; model, effort, cwd,
  and receipt transport work, but there is no seat-bearing Codex argv/instruction transport.
- Intended receipt observed: demonstrated independently in both real external harnesses.
- Native Task/collaboration bridge: unsupported, evaluated separately.
- Supported managed-fanout route: launcher-owned external processes only.

## Verdict

**NO-GO.** The shared profile and receipt are sufficient transport primitives, and external
Claude has a plausible provider-native exact-seat route, but the acceptance bar is conjunctive.
Codex currently receives no exact seat instructions, real Claude developer/dispatcher probes are
not complete, and neither native child API exposes a supported interception bridge.

This verdict does not claim that native children cannot ever be integrated. It says the required
bridge is not exposed by the installed, callable APIs tested here.

## Recommendation

Use launcher-owned external processes as the only supported managed-fanout route. Keep native
Claude Task and Codex collaboration fanout explicitly unmanaged until their public spawn APIs
carry cwd, environment/receipt, exact identity, and a lifecycle handle.

Before changing the verdict:

1. Add a provider-native Codex seat-instruction transport (for example an allowlisted generated
   instructions/profile file) and prove that the child reports the baked developer, dispatcher,
   and planner duties; do not use `BH_ROLE` or receipt contents as instructions.
2. Re-run Claude developer and dispatcher from a disclosure-approved hermetic fixture, verifying
   the scoped `--agent` load and exact receipt without exposing repository content.
3. Preserve the receipt as redacted evidence and continue scrubbing inherited receipts at every
   unmanaged/native boundary.
