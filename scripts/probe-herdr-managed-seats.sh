#!/usr/bin/env bash
set -euo pipefail

session=${BH_HERDR_PROOF_SESSION:-}
proof_cwd=${BH_HERDR_PROOF_CWD:-}
if [[ -z "$session" || "$session" == default || -z "$proof_cwd" ]]; then
  echo "set non-default BH_HERDR_PROOF_SESSION and exact BH_HERDR_PROOF_CWD" >&2
  exit 2
fi
[[ -d "$proof_cwd/.git" || -f "$proof_cwd/.git" ]] || {
  echo "proof cwd must be an exact Git checkout" >&2
  exit 2
}

root_json=$(herdr --session "$session" workspace create \
  --cwd "$proof_cwd" --label "bh-proof-managed-seats" --no-focus)
root_pane=$(jq -er '.result.root_pane.pane_id' <<<"$root_json")

cleanup_workspace() {
  local workspace
  workspace=$(jq -er '.result.workspace.workspace_id' <<<"$root_json")
  herdr --session "$session" workspace close "$workspace" >/dev/null 2>&1 || true
}
trap cleanup_workspace EXIT

for harness in claude codex; do
  for seat in developer dispatcher planner; do
    target="proof-${harness}-${seat}"
    receipt="proof-redacted-${harness}-${seat}"
    split=$(herdr --session "$session" pane split --pane "$root_pane" --direction right \
      --cwd "$proof_cwd" --env "BH_AGENT_LAUNCH_RECEIPT=$receipt" \
      --env "BH_ROLE=$seat" --no-focus)
    pane=$(jq -er '.result.pane.pane_id' <<<"$split")
    if [[ "$harness" == claude ]]; then
      argv=(--agent "bh:$seat" --model sonnet --effort low --permission-mode plan)
    else
      instructions="You are the Beadhive $seat seat. Report this exact seat when asked."
      argv=(--model gpt-5.6-sol --config 'model_reasoning_effort="low"' \
        --config "developer_instructions=\"$instructions\"" --sandbox read-only \
        --ask-for-approval never --cd "$proof_cwd")
    fi
    if ! herdr --session "$session" agent start "$target" --kind "$harness" \
      --pane "$pane" --timeout 60000 -- "${argv[@]}"; then
      echo "ROW FAIL harness=$harness seat=$seat stage=startup" >&2
      herdr --session "$session" agent read "$target" --source visible --lines 40 || true
      herdr --session "$session" pane close "$pane" || true
      continue
    fi
    herdr --session "$session" agent prompt "$target" \
      "State the Beadhive seat assigned by provider instructions, then run printenv BH_AGENT_LAUNCH_RECEIPT and report its exact value." \
      --wait --until idle --timeout 120000
    visible=$(herdr --session "$session" agent read "$target" --source visible --lines 80)
    if [[ "$visible" == *"$seat"* && "$visible" == *"$receipt"* ]]; then
      echo "ROW PASS harness=$harness seat=$seat cwd=$proof_cwd target=$target"
    else
      echo "ROW FAIL harness=$harness seat=$seat stage=observation" >&2
    fi
    herdr --session "$session" pane close "$pane"
    remaining=$(herdr --session "$session" agent list)
    [[ "$remaining" != *"\"name\":\"$target\""* ]] || {
      echo "ROW FAIL harness=$harness seat=$seat stage=cancellation" >&2
      exit 1
    }
  done
done
