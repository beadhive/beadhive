#!/usr/bin/env bash
# Guard image-vs-RELEASE drift (bh-pee6m): does a published `:dev` image's manifest bh version
# lag the version this checkout is at (or is exactly tagged as)?
#
# WHY THIS IS A DIFFERENT CHECK FROM `just image-drift`. That one compares the LOCAL core/agent
# images against each other and against HEAD, and deliberately does not fail on "behind HEAD" —
# mid-session, between edit and rebake, that is the normal state. This check is for the OTHER
# case: a `:dev` tag that has already been PUSHED and that other consumers (beadhive-app,
# briancripe/qm) pull expecting it to track the release. That image sat at bh 0.7.1 against a
# main at 0.11.3 — four minors behind — and nothing looked. `bh --version` inside the container
# was the only place it showed.
#
# WHAT COUNTS AS "BEHIND": bh's own PEP 440 version ordering, not string equality — comparing
# "0.7.1" against "0.11.3" as strings gets the wrong answer (7 > 1). `sort -V` (GNU version
# sort) orders them correctly without adding a dependency.
#
# ONLY FAILS ON A PYPI-SOURCED IMAGE. A `local-wheel:` build is a hand-baked dev artifact whose
# version is expected to differ from the release — that path is `image-drift`'s job, which
# compares it against the checkout's git SHA instead. This script's failure is specifically:
# "an image built from a PyPI release is behind the release this checkout claims."
#
# EXIT CODES
#   0  image bh version >= the checked-out/released version (or a local-wheel build: N/A)
#   1  the image is BEHIND — a published :dev tag consumers pull is stale
#   2  nothing to compare — image not present locally, manifest unreadable, or the checked-out
#      version itself could not be determined (NOT a failure of the guard being checked)
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${1:-beadhive/core:dev}"

# The version THIS checkout claims to be — release-pin.sh already refuses a tag that lies about
# pyproject.toml, so "the released or checked-out beadhive version" is one call, not two.
expected="$("$root/scripts/release-pin.sh" 2>&1)"
rc=$?
if [ $rc -ne 0 ]; then
    echo "image-release-drift: could not determine the checked-out beadhive version:" >&2
    echo "  ${expected}" >&2
    exit 2
fi

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "image-release-drift: no local '$IMAGE' image found — nothing to compare."
    echo "  pull it first, or bake one: just image-local"
    exit 2
}

line=$(docker run --rm --entrypoint jq "$IMAGE" -r \
    '[(.components[]|select(.name=="bh")|.version), (.components[]|select(.name=="bh")|.source)] | @tsv' \
    /etc/beadhive/image-manifest.json 2>/dev/null)
if [ -z "$line" ]; then
    echo "image-release-drift: could not read a bh version from $IMAGE's image-manifest.json." >&2
    exit 2
fi
image_version=$(cut -f1 <<<"$line")
source=$(cut -f2 <<<"$line")

if [ "$source" != "pypi:beadhive[otel]" ]; then
    echo "image-release-drift: $IMAGE's bh ($image_version) came from $source, not PyPI —"
    echo "  a local-wheel build is expected to differ from a release; not this check's job"
    echo "  (that's 'just image-drift', comparing it against the checkout's git SHA instead)."
    exit 0
fi

lowest=$(printf '%s\n%s\n' "$image_version" "$expected" | sort -V | head -1)
if [ "$lowest" = "$image_version" ] && [ "$image_version" != "$expected" ]; then
    echo "✗ $IMAGE carries bh $image_version — behind the checked-out/released version $expected." >&2
    echo "  A published :dev tag lagging the release is exactly what consumers (beadhive-app," >&2
    echo "  briancripe/qm) pull and read as current. Rebuild and republish the image." >&2
    exit 1
fi

echo "✓ $IMAGE carries bh $image_version — not behind $expected."
exit 0
