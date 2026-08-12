#!/usr/bin/env bash
# Can a packed seat actually EXEC inside the built image? (bh-m4nn8)
#
# WHY THIS GATE EXISTS. The packed baml seats (`bh-developer` and friends) are what the `local`
# runtime tier spawns, and they are NOT part of the image — so `image-drift` and the image
# manifest, which compare what the image CONTAINS against what it claims, structurally cannot
# see them. Nothing asserted that the thing the image is supposed to run could be run by it.
#
# It could not. The seats are dynamically linked and require up to GLIBC_2.39; the image was
# bookworm at 2.36, so every seat died at exec:
#
#     bh-developer: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.39' not found
#
# That is a floor mismatch between two independently-versioned artifacts — baml's published pack
# host and Debian's libc — with no shared pin to keep them in step. A comment in docker-bake.hcl
# would not have caught it; the next base bump downward would reintroduce it silently.
#
# TWO CHECKS, DELIBERATELY. The first needs no seat binary and always runs; the second is the
# real proof and needs one.
#
#   1. FLOOR   The image's glibc is >= the floor below. Cheap, hermetic, and it fails on a base
#              downgrade even on a machine with no packed seats to hand.
#   2. EXEC    A real packed seat runs `--help` inside the image and exits 0. This is the actual
#              acceptance criterion; it SKIPS loudly when no seat binary is available rather
#              than passing quietly, matching proof-gate.sh's rule that a gate reading green
#              with checks silently absent is worse than no gate.
#
# Usage:  scripts/image-glibc-floor.sh [image-ref] [seat-binary]
#           image-ref     default beadhive/agent:dev
#           seat-binary   default $BH_SEAT_BINARY, else dist/bh-developer if it exists
# Exit:   0 pass (or pass-with-skip) · 1 a check FAILED · 2 nothing to test (no image)
set -uo pipefail

IMAGE=${1:-beadhive/agent:dev}
SEAT=${2:-${BH_SEAT_BINARY:-dist/bh-developer}}

# The highest GLIBC_* version symbol the packed seats reference, measured with
#   objdump -T dist/bh-developer | grep -o 'GLIBC_[0-9.]*' | sort -Vu | tail -1
# Raise this ONLY together with a base image that satisfies it, and record the measurement.
REQUIRED_GLIBC=2.39

fail() { printf '  ✗ %s\n' "$1" >&2; FAILED=1; }
pass() { printf '  ✓ %s\n' "$1"; }
skip() { printf '  – %s (skipped: %s)\n' "$1" "$2"; }
FAILED=0

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "image-glibc-floor: no image '$IMAGE' — bake one first: just image-local" >&2
    exit 2
fi

echo "glibc floor — $IMAGE (packed seats need >= GLIBC_${REQUIRED_GLIBC})"

# ---- 1. the floor ---------------------------------------------------------------------------
# `ldd --version` rather than parsing libc.so.6: it is the one form present on every Debian
# release this image has ever been built on, and it reports the runtime's own version.
have=$(docker run --rm --entrypoint sh "$IMAGE" -c 'ldd --version 2>&1 | head -1' 2>/dev/null |
    grep -oE '[0-9]+\.[0-9]+$')
if [ -z "$have" ]; then
    fail "could not read the image's glibc version"
elif [ "$(printf '%s\n%s\n' "$REQUIRED_GLIBC" "$have" | sort -V | head -1)" = "$REQUIRED_GLIBC" ]; then
    pass "image glibc ${have} >= ${REQUIRED_GLIBC}"
else
    fail "image glibc ${have} is BELOW the packed seats' floor ${REQUIRED_GLIBC} —
      every seat the local runtime tier spawns will die at exec with
      \"version \`GLIBC_${REQUIRED_GLIBC}' not found\". Raise PYTHON_TAG's Debian
      release in docker-bake.hcl (trixie is 2.41); do not lower this floor."
fi

# ---- 2. a real seat, really exec'd ------------------------------------------------------------
# Mounted read-only at a fixed path rather than baked in: the seats are not image components and
# giving them one here would make them look like components to everything that reads the image.
if [ ! -x "$SEAT" ]; then
    skip "packed seat execs in the image" \
        "no seat binary at '$SEAT' — pass one, or set BH_SEAT_BINARY (baml-harness: just pack)"
elif out=$(docker run --rm -v "$(cd "$(dirname "$SEAT")" && pwd)/$(basename "$SEAT")":/tmp/seat:ro \
    --entrypoint /tmp/seat "$IMAGE" --help 2>&1); then
    pass "packed seat $(basename "$SEAT") exec'd inside the image"
else
    fail "packed seat $(basename "$SEAT") could NOT exec inside the image:
      $(printf '%s' "$out" | head -3)"
fi

exit "$FAILED"
