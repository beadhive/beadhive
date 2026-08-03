# Spike `bh-lx6e.2` — One seat interface for terminal, orca, qm, OpenHands, and openchamber?

**Bead:** `bh-lx6e.2` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-lx6e.5` (DECISION: operator picks the front-end direction from the
feasibility profiles) — and gates `bh-lx6e.4` (SPIKE: prove the front-end seam against orca).

> Framing amendment (dispatch, 2026-08-03): this bead originally carried a 70/30 scored
> keystroke-vs-bead-decision audit. That threshold is **withdrawn by operator direction** — see
> Notes on `bh-lx6e.2`. What replaces it is exactly this GO/NO-GO question, decided from
> comparable feasibility profiles the operator reads and chooses from manually.

## Question

Can bh expose **one interface** — or a small, named set — that at least the plain terminal
attach, orca, and qm can all drive to reach a conversational bh seat, such that adding each
subsequent front end is an integration, not a re-architecture? This is explicitly **not** asking
which front end is best, and **not** asking for a scored comparison — the operator picks
direction manually from the profiles this spike produces (GO/NO-GO withdrawal, `bh-lx6e.2` notes,
2026-08-03).

## Method

Source-reading of five candidates against one fixed five-field profile (interface required / owns
the agent loop? / permission-approval mechanism / auth model + remote-only composability / what bh
must expose), plus one structural check of the sibling qm-probe epic to avoid duplicating a live
qm probe. No product code; no live deployment stood up here (see Evidence §0 for why).

1. **In-repo sources** — `src/beadhive/role.py` (bh's current seat launcher), `src/beadhive/orca.py`
   (bh's existing orca plugin), `docs/design/ade-client-interfaces.md` (prior Orca-vs-OpenHands
   research), `docs/MCP.md`, bead text for `bh-lx6e`, `bh-lx6e.1`, `bh-lrcw` + its six children,
   `bh-xls2.4`/`bh-xls2.5` (planned ACP adapter), and the seven `tag:idea` beads describing
   assumed qm architecture (`bh-nc1q`, `bh-enq4`, `bh-5waa`, `bh-b210`, `bh-zu7l`, `bh-tvzl`,
   `bh-yc79`).
2. **Live source-reading of each candidate's own public repo/docs**, fetched 2026-08-03 via
   `curl`/GitHub's REST API from this worktree (network egress confirmed available) — READMEs,
   security docs, and in qm's and OpenHands' case actual TypeScript/Python source files, not just
   marketing copy. Citations below give exact file paths and, for GitHub raw content, the fetched
   URL.
3. **The Agent Client Protocol (ACP) specification itself** (`agentclientprotocol/agent-client-protocol`
   on GitHub — the project moved out of `zed-industries` at some point after the in-repo
   `ade-client-interfaces.md` doc was written; confirmed via GitHub search, 2026-08-03), read at
   the schema and transport-doc level, since it is the one candidate-spanning protocol (OpenHands
   offers it; a planned first-party front end, `bh-xls2.4`/`bh-xls2.5`, targets it) with a
   precisely specified permission-request method.
4. **A check of whether bh-lrcw's live qm probe has produced anything to consume** (`bh bd show
   bh-lrcw` and its six children, `ls docs/spikes/`) — per this bead's own Design section
   ("consume its evidence rather than duplicating it").
5. **A check of what session evidence already exists** for the interaction-type-distribution
   clause (`bh doctor`'s Observability block; `~/.claude/projects/<this-repo>/*.jsonl` transcript
   inventory) — per the notes' amended clause, evidence-derivation only, no live-session capture
   attempted.

No live deployment of qm, OpenHands, or openchamber was stood up. Orca was not re-probed live
either — `docs/design/ade-client-interfaces.md` already did that work (2026-07-07) and is
consumed here, refreshed against orca's current public docs where the profile needed a fact that
doc didn't cover (its own permission-approval mechanics specifically).

## Evidence

### 0. bh-lrcw has not produced anything to consume — a named gap in this bead's own premise

This bead's Design section states "bh-lrcw is ALREADY standing up a real qm deployment... Consume
its evidence rather than duplicating it." That premise does not hold at the time this spike ran:

1. `bh bd show bh-lrcw` — epic status **OPEN**, label `kickoff:pending`, 1/6 children complete
   (the one complete child is `bh-lrcw.6`, a bookkeeping "state change: kickoff → pending" bead,
   not a probe).
2. All four real probe beads (`bh-lrcw.1` deployment anatomy, `.2` CANCEL contract, `.3`
   posture, `.4` identity/scheduling) are **OPEN**, unclaimed, unstarted.
3. `bh-lrcw.1` — the prerequisite every other probe bead depends on — is itself blocked on an
   unresolved `Gate: human` (`bh-hvkx`, description: "Ad-hoc gate blocking bh-lrcw.1 ... Reason:
   kickoff bh-lrcw"), also OPEN.
4. `ls docs/spikes/` (checked 2026-08-03) contains no `bh-lrcw*` file of any kind.

So there is no live-probe evidence to consume. Given that, this spike did its own **source-reading**
of qm's own public repository — not a live deployment probe, not a re-run of bh-lrcw's mandate
(standing up qm locally, testing the CANCEL ladder against process signals, testing posture
mutability mid-flight) — to answer the five architecture-level fields this bead's own Design
section asks for. That is squarely inside this bead's own Method line ("source-reading plus a live
probe where a candidate can be stood up cheaply") and does not duplicate bh-lrcw's live-deployment
mandate. This distinction — and the fact that bh-lrcw's premised evidence didn't exist — is itself
a finding `bh-lrcw.5` and any dispatcher scheduling bh-lrcw's children should know about.

### 1. Terminal attach (the baseline)

1. `src/beadhive/role.py:180-224` (`launch()`) — `bh role <seat>` resolves an argv
   (`["claude", "--agent", "bh:<seat>"]` or the `opencode` equivalent per
   `KNOWN_HARNESSES = ("claude", "opencode")`, line 113) and calls
   `run(argv, check=False, capture=False, env=env)` (line 223) — **inherited stdio**, then
   `raise SystemExit(result.returncode)` (line 224). This is a foreground exec with no session
   name, no detach, and no handle anything else can grab — quoted verbatim in `bh-lx6e`'s own
   epic description as the finding that motivated the whole molecule.
2. There is no separate approval channel: whatever the harness's own interactive TUI does for a
   tool-permission prompt happens directly in the inherited TTY.
3. Auth is whatever authenticates the shell (SSH keys / host login) plus the harness's own
   locally-stored credential (Claude Code's OAuth token, an API key, etc.) — nothing bh-specific.

### 2. Orca

Primary source: `docs/design/ade-client-interfaces.md` (fetched 2026-07-07, already in-repo),
refreshed 2026-08-03 against `onorca.dev/docs` live pages for the two facts that doc didn't cover.

1. **Interface required** — a PTY. `ade-client-interfaces.md` §3.1: "25+ pre-configured harnesses
   ... PTY-spawned." Confirmed directly on orca's own docs (`onorca.dev/docs/agents/claude-code`,
   fetched 2026-08-03): *"In any worktree, open a terminal and pick Claude Code from the agent
   combobox. Orca launches it with the worktree as the working directory..."* — Orca spawns the
   exact same kind of process `bh role` does, into a terminal pane it manages.
2. **Owns the agent loop?** No. Orca is "the seat-runtime tier" (`ade-client-interfaces.md` §2's
   layer diagram) — it spawns and supervises a complete, self-contained harness CLI session; the
   harness (Claude Code, Codex, ...) owns 100% of its own tool loop. Orca adds session
   management (multiplexing, hibernation, mobile reattach) around that PTY, nothing more.
3. **Permission approvals** — identical mechanism to the terminal baseline: the harness's own
   inline TTY prompt, rendered verbatim inside Orca's terminal pane. Orca does not intercept,
   replace, or structure it. Orca layers a "waiting on input" state and a mobile push notification
   on top (`ade-client-interfaces.md` §3.6: *"reply when an agent is waiting on input"*), but the
   approval itself is still the harness's raw prompt, now visible/answerable from a phone.
4. **Auth model** — local use: "Orca picks up `~/.claude` automatically — no extra config needed"
   (`onorca.dev/docs/agents/claude-code`) — i.e. it piggybacks entirely on the harness's own
   host-local credential file. Remote/mobile pairing: a **private network path**, not a public
   auth flow — `onorca.dev/docs/remote-servers` (fetched 2026-08-03): *"Keep the server and client
   on a private network path you control, such as the same Tailscale tailnet or LAN"*, matching
   `ade-client-interfaces.md` §3.5's `--pairing-address` note (Tailscale/LAN/SSH-forward, not
   localhost). It composes with remote-only, but only via a network overlay bh does not control —
   not via any bh- or Orca-issued credential.
5. **What bh must expose** — nothing new for the drive-interface itself (same PTY as terminal);
   bh already ships the registration half of this integration (`src/beadhive/orca.py` — `orca repo
   add`, the `wt_create`/`wt_remove` delegation hooks, scoped deliberately to orca's `repos` list
   only per the module's own scope-invariant docstring, lines 1-27). The one missing piece is
   shared with the terminal baseline, not Orca-specific: a durable, detachable session
   (`bh-lx6e.1`, in progress — see §6).

### 3. qm

Primary sources: `github.com/yc-software/qm` — `README.md`, `SECURITY.md`,
`src/harness/harness.ts`, `src/harness/claude-harness.ts` (all fetched 2026-08-03 via
`raw.githubusercontent.com`; file listing via GitHub's contents API). Identity matches the URL
already named in `bh-lrcw`'s own epic description ("github.com/yc-software/qm, 'a multiplayer
agent harness for work'") — now independently confirmed against the actual repository, not just
the bead text.

1. **Interface required** — **none, in the sense the other four candidates need one.** qm's own
   architecture diagram (`README.md`, "Architecture" section): a headless core (`API · identity ·
   policy · scheduler`) containing an `Agent loop (Pi, OpenCode, Claude Code)`, backed by Postgres,
   driving a per-scope sandbox. `src/harness/claude-harness.ts:1-17` confirms this concretely: qm
   embeds `@anthropic-ai/claude-agent-sdk` (`query`, `tool`, `createSdkMcpServer`, `SDKMessage`) —
   the **programmatic Claude Agent SDK**, called as an in-process library from qm's own Node
   process — not a PTY, not ACP, not any wire protocol bh would need to speak. qm's harness
   abstraction (`src/harness/harness.ts:1-100`, the `HarnessTurnController.runTurn()` interface)
   is qm's OWN internal turn-taking contract that each pluggable harness SDK implements; it is not
   something external to qm.
2. **Owns the agent loop? — genuinely partial, and this complicates the bead's own initial framing.**
   The bead's Requirements section states qm "is positioned... as the human-facing surface over
   someone else's engine" — implying it does *not* own the loop, the same as Orca/terminal. Reading
   qm's actual source contradicts a clean "no": `src/harness/harness.ts` shows qm's core defines
   its **own** cross-harness tool surface (`pi-tools.ts`'s `execute, read, write, publish, memory,
   history, background` — the same fixed tool set regardless of which harness SDK is underneath,
   per `CHILD_TOOL_NAMES`), owns session/turn sequencing, screens content
   (`screenExternalContent`), and gates tool calls (`toolApprovalGate`) — all *before* the pluggable
   harness SDK is invoked. What qm delegates to the harness SDK is narrower than "the agent loop":
   it's per-turn model invocation constrained to qm's own tool surface, not a self-contained CLI
   session running the harness's native tools. That sits meaningfully closer to "owns it" than
   Orca or the terminal baseline (where the harness's own native tool loop runs unmodified), and
   meaningfully short of OpenHands' native mode (where the agent implementation itself, not just
   the tool surface and turn sequencing, is OpenHands'). **Verdict for this field: PARTIAL —
   qm owns the outer session/tool/turn loop; it delegates only per-turn model generation to a
   pluggable harness SDK.** Whichever way this is read, it is not "attaches to an already-running
   bh-managed process" the way Orca/terminal are — see point 5.
3. **Permission approvals** — `README.md`, "Security and secrets": three org-selected postures —
   *"Strict — every harness tool call pauses for human approval, except the two no-effect turn
   enders,"* *"Auto (default) — a classifier screens provenance-labelled external data and tool
   results before they reach the model,"* *"Dangerous — no content screening, no pauses."* Plus,
   applying in every posture including Dangerous: *"The predeclared command policy — approval
   rules and hard denials for things like recursive deletes or destructive SQL."* Mechanically,
   this is the `toolApprovalGate` callback in `src/harness/harness.ts`'s `HarnessTurnInput`, with
   `pendingApprovals`/`pausedOnApproval` surfaced back in `HarnessTurnResult` and answered
   conversationally through qm's own Slack/web surfaces. (The seven `tag:idea` beads —
   `bh-nc1q`, `bh-enq4`, `bh-5waa` in particular — describe this same posture ladder from earlier,
   informal research; this source-read independently confirms their description was accurate,
   though it does not substitute for the CANCEL-ladder/mid-flight-mutability answers `bh-lrcw.2`/
   `.3` are chartered to produce.)
4. **Auth model** — org-level identity via qm's built-in one-time-email-link `auth` broker, or an
   external IdP registered against `<publicUrl>/auth/callback` (`docs/getting-started.md`,
   fetched 2026-08-03). Per `README.md`: *"the agent acts as the person it's working for, with
   their credentials and permissions"* — per-human delegated identity, not a shared bot token.
   Explicitly built for remote/public deployment (`qm init ... --target fly-or-aws`) — composes
   with remote-only natively; that is qm's default deployment shape, not an add-on.
5. **What bh must expose — inverted, not a seam.** Because qm embeds the harness SDK inside its
   own long-lived server process rather than attaching to an externally-launched one, adopting qm
   does not mean "bh exposes an interface qm's client attaches to" the way it does for
   terminal/Orca. It means **qm's core becomes the process that runs the harness**, and bh's role
   shrinks to whatever qm's `execute` tool can reach inside its per-scope sandbox — which, since
   that sandbox is "its durable computer" running arbitrary commands, already includes the `bh`/`bd`
   CLI unchanged (a qm-hosted seat can run `bh work claim/submit/...` as ordinary shell commands,
   exactly as a human would from a terminal — no new interface needed for that half). What *would*
   be new, concretely, if the operator wants tighter integration: (a) a `toolApprovalGate`-callable
   webhook or CLI verdict so qm's posture can consult bh's own review-gate state, and (b) a
   caller-supplied `session_id` contract so qm's audit trail, bh's OTEL spans, and bd events join
   on one key (idea bead `bh-tvzl`) — both **DEFERRED**, both explicitly waiting on `bh-lrcw`'s
   still-unrun probes (`bh-lrcw.3`/`.4`) to confirm they're even possible (per-scope postures?
   caller-supplied session id accepted at creation?). This is a data/service integration, not a
   front-end seam.

### 4. OpenHands — two structurally different modes; only one is "a front end"

Primary sources: `docs/design/ade-client-interfaces.md` §4 (fetched 2026-07-07, already in-repo);
`github.com/OpenHands/software-agent-sdk` — `openhands-sdk/openhands/sdk/security/confirmation_policy.py`,
`openhands-agent-server/openhands/agent_server/README.md`, `auth_router.py` (fetched 2026-08-03).

1. **Interface required — depends entirely on which mode.**
   - **Native mode**: none, from bh's side — OpenHands' own Agent Server (REST + WebSocket,
     `openhands-agent-server/openhands/agent_server/README.md`) owns the whole runtime; the
     harness underneath is OpenHands' own agent implementation, not something bh launches.
   - **ACP-client mode**: ACP-over-stdio — `ade-client-interfaces.md` §4 confirms OpenHands ships
     "ACP (Agent-Client Protocol) — open protocol driving third-party harnesses (Claude Code,
     Codex, Gemini) alongside its own open-source agent," citing OpenHands' own ACP announcement.
     In this mode OpenHands is the ACP **client**; per the ACP spec itself
     (`agentclientprotocol/agent-client-protocol`, `docs/protocol/v2/overview.mdx`, fetched
     2026-08-03): *"Agents ... typically run as subprocesses of the Client"* — so a bh-side ACP
     agent-adapter is what OpenHands' ACP client would launch and speak JSON-RPC 2.0 to over
     stdin/stdout (`docs/protocol/v2/transports.mdx`).
2. **Owns the agent loop? — this is the bead's named "sharpest dividing line," and it is a real
   yes for the mode most people mean by "OpenHands."** Native mode: **YES** — this is exactly what
   the bead's Requirements section flags: adopting OpenHands' own agent means adopting an
   orchestrator, not adding a front end to bh's own seats. Confirmed concretely by
   `openhands-sdk/.../security/{confirmation_policy.py,risk.py,llm_analyzer.py}` — OpenHands
   evaluates its **own** actions against its **own** `SecurityRisk` classifier and
   `ConfirmationPolicyBase` (`AlwaysConfirm`/`NeverConfirm`/`ConfirmRisky(threshold)`), which only
   makes sense if OpenHands' agent is the one taking the actions. ACP-client mode: **NO** — it
   fully delegates to the external harness's native loop, the same shape as Orca/terminal, just
   over JSON-RPC instead of a PTY.
3. **Permission approvals** — native mode: the `ConfirmationPolicyBase.should_confirm(risk)`
   decision (`confirmation_policy.py`, quoted above) surfaced as an event over the Agent Server's
   REST/WebSocket API for a client to approve/reject. ACP-client mode: ACP's own
   `session/request_permission` — a precisely specified agent→client JSON-RPC method
   (`agentclientprotocol/agent-client-protocol`, `schema/v2/schema.json`, `$defs.RequestPermissionRequest`)
   carrying a title/description/subject and a `PermissionOption` list
   (`allow_once`/`allow_always`/`reject_once`/`reject_always`, `$defs.PermissionOptionKind`),
   answered with `RequestPermissionResponse` (`x-method: "session/request_permission"`). This is
   the single most exactly-specified permission-approval mechanism of any candidate here.
4. **Auth model** — a single shared bearer `SESSION_API_KEY` over the Agent Server's REST/WS API:
   *"If set, all requests must include this key in the `Authorization` header as `Bearer <key>`"*
   (`openhands-agent-server/openhands/agent_server/README.md`, "Configuration Options"). Composes
   trivially with remote-only (any reverse proxy/tunnel in front of a bearer-token API) but is a
   shared secret, not per-user identity, in the open-source Agent Server (`ade-client-interfaces.md`
   §4 notes Cloud/Enterprise exist separately and presumably add real multi-user auth — not
   independently verified here). ACP-client mode's auth is negotiated per-agent via
   `auth/login`/`auth/logout` (`docs/protocol/v2/authentication.mdx`) — i.e. whatever the bh-side
   ACP adapter advertises, which could reuse the harness's own existing auth unchanged.
5. **What bh must expose** — native mode: same inverted shape as qm (§3.5) — OpenHands' server
   becomes the runtime; not a seam bh exposes. ACP-client mode: exactly what `bh-xls2.4` ("Phase 3:
   ACP runtime adapter (multi-harness ingestion)") and `bh-xls2.5` ("Phase 4: interactivity —
   permission approvals + chat ... Orca-replacement milestone") already scope — a bh-side ACP
   agent-adapter process implementing `initialize` / `session/new` / `session/prompt` /
   `session/update` / `session/request_permission` / optional `auth/login`, wired to whichever
   harness the seat runs. Community precedent exists for wrapping Claude Code this way
   specifically (`Xuanwo/acp-claude-code`, found via GitHub search 2026-08-03) — bh would not be
   inventing the pattern from nothing. This same adapter would also serve any other ACP client,
   including the first-party beadhive-ui `bh-xls2` is building.

### 5. openchamber — identity confirmed, and it is architecturally its own fourth shape

The bead explicitly requires openchamber's identity be confirmed with a cited source or excluded.
**Confirmed.** `github.com/openchamber/openchamber` — GitHub repository description (via GitHub
search API, fetched 2026-08-03): *"Desktop and web interface for OpenCode AI agent."*
`openchamber.dev`'s own site meta (fetched 2026-08-03): *"OpenChamber is an agentic development
environment for AI coding across desktop, browser, phone, and VS Code."* `README.md` (fetched
2026-08-03), "Why OpenCode?" section: *"OpenChamber uses OpenCode to power its coding agents...
OpenChamber is an independent project and is not affiliated with the OpenCode team."*

1. **Interface required** — HTTP+SSE to OpenCode's own server API, via the official
   `@opencode-ai/sdk` — confirmed as a direct dependency in openchamber's `package.json` (fetched
   2026-08-03: `"@opencode-ai/sdk": "1.18.11"` under `dependencies`). Note: the same `package.json`
   also lists `bun-pty` as a *patched* dependency — a real PTY is used, but only for openchamber's
   auxiliary embedded-terminal feature ("reattach to a running terminal," per its own README
   feature list), not for driving the agent itself.
2. **Owns the agent loop?** No. Per its own README, OpenCode is the engine; openchamber is
   explicitly and only a client over OpenCode's server, the same "attaches to an existing loop"
   shape as Orca/terminal — just over HTTP+SSE instead of a PTY, and hard-locked to one harness.
3. **Permission approvals** — tracked as a first-class session status alongside
   working/waiting/finished/failed (README: *"See which sessions are working, waiting, finished,
   or failed, along with approvals, scheduled tasks, provider limits, token use, and costs"*),
   sourced from OpenCode's own permission mechanism via the SDK. The exact wire schema is
   OpenCode's own (not independently re-verified in this spike — out of scope for a candidate
   that is fundamentally OpenCode's front end, not bh's).
4. **Auth model** — a local UI password / WebAuthn passkeys gate, plus per-device pairing tokens
   issued through openchamber's own **Private Relay** for remote/mobile access — *"end-to-end
   encrypted and can be revoked at any time"* (`packages/docs/content/docs/security.mdx`, fetched
   2026-08-03). Explicitly designed to compose with remote-only — "Ship from anywhere" is
   openchamber's core pitch, and it is the only candidate here with a purpose-built, revocable,
   E2E-encrypted mobile-pairing story that is not just "a private network path" (contrast Orca §2.4).
5. **What bh must expose — a fourth, incompatible shape, and a hard harness lock.** openchamber
   can only ever drive an OpenCode-backed seat — it cannot drive a Claude-Code-based bh seat under
   any seam bh builds, because openchamber's client only speaks OpenCode's SDK. `role.py:113`
   already lists `opencode` as a `KNOWN_HARNESSES` value, but only in its bare interactive-TUI
   form (`opencode --agent <seat>`, line 124) — for openchamber to attach at all, bh would need a
   **new launch mode** running `opencode serve` (OpenCode's own headless HTTP+SSE server) instead,
   and expose that server's URL/port to a paired openchamber client. That is an incremental
   addition (the harness is already supported), but it is a genuinely different "what bh exposes"
   shape from both the PTY seam (§1-2) and the ACP seam (§4) — and it only ever covers the subset
   of seats running OpenCode.

### 6. The durable-session layer is a prerequisite, not a distinguisher

`bh-lx6e`'s own Design section: "Layer 2 is not an alternative to anything — a seat that dies when
the websocket drops is not a seat, so every front end needs it." `bh-lx6e.1` (SPIKE: bh seat —
durable sessions over a pluggable exec transport, **in progress**, no verdict doc yet as of this
spike) is chartered to answer whether one verb set (`bh seat ls | attach | kill`, tmux-backed) can
cover the transport layer under the PTY-shaped candidates (terminal, Orca). This spike treats that
as a shared prerequisite for §1-2, not a per-candidate distinguishing fact — it does not change
which candidates share an interface *shape*, only whether that shape survives a disconnect.

### 7. Interaction-type distribution — evidence check per the amended clause, named gap

Per the notes amendment: derive this only from session evidence that already exists; if
insufficient or absent, say so explicitly rather than estimate.

1. **OTEL/observaloop traces**: `bh doctor` on this machine reports `otel.enabled: false`. No
   traces are being captured in this environment. This source is **absent**, not merely unqueried.
2. **bd event history**: `bd`'s event/audit trail (`bd history`, gate/lifecycle events) records
   bh-verb-level bead-state transitions (assigned/claimed/submitted/merged/gate-resolved) — the
   right granularity for "bead-state decisions already expressible as bh verbs" as a *category
   label*, but it cannot see the other three categories the bead names (permission approvals,
   free-form steering, genuinely terminal-shaped work) at all, because those happen inside a
   session's turns, which bd does not record. This source answers the wrong question by
   construction, not an incomplete version of the right one.
3. **Transcripts**: real Claude Code session transcripts for this hive exist locally
   (`~/.claude/projects/-Users-brian-workspace-github-beadhive-beadhive/`, well over 100 `.jsonl`
   files as of 2026-08-03), and a plain-text scan confirms a large subset reference
   `bh:dispatcher`/`bh:supervisor` agent invocations — so this evidence source is **not absent**.
   But turning that into the four-way distribution the bead asks for needs a reliable,
   reproducible classifier for what "a permission approval" looks like inside a raw transcript
   (Claude Code's own interactive approval prompt is not recorded as a distinct event type in this
   schema at a first pass — it happens at a layer between the model's tool-use block and the
   recorded tool-result, so a shallow parse would systematically undercount it) — and a
   trustworthy sample of **at least 5** sessions specifically run under the dispatcher or
   supervisor seat, not developer/planner/merger. Building and validating that classifier is its
   own scoped effort, not a side-effect of a source-reading spike; a shallow attempt here would
   produce a number that *looks* measured while resting on an unvalidated definition of
   "permission approval" — exactly the failure mode the amended notes warn against ("An invented
   number here would look exactly like a measured one").

**Conclusion for this clause: the distribution is not reported.** Evidence exists in principle
(transcripts) but not in a form this spike could turn into a reliable four-way split within its
own bounded scope; OTEL is off; bd is the wrong granularity. This is stated as a named gap per the
notes' explicit instruction, not filled with an estimate. See Recommendation for what capturing it
properly would take.

## Verdict — **NO-GO** (on "one interface for all five"), with a named partial seam

One interface does **not** cover even the GO bar's minimum ("terminal attach, orca and qm can all
drive"): qm's integration shape is not a front end attaching to a bh-exposed interface at all (§3.5)
— it is bh becoming a callable inside qm's own sandbox, the reverse relationship. That alone fails
the stated GO bar, independent of OpenHands or openchamber.

The five candidates split into **four incompatible shapes**, not five independent ones — the split
is real structure, not just "everything is different":

| Shape | Candidates | Interface | Owns loop? |
|---|---|---|---|
| **A — PTY, attaches to a bh-launched process** | terminal (baseline), orca | pty | no |
| **B — ACP-over-stdio, attaches to a bh-launched process** | OpenHands (ACP-client mode) [+ a planned first-party UI, `bh-xls2.4`/`.5`, not itself one of the five candidates] | JSON-RPC 2.0 over stdio | no |
| **C — bh becomes a callable inside an external runtime's own sandbox/loop** | qm, OpenHands (native mode) | none from bh (inverted) | qm: partial; OpenHands-native: yes |
| **D — HTTP+SSE to one specific external harness's own server, harness-locked** | openchamber | HTTP+SSE via `@opencode-ai/sdk` | no (but only ever serves an OpenCode-backed seat) |

Shape A is buildable now and already in progress (`bh-lx6e.1`); it covers exactly 2 of 5:
terminal and orca. Shape B is buildable and already scoped elsewhere in the backlog
(`bh-xls2.4`/`.5`); it covers OpenHands *only in ACP-client mode*, not OpenHands as most people
mean it. Shape C is not a front-end integration at all — for qm and OpenHands' native mode,
"adopting" them means adopting an orchestrator that bh's beads/gates would need to be read by or
written to, the inversion the bead's Requirements section specifically warned about for
OpenHands, and which this spike's source-reading shows applies to qm's core loop-ownership too
(§3.2), contradicting this bead's own initial framing of qm as safely "the surface, not the
engine." Shape D serves nobody else and only ever serves an OpenCode-backed seat.

This is a legitimate partial answer, not a failure to answer: **Shape A is the concrete, buildable,
already-in-flight seam this spike recommends as the near-term investment** (see Recommendation).

## Recommendation

1. **Ship Shape A first.** It already covers 2 of 5 named candidates (terminal, orca) with no new
   protocol work — only the durable-session layer `bh-lx6e.1` is already spiking. `bh-lx6e.4`
   ("prove the front-end seam against orca") should prove *this* shape specifically: a durable,
   detachable PTY session that both a raw terminal and Orca's own PTY-spawn model can attach to.
2. **Do not force qm, OpenHands-native, or openchamber onto Shape A or Shape B.** Per §3.5/§4.5/§5.5,
   none of them fit either seam without becoming a fundamentally different kind of integration
   (a data/service integration for qm and OpenHands-native; a harness-locked HTTP+SSE client for
   openchamber). If the operator wants one of these at `bh-lx6e.5`, that is explicitly a decision
   to adopt a different orchestrator (qm, OpenHands-native) or accept a harness lock (openchamber)
   — not an engineering task to make bh's seam "fit" them. Naming that distinction clearly at the
   decision point is the main way this comparison avoids misleading the operator, per this bead's
   own charge.
3. **Shape B (ACP) is worth building on its own merits, separately from this spike's GO/NO-GO.**
   `bh-xls2.4`/`bh-xls2.5` already scope it, and it is the only shape with a precisely specified,
   already-standardized permission-approval method (`session/request_permission`). It would serve
   OpenHands-ACP-mode and the planned first-party beadhive-ui, but should not be conflated with
   "OpenHands support" generally — say explicitly which mode is meant whenever it comes up.
4. **When bh-lrcw actually runs**, its posture/CANCEL-ladder/session-id answers (`bh-lrcw.2`/`.3`/`.4`)
   will determine whether the Shape-C data integration named in §3.5 (posture-aware webhook,
   `session_id` correlation) is even possible — currently all four of those held `tag:idea` beads
   are waiting on exactly that. This spike's qm profile should be treated as source-reading, not a
   substitute for bh-lrcw's live probe.
5. **Interaction-type distribution — what it would take to capture it properly**, since it stayed
   unmeasured (§7): either (a) turn on `otel.enabled` for dispatcher/supervisor seats going
   forward with a per-turn interaction-type span attribute defined up front (permission_approval /
   bh_verb_decision / free_form_steering / terminal_shaped), so future sessions are natively
   measurable without a retrospective mining pass; or (b) commission a small, explicitly-scoped
   analysis pass over the existing `~/.claude/projects` transcripts for this hive, with the
   taxonomy defined and a human spot-check of classifier accuracy *before* anyone treats its output
   as evidence for `bh-lx6e.5` — not a byproduct of an unrelated spike's research budget.
