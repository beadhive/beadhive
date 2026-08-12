# Claude Code `--permission-mode` — vocabulary snapshot

> **Status:** reference, not a decision. This records an **external contract** that Anthropic owns
> and can change. Everything below is a snapshot; re-verify before relying on it.
>
> | | |
> | --- | --- |
> | **Pulled from** | `claude --version` → **2.1.228 (Claude Code)** |
> | **Date** | 2026-08-11 |
> | **Sources** | the installed binary's `claude --help`, and <https://code.claude.com/docs/en/permissions> |
> | **Why it exists** | `baml-harness/baml_src/harness.baml:457` passes this value straight to the CLI: `["-p", prompt, "--output-format", "json", "--permission-mode", seat.permission_mode]`. A wrong value is a runtime failure at spawn, and a *plausible but wrong* value is worse — see the `manual` trap below. |
> | **Related** | `work-runtime-tiers-adr.md` Amendment 2 (the seat contract), beads `bh-baml-6gv`, `bh-c6dk.13`, `bh-c6dk.14` |

**Re-verify with one command.** The authoritative list is the binary's own, not this file:

```bash
claude --help | grep -A3 permission-mode
```

## The accepted values

`claude --help` at 2.1.228 reports exactly:

```text
--permission-mode <mode>   (choices: "acceptEdits", "auto", "bypassPermissions",
                            "manual", "dontAsk", "plan")
```

| Mode | Behavior | Unattended? |
| --- | --- | --- |
| `manual` | Alias for the mode the docs call `default`: **prompts on first use of each tool**. The CLI labels it *Manual*; the alias requires v2.1.200+. | **No** — stalls, nobody to answer |
| `plan` | Reads files and runs read-only shell commands. Does **not** edit source files. | Read-only seats only |
| `acceptEdits` | Auto-accepts file edits and common filesystem commands (`mkdir`, `touch`, `mv`, `cp`) for paths in the working directory or `additionalDirectories`. Other tools still prompt. | Partially — non-edit tools can still block |
| `auto` | Auto-approves tool calls with **background safety checks** that verify actions align with the request. | **Yes** — built for this |
| `dontAsk` | Auto-**denies** tools unless pre-approved via `/permissions` or `permissions.allow`. `AskUserQuestion` and tools marked `requiresUserInteraction` are denied even if allowed. | **Yes** — roster is authoritative |
| `bypassPermissions` | Skips prompts except explicit `ask` rules. Docs restrict it to isolated environments (containers/VMs). Circuit-breaks on `rm -rf /` and `rm -rf ~`. | Yes, but only in a sandbox |

`default` is the docs' canonical name for `manual`. **It is not in the CLI's `choices` list** — pass
`manual`.

## Three things that bite

**1. `manual` is not "manually configured", it is "ask a human every time."** It reads like a safe,
neutral default and is the single worst choice for a dispatched seat: it prompts, and an unattended
seat has nobody to answer. `baml-harness/baml_src/bundle.baml`'s documented example and several of
its tests use `permission_mode: "manual"` — those predate headless dispatch and would hang a real
unattended run. Tracked on `bh-baml-6gv`.

**2. `plan` is what read-only means here.** `baml-harness/baml_src/capabilities.baml:49` states it:
*"`plan` is read-only regardless of the roster."* Beadhive's **reviewer** ("Read-only re: code —
does NOT implement or merge") and **warden** ("Read-and-block only: no Edit/Write, no merge, no
dispatch") depend on this. Any change to the default must keep those roles resolving to `plan` —
that is the safety criterion on `bh-baml-6gv`, not a nicety.

**3. `auto` can be switched off underneath you.** Managed settings support
`permissions.disableAutoMode` and `permissions.disableBypassPermissionsMode`. A host or org policy
can therefore forbid the mode a seat was built to run in. A roster-based `dontAsk` has no such
dependency, since it relies only on `permissions.allow`.

## `auto` vs `dontAsk` for a governed seat

Both avoid prompting, which is the hard requirement. They differ in *what decides*:

- **`auto`** — a background model-side check judges each call against the request. Simpler, adapts
  to work the roster did not anticipate, but it is a model judgement: it costs something, it can
  refuse, and it makes a run less deterministic.
- **`dontAsk`** — the static `permissions.allow` roster decides, and hitch already composes exactly
  that roster through `project_operations`. Fully deterministic, no model in the loop, and the
  profile becomes the single source of authority. The cost is that anything the roster did not
  anticipate is denied rather than reasoned about.

For Beadhive the operator's chosen default is **`auto`** (2026-08-11). `dontAsk` is recorded on
`bh-baml-6gv` as the alternative worth revisiting once real dispatch runs show how often a seat
needs something outside its composed roster.

## Staleness

This vocabulary has already moved once within this project's lifetime: baml-harness's code was
written against a set that did not include `auto` or `dontAsk`, which is how the wrong claim
"`auto` is not valid" entered bead `bh-baml-6gv` before being corrected the same day. Treat any
mode list older than the installed binary as suspect, and prefer `claude --help` over this file
whenever the two disagree.
