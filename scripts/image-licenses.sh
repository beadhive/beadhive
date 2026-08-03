#!/usr/bin/env bash
# Assert a BUILT image still carries third-party licence notices.
#
# WHY THIS EXISTS: publishing an image makes Beadhive a REDISTRIBUTOR of every component in it.
# The PyPI wheel conveys nothing — pip fetches certifi from PyPI, so no third-party clause binds
# us. An image layer contains the actual bytes, so every "retain this notice in copies" term
# (MIT, BSD-2/3, ISC, Apache-2.0, MPL-2.0, PSF, HPND) binds us directly.
#
# Today that works BY ACCIDENT: `uv tool install` happens to preserve .dist-info/licenses/.
# Nothing asserts it. A future "slim the image" change — stripping dist-info, a multi-stage
# copy of only .py files, an aggressive prune — drops every notice at once, SILENTLY. Nothing
# fails, no test goes red, and the image quietly ships out of compliance. That is the single
# failure this script exists to catch.
#
# Usage: image-licenses.sh <image-ref>
# Runs in bh-pc2a.17's proof gate, where a built image exists. The unit suite cannot build an
# image (see tests/test_image_build.py's module docstring), so the DECISION LOGIC here is
# covered by tests/test_image_licenses.py with a stubbed docker.
set -uo pipefail

IMAGE=${1:-}
if [ -z "$IMAGE" ]; then
  echo "usage: image-licenses.sh <image-ref>" >&2
  exit 2
fi

# Dists KNOWN to ship no licence file, each with a reason. An entry here is a deliberate,
# reviewed exception — not a way to silence the check. Adding one means you established WHY.
#
#   fastmcp-slim — ships no LICENSE/COPYING in its wheel at all (verified by inspecting the
#     installed dist). It DECLARES a licence in metadata, so the licence gate (just
#     license-check) passes it; what is absent is the notice FILE that attribution requires.
#     Upstream gap, not ours to fix; recorded rather than hidden.
KNOWN_MISSING="fastmcp_slim"

# Wholesale-stripping backstop. If a change guts dist-info, the missing-list check alone could
# be fooled (no dists found => nothing "missing"). A floor on the absolute count cannot be.
# Real count at the time of writing: 85 dists, 84 carrying licence material.
MIN_DISTS=60

# One shell snippet, run inside the image. Emits "<dist> <file-count>" per dist.
# NOTE: `ls a b c` exits non-zero when ANY arg is missing, which over-reports absence — use
# find, which matches per-name. That bug cost a false "6 dists missing licences" reading.
# shellcheck disable=SC2016  # single quotes are deliberate: this expands INSIDE the container,
# not here. Double quotes would interpolate the host's $d/$n and send a broken snippet.
INVENTORY='
for d in $(find / -name "*.dist-info" -type d 2>/dev/null); do
  n=$(find "$d" \( -ipath "*/licenses/*" -o -iname "LICENSE*" -o -iname "COPYING*" \
        -o -iname "NOTICE*" -o -iname "AUTHORS*" \) -type f 2>/dev/null | wc -l)
  echo "$(basename "$d") $n"
done'

out=$(docker run --rm --entrypoint sh "$IMAGE" -c "$INVENTORY" 2>/dev/null)
rc=$?
if [ $rc -ne 0 ] || [ -z "$out" ]; then
  echo "image-licenses: could not inventory $IMAGE (docker exit $rc, empty output)" >&2
  echo "  The image must exist locally — bake it first (just image core)." >&2
  exit 1
fi

total=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
missing=$(printf '%s\n' "$out" | awk '$2 == 0 { print $1 }')

fail=0

if [ "$total" -lt "$MIN_DISTS" ]; then
  echo "image-licenses: FAIL — only $total dist-info dirs found, expected >= $MIN_DISTS." >&2
  echo "  That is consistent with dist-info being stripped from the image." >&2
  fail=1
fi

unexpected=""
for d in $missing; do
  base=${d%%-*}
  case " $KNOWN_MISSING " in
    *" $base "*) ;;
    *) unexpected="$unexpected $d" ;;
  esac
done

if [ -n "$unexpected" ]; then
  echo "image-licenses: FAIL — dists carrying NO licence file, and not a known exception:" >&2
  for d in $unexpected; do echo "    $d" >&2; done
  echo "  Either the image dropped their notices, or a new dependency ships none." >&2
  echo "  Do not silence this by extending KNOWN_MISSING without establishing why." >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

notices=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
echo "image-licenses: OK — $notices dists inventoried, all carry licence material"
echo "  (known exceptions, licence file absent upstream:$(for d in $KNOWN_MISSING; do printf ' %s' "$d"; done))"
