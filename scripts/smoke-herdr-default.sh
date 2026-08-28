#!/usr/bin/env bash
set -euo pipefail

hive=${1:?usage: smoke-herdr-default.sh HIVE_ID}
bh_bin=${BH_BIN:-bh}

status_json=$(env -u BH_HERDR_SESSION "$bh_bin" plugin herdr status --json)
presentation_json=$(
  env -u BH_HERDR_SESSION "$bh_bin" plugin herdr view presentation --hive "$hive" --json
)
crew_json=$(env -u BH_HERDR_SESSION "$bh_bin" plugin herdr view crew --hive "$hive" --json)

jq -e '.session == "default" and .disposition == "available" and .server.available == true' \
  <<<"$status_json" >/dev/null
jq -e \
  '.workspace.locator.session == "default" and .workspace.correlation.state == "exact"' \
  <<<"$presentation_json" >/dev/null
jq -e \
  '.scope.session == "default" and .workspace.locator.session == "default"' \
  <<<"$crew_json" >/dev/null

printf 'Herdr default-session smoke passed for %s\n' "$hive"
