# Alerts — proactive agent steering

`bh alerts show` is the small, normalized surface for conditions that warrant an
agent or operator's attention. It reports only active alerts, each with
`severity`, `code`, `message`, and `remediation`; `--json` emits that list directly
for a harness. The same list is available through the `beadhive://alerts` MCP
resource.

Today the first source is the warning set already calculated by `bh doctor`. New
warning sources register an alert rule here instead of adding a bespoke check to
every work or dispatch command.

## Harness hook pattern

A harness hook such as Claude Code's `SessionStart` should call `bh alerts show
--json` (or read `beadhive://alerts`) and, only when the list is non-empty, inject
short steering text naming the active alerts and their remediation. That makes a
durable condition visible at the start of a session without turning every command
into a warning gate.

The hook is a convenience, not the only access path: an agent or operator may run
`bh alerts show` or read `beadhive://alerts` at any time. MCP clients that read the
resource establish a baseline; later MCP mutations emit `resources/updated` only
when the normalized alert list changes, prompting subscribed clients to re-read it.
