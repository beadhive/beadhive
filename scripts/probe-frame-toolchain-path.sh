#!/bin/sh
# Reproducible bh-qn9zt probe for a freshly launched frame command context.
set -eu

expected_home=${FRAME_EXPECTED_HOME:-$HOME}
expected_prefix="$expected_home/.local/bin:$expected_home/.nix-profile/bin:/nix/var/nix/profiles/default/bin"

uid=$(id -u)
test "$uid" -ne 0
test "$HOME" = "$expected_home"
case "$PATH" in
  "$expected_prefix"|"$expected_prefix":*) ;;
  *)
    printf 'unexpected PATH ordering: %s\n' "$PATH" >&2
    exit 1
    ;;
esac
case ":$PATH:" in
  *:/root/*)
    printf 'root-owned path leaked into non-root frame: %s\n' "$PATH" >&2
    exit 1
    ;;
esac
printf '%s\n' "$PATH" | awk -F: '
  {
    for (i = 1; i <= NF; i++) {
      for (j = i + 1; j <= NF; j++) {
        if ($i == $j) {
          print "duplicate PATH entry: " $i > "/dev/stderr"
          exit 1
        }
      }
    }
  }
'

for tool in bd dolt nix; do
  command -v "$tool" >/dev/null
done

printf 'uid=%s\n' "$uid"
printf 'home=%s\n' "$HOME"
printf 'path=%s\n' "$PATH"
printf 'profile_target=%s\n' "$(readlink -f "$HOME/.nix-profile")"
printf 'bd=%s\n' "$(command -v bd)"
printf 'dolt=%s\n' "$(command -v dolt)"
printf 'nix=%s\n' "$(command -v nix)"
bd version
dolt version
nix --version
