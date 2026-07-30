# Spike bh-a7so.3 — Can codex satisfy the seat envelope and enforce the same authority boundary?

**Bead:** `bh-a7so.3` · **Seat:** `dev/codex` · **Type:** research-only (no product code)
**Feeds decision on:** Amendment 1 of
[`docs/design/work-runtime-tiers-adr.md`](../design/work-runtime-tiers-adr.md) — specifically the
"provider/model stay runtime" clause, which the amendment itself flags as conditional: "UNLESS
spike 3 finds otherwise" (work-runtime-tiers-adr.md:248-250). This bead is spike 3, matching
Amendment 1's own numbered item 3 under "What is still unvalidated"
(work-runtime-tiers-adr.md:323-327). Also feeds `bh-c6dk.2`.

## Question

Two parts, and answering only the first is not enough to close the bead:

1. Does `codex exec` (its current non-interactive surface) offer a headless-invocation envelope —
   argv, stdin, structured output, session/resume — comparable to what `seat_argv` /
   `run_resolved_seat` already build for `claude -p` in `baml_src/harness.baml`?
2. Can codex's own permission engine express the **same closed `allow`/`ask`/`deny` roster**
   (`ToolRules`, harness.baml:41-48) the bundle carries — the roster `boundary_enforced_by`
   (provider.baml) says the *provider's own engine*, not this harness, enforces — or only a
   weaker or differently-shaped subset?

This is explicitly **not** a request to implement the provider (no product code), and not a
general verdict on whether codex is a good coding agent — only whether its documented CLI surface
can satisfy this harness's specific typed contract (`SeatRun` / `RoleOutcome` / `ToolRules` /
`--resume`) without silently weakening the authority boundary `provider.baml` says the provider
itself enforces.

## Method

No `codex` binary is installed in this environment (`codex not found`, verified locally), and the
bead requires citing real sources rather than recollection, so two passes:

1. **Local, in this repo pair.** Read `baml_src/provider.baml` end to end (the three authority
   axes, `provider_semantics("codex")`, `known_provider_kinds()` / `runnable_provider_kinds()`,
   `require_supported_provider`), `baml_src/harness.baml` end to end (`ToolRules`, `settings_json`,
   `seat_argv`, `ClaudeResult`, `SeatRun`, `run_resolved_seat`), `baml_src/permissions.baml` end to
   end (`project_operations` — the authority projector claude-code actually runs through today,
   not just the `ToolRules` shape it emits), and `baml_src/bundle.baml` (`ResolvedSeat`). Read
   `docs/design/work-runtime-tiers-adr.md` including Amendment 1 in full, and the parent epic
   `bh-a7so`'s design field.
2. **Remote, primary sources only.** Fetched OpenAI's own docs from `learn.chatgpt.com/docs/*.md`
   (the canonical redirect target of `developers.openai.com/codex/*.md` — confirmed with
   `curl -sL -w '%{url_effective}'`; the site serves raw Markdown for every page, per its own
   `llms.txt` index) and the `openai/codex` GitHub repository's Rust source
   (`codex-rs/exec/src/*.rs`, `codex-rs/utils/cli/src/*.rs`, `codex-rs/cli/src/exit_status.rs`) via
   `curl` against `raw.githubusercontent.com`, main branch, fetched 2026-07-30. The Rust source was
   necessary because `learn.chatgpt.com/docs/developer-commands.md?surface=cli` (the CLI flag
   reference) renders its per-command flag tables from a client-side JS `<ConfigTable
   options={execOptions}/>` component whose data is **not present** in the static Markdown dump —
   so an exhaustive flag list could not be read off that page alone; the Rust CLI-parsing structs
   are the ground truth used below instead, and are cited by file:line.

Every claim below cites either a `docs/...` / `baml_src/...` file:line in this repo pair, a
`learn.chatgpt.com/docs/codex/...` doc URL, or an `openai/codex` GitHub file:line.

## Evidence

### 1. The envelope codex has to match

`baml_src/harness.baml` already defines the contract: `ToolRules{ allow, ask, deny,
inherit_user }` (harness.baml:41-48) rendered to a `--settings` JSON payload by `settings_json`
(61-68); `seat_argv` builds `claude`'s argv including `--settings`, `--resume`,
`--session-id`, `--model`, `--add-dir` (185-250); `ClaudeResult{ session_id, total_cost_usd,
... }` is the single-envelope reply (91-98); `SeatRun{ outcome, session_id, cost_usd, usage,
packs }` is the typed result (121-135); and `run_resolved_seat` hardcodes
`baml.sys.exec("claude", seat_argv(...), ...)` (317-331) — there is currently exactly one
provider wired in, not a provider-dispatch table.

### 2. provider.baml's claim about codex, restated as the thing to verify

`provider_semantics("codex")` (provider.baml:100-109): `executes_tools_locally: true`,
`bounded_by`/`boundary_enforced_by`: *"Codex's own sandbox/approval configuration — a different
model from Claude Code's, and NOT expressible in this bundle's rule format"*, `implemented:
false`. `require_supported_provider` panics for codex today (157-176), and
`runnable_provider_kinds()` is asserted to equal exactly `["claude-code"]`
(provider.baml:728). This spike either confirms or refutes the `bounded_by` claim with
specifics — see Evidence 10-11 below.

### 3. `codex exec`'s headless envelope — largely does parallel `claude -p`

Non-interactive invocation is `codex exec "<prompt>"`, or the prompt from stdin (`codex exec -`,
or prompt-plus-piped-stdin-as-context) —
[`learn.chatgpt.com/docs/non-interactive-mode`](https://learn.chatgpt.com/docs/non-interactive-mode)
("You invoke it with `codex exec`"; "If stdin is piped and you also provide a prompt argument,
Codex treats the prompt as the instruction and the piped content as additional context").
Default output prints progress to stderr and only the final message to stdout; `--json` switches
stdout to a JSONL event stream (`thread.started`, `turn.started`, `turn.completed`,
`turn.failed`, `item.*`, `error`) — same doc, "Make output machine-readable". The exact schema is
defined in Rust and is authoritative: `ThreadEvent` enum, `codex-rs/exec/src/exec_events.rs:11-37`.
`--output-schema <file>` plus `-o`/`--output-last-message <path>` can force the final agent
message to conform to an arbitrary JSON Schema and write it to a file (same doc, "Create
structured outputs with a schema") — a genuine, arguably *better* analog than Claude's own
chatty-reply recovery (`SeatTurn$parse` in harness.baml:75-88) for getting a `RoleOutcome`-shaped
reply out of codex.

### 4. Flags that already parallel `seat_argv`

`--add-dir <dir>`, `--cd`/`-C <dir>`, `--model`/`-m`, `--profile`/`-p`, `--sandbox`/`-s`, and
`--dangerously-bypass-approvals-and-sandbox` (alias `--yolo`) are real, current flags shared
between the interactive CLI and `codex exec` —
`codex-rs/utils/cli/src/shared_options.rs:8-63` (`SharedCliOptions`), flattened into `exec`'s own
`Cli` at `codex-rs/exec/src/cli.rs:23-24`. `--add-dir` in particular is a direct structural
analog to `seat_argv`'s own `--add-dir` handling for claude-code (harness.baml:196-203).

### 5. `thread_id` is the resume-token analog

`thread.started` is the first JSONL event and carries `thread_id`:
`ThreadStartedEvent{ thread_id: String }`, doc comment *"Can be used to resume the thread
later"* — `codex-rs/exec/src/exec_events.rs:39-43`. Functionally this is `SeatRun.session_id`'s
analog.

### 6. Resume is a subcommand, not a flag

Resume is a **subcommand**, not a flag: `codex exec resume --last "<prompt>"` or
`codex exec resume <SESSION_ID> "<prompt>"` —
[`learn.chatgpt.com/docs/non-interactive-mode`](https://learn.chatgpt.com/docs/non-interactive-mode)
("Resume a non-interactive session"). The argv shape is literally
`codex exec [OPTIONS] <COMMAND> [ARGS]` with `Command::Resume(ResumeArgs)`
(`codex-rs/exec/src/cli.rs:12, 143-150`) — structurally different from Claude's `--resume <id>`,
which is one more flag on the *same* base invocation (harness.baml:233-244, `seat_argv`). A
codex `seat_argv` equivalent would branch into two different subcommand shapes rather than add
one flag.

### 7. Resume needs persisted rollout files

Resume requires the run to **not** pass `--ephemeral` ("Use `--ephemeral` when you don't want to
persist session rollout files to disk" — same doc) — an ephemeral run has nothing to resume.

### 8. Exit codes are binary only, no taxonomy

Every failure path in the `exec` crate ends in `std::process::exit(1)` — 13 call sites in
`codex-rs/exec/src/lib.rs` (e.g. lines 299, 317, 472, 491, 668, 789, 1058, 1809, 1820, 1906,
1921, 1928, 1937), including the main completion path: `if error_seen { std::process::exit(1); }
Ok(())` (lib.rs:1057-1061). `error_seen` is set on an unretried server error, or a completed turn
whose status is `Failed`/`Interrupted` (lib.rs:996-1013) — collapsing everything short of clean
success into the same `exit(1)`. There is **no** taxonomy comparable to Decision 4 / Amendment
1's `0`/`10`/`11`. This is not a *new* gap the codex provider would introduce, though: the
harness already reads `envelope.is_error` from the parsed stdout payload rather than depending on
`claude`'s own process exit code (harness.baml:333-338) — the same approach (parse
`turn.completed` / `turn.failed` / `error` off the JSONL stream) would carry over to codex.

### 9. Cost is absent from the event schema

`TurnCompletedEvent{ usage: Usage }`, where `Usage = { input_tokens, cached_input_tokens,
cache_write_input_tokens, output_tokens, reasoning_output_tokens }` —
`codex-rs/exec/src/exec_events.rs:49-73`. **No `cost_usd` / `total_cost_usd` field exists
anywhere in the event schema.** `SeatRun.cost_usd` (harness.baml:124) currently just reads
`envelope.total_cost_usd ?? 0.0` for claude-code (harness.baml:273); a codex provider would need
to *compute* a dollar cost from token counts against a maintained per-model price table — a new,
provider-specific responsibility claude-code does not require.

### 10. Permissions — the central finding: no single roster mechanism exists

Codex has **no** mechanism equivalent to `ToolRules{allow,ask,deny}` sent as one payload. It
splits authority across (at least) four separately-vocabularied, mostly config-file-based
mechanisms:

- **(a) Sandbox mode / permission profiles** — `sandbox_mode`: `read-only` | `workspace-write` |
  `danger-full-access`, or the BETA `default_permissions` / `[permissions.<name>]` profiles with
  per-path `read`/`write`/`deny` and per-domain `allow`/`deny` — a coarse, **global**
  technical-capability toggle (what the process *can* touch), never a per-tool-pattern roster,
  and with **no third "ask" outcome anywhere in its own vocabulary** (fs: `read`/`write`/`deny`;
  net: `allow`/`deny`) —
  [`learn.chatgpt.com/docs/agent-approvals-security`](https://learn.chatgpt.com/docs/agent-approvals-security)
  ("Sandbox and approvals"),
  [`learn.chatgpt.com/docs/permissions`](https://learn.chatgpt.com/docs/permissions)
  ("Configuration spec"; mirrored in `config-reference.md` under `default_permissions` /
  `permissions.<name>.*`).
- **(b) Approval policy** — `approval_policy`: `untrusted` | `on-request` | `never`, or
  `{ granular = { sandbox_approval, rules, mcp_elicitations, request_permissions,
  skill_approval } }` — a **separate**, global-or-5-category knob for *when* to pause, layered
  on top of (a), not a per-command-pattern roster either
  ([`learn.chatgpt.com/docs/config-file/config-reference`](https://learn.chatgpt.com/docs/config-file/config-reference)).
  In fully non-interactive `codex exec`, an "ask" has nobody to answer it unless routed to
  `approvals_reviewer = "auto_review"` — an **LLM reviewer agent** standing in for the
  operator, a categorically different enforcement actor than a human-authored closed roster
  (`learn.chatgpt.com/docs/agent-approvals-security`, "Automatic approval reviews").
- **(c) `execpolicy` prefix rules** — `.rules` files, Starlark
  `prefix_rule(pattern=[...], decision="allow"|"prompt"|"forbidden")`. This is the *closest*
  three-way analog to `allow`/`ask`/`deny` (three outcomes, same shape of decision), but it is
  (i) **experimental** — "Rules are experimental and may change"
  ([`learn.chatgpt.com/docs/agent-configuration/rules`](https://learn.chatgpt.com/docs/agent-configuration/rules)),
  (ii) scoped **only to shell-command prefixes** matched as argv token lists — it says nothing
  about file edits, MCP tool calls, or any non-shell action, and (iii) is loaded from **files at
  startup** (`~/.codex/rules/*.rules`, or a trusted project `<repo>/.codex/rules/`) rather than
  carried inline in one CLI flag the way `--settings <json>` is ("Project-local rules under
  `<repo>/.codex/rules/` load only when the project `.codex/` layer is trusted" — same doc).
- **(d) Per-MCP-tool approval** — `mcp_servers.<id>.tools.<tool>.approval_mode`: `auto` |
  `prompt` | `writes` | `approve`, plus separate `enabled_tools`/`disabled_tools` allow/deny
  lists — yet another vocabulary, specific to MCP servers only
  (`config-reference.md`, `mcp_servers.<id>.*` keys).

None of (a)-(d) is a single, portable, per-tool-pattern `allow`/`ask`/`deny` roster the way
`ToolRules` is; they are four separately-vocabularied mechanisms, three of them file/config-layer
based rather than argv-carried, split by **action type** (shell vs. filesystem/network vs. MCP)
rather than unified. This is exactly what `provider.baml`'s own `bounded_by` text already
asserted (provider.baml:105-106) — now with specifics.

### 11. The harness's current authority pipeline shows this is not a reformatting problem

`baml_src/permissions.baml` is not `ToolRules` in isolation — it is
`project_operations(ops: HitchOperation[]) -> ToolRules` (permissions.baml:104-154), a function
that projects agent-hitch's *neutral* `HitchOperation{ tool, executable, arguments, decision }`
model into Claude's `Bash(...)` pattern strings. It is explicitly, narrowly scoped:
`projectable_tool() -> "bash"` (permissions.baml:46-48), and it **panics rather than silently
drops** an operation declared on any other tool (permissions.baml:110-120, "will not drop one
silently") — i.e. even today's claude-code path does not generically project every action type;
it treats bash-only scope as a hard boundary, not an oversight. The file's own header states the
general principle this spike's finding is one instance of: *"That translation is lossy in
exactly the dimension enforcement cares about, which is why the seam hands over operations and
leaves the projection to the consumer... only the consumer knows which loop will enforce the
result"* (permissions.baml:5-8), and separately distinguishes a DELEGATED loop (Claude Code runs
the tool loop; an ambient catch-all `ask` is dropped because the CLI already behaves that way)
from a SOVEREIGN one (permissions.baml:15-36). Codex is *also* `executes_tools_locally: true`
(provider.baml:103) — i.e. also "delegated" in this framework's terms — but its target shape
(Evidence 10a-d) looks nothing like `Bash(...)` allow/ask/deny, so a codex provider needs its
**own** `project_operations`-equivalent, not a reuse of the existing one: translating
`HitchOperation[]` into some combination of an `execpolicy` `.rules` payload (shell prefixes), a
`[permissions.<name>]` / `sandbox_mode` block (fs/network), and `mcp_servers.*.tools.*.approval_mode`
(MCP) — three targets instead of one.

### 12. Amendment 1 pre-committed to being tested by exactly this finding

*"`--provider`/`--model` stay runtime because the roles matrix already has the dispatcher
overriding model per bead — **UNLESS spike 3 finds otherwise** (see below)"*
(`docs/design/work-runtime-tiers-adr.md:248-250`). Its own "What is still unvalidated" list, item
3, names the exact test: *"if codex cannot express the same allow/ask/deny roster, a runtime
`--provider` switch would silently weaken a baked boundary and **`--provider` must bake too**"*
(`work-runtime-tiers-adr.md:323-327`). Evidence 10-11 above is that "otherwise."

## Verdict — **NO-GO**

NO-GO on the proposed contract's assumption that `--provider` stays a runtime flag — not a verdict
that codex is unbuildable in general (see Recommendation).

Codex's permission engine cannot express the same `allow`/`ask`/`deny` roster `ToolRules` carries
(Evidence 10-11): it splits authority across `sandbox_mode`/permission-profiles (allow/deny only for
filesystem and network, no `ask` outcome at all), `approval_policy` (a global-or-5-category knob for
*when* to pause, not a per-tool-pattern roster, and answered by an LLM reviewer rather than the
operator's own roster when routed to `auto_review`), experimental shell-prefix-only `execpolicy`
rules (the closest 3-way match, but file-staged and scoped to shell commands only), and a fourth,
separate MCP-specific approval vocabulary. Today's claude-code path narrowly and deliberately
projects one neutral operation model (`HitchOperation[]`) into exactly one target shape
(`Bash(...)` patterns); codex would need an entirely different, three-target projector, not a
reformat of the existing one.

A runtime `--provider claude-code` → `--provider codex` switch on an otherwise-unchanged baked
bundle would therefore silently swap which of these differently-shaped, differently-enforced
mechanisms interprets the operator's reviewed roster — in the worst case falling back to whatever
ambient `sandbox_mode`/`approval_policy`/`~/.codex/config.toml` the runtime environment happens to
supply, rather than the audited roster the binary was built and reviewed against. That is precisely
the silent-weakening scenario `boundary_enforced_by` (provider.baml) warns about. `--provider` is
therefore an **authority argument** and must bake into the seat binary alongside `permissions` /
`permission_mode` / `mcp_config` / `plugin_dirs`, contradicting Amendment 1's "provider/model stay
runtime" clause — which Amendment 1 itself flagged as conditional on this exact spike (Evidence 12).

`--model`/`--tier` are unaffected by this finding and can stay runtime as proposed: nothing in the
evidence above touches model selection's authority properties, only the tool/action-permission axis,
which is where codex diverges from claude-code.

## Recommendation

1. **Amend the contract before scoping the codex build.** File a follow-up decision (Amendment 2, or
   a new section of the ADR) moving `--provider` into the baked set: a seat binary is compiled for
   exactly one provider, and switching providers means rebuilding — the same discipline already
   applied to the permission roster. `--model`/`--tier` stay runtime, unaffected by this finding.
2. **Do not flip `provider_semantics("codex").implemented` to `true` until (1) lands.** Doing so
   today would either silently under-enforce (falling back to codex's own ambient config when the
   baked `ToolRules` cannot be translated) or require inventing a translation ad hoc, which is the
   exact failure mode `require_supported_provider`'s default-closed stance exists to prevent
   (provider.baml:153-176).
3. **Once (1) is decided, size the actual build** — this is a real implementation, not a
   `implemented: true` flip plus a `match` arm. It touches, at minimum:
   - A `codex_seat_argv`-equivalent to `seat_argv` (harness.baml:185-250), structurally branched
     (fresh run vs. the `codex exec resume <id>` subcommand — Evidence 6) rather than one
     flag-additive function.
   - A JSONL stream consumer (parse `thread.started` → `session_id`, drain to `turn.completed` /
     `turn.failed` / `error`) replacing the single-envelope `ClaudeResult` parse
     (harness.baml:91-98, 263-277), mirroring `exec_events.rs`'s `ThreadEvent` schema (Evidence
     3, 8).
   - A `cost_usd` computation from `Usage` token counts against a maintained per-model price table,
     since codex emits no dollar figure (Evidence 9) — new bookkeeping the claude-code path does not
     need.
   - A **new authority projector**, parallel to but structurally different from
     `permissions.baml`'s `project_operations` (Evidence 11): translating `HitchOperation[]` into
     codex's fragmented shape, plus a decision on *where* the projected config lives at build time —
     most likely a generated `$CODEX_HOME`-equivalent directory (or a `--profile <seat>.config.toml`
     paired with a pointed `CODEX_HOME`) baked alongside the binary, since `execpolicy` rules and
     permission profiles are file-loaded, not argv-carried (Evidence 10c;
     `config-reference.md`: project-scoped config loads only when trusted, mirroring the
     `.rules` trust gate).
   - A `tier_model_table("codex", ...)` arm with real `gpt-5.x-codex` model ids — a separate, small
     research task (model catalogue only), not covered by this spike.
   - A test suite mirroring harness.baml's ~15 claude-code argv/parse tests, sized for the above,
     plus new tests asserting the codex authority projector fails closed (refuses rather than
     silently drops) for any `HitchOperation` it cannot faithfully express — matching
     `project_operations`'s own behavior today (Evidence 11).
4. **What stays valid regardless:** codex's headless-invocation envelope (argv, stdin, `--json`
   streaming, `--output-schema`, `--add-dir`) and its resume story (`thread_id` /
   `codex exec resume`) are workable analogs to claude-code's (Evidence 3-7) — the size and risk here
   is entirely in the authority axis, not in the invocation envelope.
