// beadhive: steer direct `bd` invocations toward the rig-aware `bh bd` passthrough.
//
// Ported from the bh Claude Code plugin's scripts/bd-steer.sh. That version is a
// PreToolUse hook and can only attach a non-blocking hint (Claude's permission-decision
// contract has no way to rewrite the command actually executed); OpenCode's
// `tool.execute.before` hook can rewrite `output.args.command` outright, so this steers
// for real instead of just nudging — `bd create` auto-applies provider/org/repo, and
// `-r <rig>`/`--all` route the call across rigs, so a raw `bd` can silently hit the wrong
// database.
//
// Conservative like the original: only rewrites a SIMPLE `bd ...` invocation (`bd` as the
// very first token, optionally after leading whitespace). Any chaining/pipes/redirects/
// substitution bails out untouched — rewriting just the leading token of a compound command
// risks getting the rest of it wrong, so a mixed command just runs as typed (same bail list
// approve-readonly.sh uses to stay conservative).
export const BhSteerPlugin = async () => ({
  "tool.execute.before": async (input, output) => {
    if (input.tool !== "bash") return
    const command = output.args && output.args.command
    if (typeof command !== "string") return
    if (/[\n]|&&|\|\||;|\||>|<|\$\(|`/.test(command)) return

    const match = command.match(/^(\s*)bd(\s+\S.*|\s*)$/s)
    if (!match) return

    output.args.command = `${match[1]}bh bd${match[2]}`
  },
})
