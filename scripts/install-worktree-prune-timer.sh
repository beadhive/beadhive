#!/usr/bin/env bash
# Install, inspect, or remove the per-user systemd timer for SAFE-only bh worktree pruning.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install-worktree-prune-timer.sh [--interval DURATION] [--verify|--uninstall]

Install a user-level systemd timer that runs `bh worktree prune` across every managed hive.
The default interval is 6h. DURATION uses systemd's time syntax, for example: 30min, 6h, 1d.
EOF
}

mode=install
interval=6h
while (($#)); do
  case "$1" in
    --interval)
      (($# >= 2)) || { echo "--interval needs a duration" >&2; exit 2; }
      interval=$2
      shift 2
      ;;
    --verify)
      mode=verify
      shift
      ;;
    --uninstall)
      mode=uninstall
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

command -v systemctl >/dev/null 2>&1 || {
  echo "systemctl is required; use the documented cron fallback instead" >&2
  exit 1
}

config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
unit_dir="$config_home/systemd/user"
timer=bh-worktree-prune.timer
service=bh-worktree-prune.service

case "$mode" in
  verify)
    systemctl --user is-enabled "$timer"
    systemctl --user list-timers "$timer" --all
    exit 0
    ;;
  uninstall)
    systemctl --user disable --now "$timer" >/dev/null 2>&1 || true
    rm -f "$unit_dir/$timer" "$unit_dir/$service"
    rm -rf "$unit_dir/$timer.d"
    systemctl --user daemon-reload
    echo "removed $timer"
    exit 0
    ;;
esac

if ! systemd-analyze timespan "$interval" >/dev/null 2>&1; then
  echo "invalid systemd duration: $interval" >&2
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
payload_dir="$HOME/.local/share/beadhive/scripts"
mkdir -p "$unit_dir/$timer.d" "$payload_dir"
install -m 0755 "$script_dir/bh-worktree-prune" "$payload_dir/bh-worktree-prune"
install -m 0644 "$script_dir/$service" "$unit_dir/$service"
install -m 0644 "$script_dir/$timer" "$unit_dir/$timer"
printf '[Timer]\nOnUnitActiveSec=%s\n' "$interval" >"$unit_dir/$timer.d/interval.conf"

systemctl --user daemon-reload
systemctl --user enable --now "$timer"
echo "installed $timer (every $interval)"
