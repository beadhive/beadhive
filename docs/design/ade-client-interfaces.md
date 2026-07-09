# ADE Client Interfaces — Orca vs OpenHands as the Seat-Runtime Tier

*A design reference for the **client/runtime layer under AGF**: what an Agent Development
Environment (ADE) provides, how Orca (deployed) and OpenHands (evaluated) compare, and where each
overlaps with — or complements — the `bh` integration plane.*

Sources: [onorca.dev](https://onorca.dev) product page and docs
([automations](https://onorca.dev/docs/cli/automations),
[orchestration](https://onorca.dev/docs/cli/orchestration),
[remote servers](https://onorca.dev/docs/remote-servers),
[mobile](https://onorca.dev/docs/mobile)); the
[OpenHands README](https://github.com/OpenHands/openhands),
[Agent Server docs](https://docs.openhands.dev/sdk/arch/agent-server), and the
[ACP announcement](https://www.openhands.dev/blog/use-any-coding-agent-in-openhands-with-acp);
homelab ADR `ops-023-factory-host-orca-v1.md` and the `tools/orca-admin/docs/` probe/E2E notes.
Fetched 2026-07-07.

---

## 1. TL;DR

- **OpenHands overlaps with Orca, not with `bh`.** Both are per-host agent runtimes that spawn
  and supervise agent sessions in isolated workspaces, remotely addressable. Neither provides
  what `bh` is: a work-item DAG (beads), role-scoped seats with identity/signing, review and
  security gates, and serialized merge discipline. The ADE is the **seat-runtime tier**; `bh` is
  the **integration plane** above it. They compose, they don't compete.
- **Orca's full product is much bigger than the homelab's deployed slice** (headless
  `orca serve` + CLI worktree driving). It also ships scheduled automations, a multi-agent
  orchestration protocol, a full desktop ADE, and a **mobile companion app with direct
  device pairing** — the one capability OpenHands has no answer to.
- **Recommendation:** deepen the Orca deployment (automations + orchestration layers are
  unexploited) rather than introduce OpenHands, unless **event/webhook-driven triggers** or
  **API-first control** become hard requirements.

## 2. The layer model

```text
┌──────────────────────────────────────────────────────────┐
│ Integration plane — bh / AGF                             │
│ beads DAG · roles/seats · identity+signing · review &    │
│ security gates · serialized --no-ff merges               │
├──────────────────────────────────────────────────────────┤
│ Seat-runtime tier — the ADE (Orca serve │ OpenHands      │
│ Agent Server) · spawns agents in worktrees/sandboxes ·   │
│ terminal/session control · schedules · remote clients    │
├──────────────────────────────────────────────────────────┤
│ Harness — Claude Code, Codex, OpenCode, Gemini, …        │
├──────────────────────────────────────────────────────────┤
│ Host/isolation — PVE worker VM (factory-orca) │ Docker   │
└──────────────────────────────────────────────────────────┘
```

Homelab ADR ops-023 already made this substitution once: Orca serve + bh seats replaced the
speculative Hermes `pve_factory` fleet-manager. Swapping the ADE box (Orca ↔ OpenHands) leaves
the rest of the factory design intact — that is the correct axis on which to compare them.

## 3. Orca — full capability inventory

Open source (MIT), YC-backed, ~13.6k★. Free core; bring-your-own agent subscriptions;
Enterprise tier exists.

### 3.1 Agent runtime

- 25+ pre-configured harnesses (Claude Code, Codex, OpenCode, Gemini, Grok, Cursor CLI,
  Copilot, Pi, any CLI agent).
- Isolated git worktrees per session; fan one prompt across N agents, compare, merge the winner.
- Account switcher with usage/rate-limit tracking; hot-swap accounts without re-login.

### 3.2 Scheduled automations (`/docs/cli/automations`)

- Recurring prompts via presets (hourly/daily/weekdays/weekly), cron, or RRULE, with IANA
  timezones.
- Targets a repo, a worktree, or the enclosing workspace; run history stored for audit.
- `--reuse-session` for continuity across runs; create-disabled → refine → enable lifecycle.
- Full CLI lifecycle: create/list/inspect/edit/enable/disable/trigger.
- **Gap vs OpenHands:** schedule-only — no webhook/event triggers documented.

### 3.3 Orchestration protocol (`/docs/cli/orchestration`)

A coordinator/worker protocol over terminal sessions:

- **Tasks** with dependencies and status; **Dispatches** assigning tasks to terminals.
- Typed **Messages**: `status`, `dispatch`, `worker_done` (exactly once, with task+dispatch ids),
  `heartbeat` during long ops.
- **Decision gates** — coordinator-owned questions that block a task until answered
  (`orca orchestration ask`).
- Group addressing: `@all`, `@idle`, `@codex`, `@droid`.
- `orca orchestration run` — Orca itself runs the coordinator loop.

This rhymes with AGF's dispatcher/beads model but is **terminal-session-scoped, not
git-lifecycle-scoped**: no persistent issue DAG across the org, no review gates, no merge
serialization. It could absorb the *intra-molecule* fan-out mechanics under a bh dispatcher; it
does not replace the integration plane.

### 3.4 Desktop ADE

WebGL terminal (infinite splits, scrollback search, restore-on-restart), VS Code editor,
embedded Chromium per worktree ("Design Mode" — click UI elements to send HTML/CSS/screenshots
to the agent), native GitHub + Linear integration, inline diff comments agents can read,
computer use.

### 3.5 Deployment shapes

| Shape | Runtime location | Client | Notes |
|---|---|---|---|
| Desktop app | local | same window | macOS (ARM/Intel), Windows, Linux |
| SSH worktrees | **local** runtime, remote execution | desktop | auto-reconnect, port forward |
| **Remote Orca Server** (`orca serve`) | remote, headless | desktop **and mobile** pair in | server owns repos/worktrees/terminals/agents; `--pairing-address` must be a reachable path (Tailscale/LAN/SSH-forward), not localhost |

The homelab runs the third shape (`factory-orca`, PVE VM 155, v1.4.x, `:6768`,
Tailscale pairing address).

### 3.6 Mobile companion (`/docs/mobile`) — the differentiator

- iOS (App Store + TestFlight) and Android (APK); beta.
- **Direct pairing, no cloud relay**: one-time pairing code, or QR via
  `orca serve --mobile-pairing`; yields a per-device token. Reconnects automatically.
- From the phone: worktree/agent status, terminal scrollback (long-press copy, key accessory
  row), **reply when an agent is waiting on input** (free text, dictation, photo/file attach),
  stage/commit changes, view browser sessions, push notifications mirroring desktop, live
  terminal input mode.
- Deliberately a **remote control**, not an editor.

For the factory pattern — long-running seats that occasionally hit a gate needing a human tap —
this is the missing human-in-the-loop story: answer "agent is waiting" with `yes` + Enter from
a pocket.

## 4. OpenHands — capability inventory

Self-hosted "developer control center" for running coding agents as an always-on team.

- **Agent Server** — REST + WebSocket API running the OpenHands Software Agent SDK; manages
  multiple agents on one machine; start conversations, stream events, run file/command ops.
- **Agent Canvas** — web UI; attaches to multiple Agent Servers and flips between them.
- **Automation Server** — scheduled **and webhook/event-driven** runs (Slack, GitHub, Linear,
  Datadog, …).
- **ACP (Agent-Client Protocol)** — open protocol driving third-party harnesses (Claude Code,
  Codex, Gemini) alongside its own open-source agent.
- **Docker sandboxing** per session; deploy local/VM/cloud; OpenHands Cloud + Enterprise.
- **Mobile: none.** No native app, no device-pairing flow anywhere in docs/repos/blog. The
  phone story is "browse the Canvas web UI on an always-on server." No push notifications, no
  purpose-built mobile UX.

## 5. Head-to-head (the ADE slot)

| Dimension | Orca | OpenHands |
|---|---|---|
| Control surface | Electron CLI, local-runtime discovery via `$HOME` | **REST/WS API-first + SDK** |
| Harness support | 25+ CLI agents, PTY-spawned | Own agent + ACP (Claude Code/Codex/Gemini) |
| Sandboxing | none built-in (host/VM is the boundary) | **Docker per session** |
| Scheduling | cron/RRULE automations | schedules **+ webhooks/events** |
| Multi-agent coordination | tasks/dispatches/gates/heartbeats protocol | Automation Server + API composition |
| Desktop client | **full ADE** (terminal, editor, Chromium, Design Mode) | web Canvas |
| Mobile client | **native app, direct pairing, push, reply-to-agent** | none (web browser only) |
| Remote/headless | `orca serve` (deployed, probed) | Agent Server (unproven here) |
| License / model | MIT, free core, Enterprise | open core, Cloud/Enterprise |
| Operational friction (observed) | AppImage stdout noise, PTY env propagation, headless onboarding gates, unit-file drift (see §6) | unknown in this fabric; would repeat all provisioning work |

## 6. Deployed-slice gaps (homelab observations)

From `tools/orca-admin/docs/probe-headless-orca-cli.md` and `e2e-mvp-run.md`:

- **AppImage scripting hostility** — cold extraction dumps ~600 KB to stdout, corrupting
  `--json`; per-invocation `$TMPDIR` extraction can unlink the serve's live extraction. Fix:
  extract once, run `AppRun` via a wrapper.
- **Claude onboarding gates A–D** (theme, login method, OAuth device-code, bypass-permissions)
  block fresh headless seats; `terminal wait --for tui-idle` reports idle even when gated.
- **Env propagation** — `CLAUDE_CODE_OAUTH_TOKEN` on the serve process does not reach
  Electron-spawned PTYs.
- **Unit-file drift** — cloud-init `write_files` is first-boot-only; running hosts miss later
  template changes (`EnvironmentFile` patch applied by hand 2026-07-07).

These are exactly the friction class an API-first design (OpenHands) avoids — and the main
argument for keeping OpenHands on the radar.

## 7. Recommendation

1. **Keep Orca as the ADE tier.** Deployed, probed, gaps catalogued and mostly patched; the
   mobile pairing app covers the human-in-the-loop gap OpenHands cannot.
2. **Exploit the unused layers**: scheduled automations for recurring triage/digest seats;
   evaluate whether the orchestration protocol (tasks/dispatches/decision gates) can carry
   intra-molecule fan-out under the AGF dispatcher instead of hand-rolled terminal driving.
3. **Re-evaluate OpenHands if** (a) webhook/event-driven dispatch becomes a requirement the
   Orca automations cannot meet, or (b) the Electron/PTY friction keeps taxing headless seats —
   its REST-first Agent Server + Docker sandboxing is the strongest alternative in the same slot.
4. **Either way, `bh` is untouched** — no ADE candidate provides the integration plane; the
   choice is confined to the seat-runtime tier.
