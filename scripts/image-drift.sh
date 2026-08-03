#!/usr/bin/env bash
# Report skew between the locally-baked images and the working tree (bh-pc2a.33).
#
# WHY THIS EXISTS. `just image-local` used to default to `core` alone, so core could be rebaked
# from the working tree while agent silently kept an older layer. That happened: agent sat at bh
# 0.6.0 — predating the manifest reader — while core was 0.7.1. Nothing surfaced it. Both images
# read `:dev`, and `bh --version` only differs if you go looking. The symptom was a stale agent
# image reporting `✗ missing: docker`, which reads as a host problem and sends the operator toward
# installing docker in the container or mounting the socket — exactly what bh-pc2a.6 forbids.
#
# The default is fixed, so this is the backstop for the case the default cannot cover: a bake of
# one target, an interrupted build, or an image carried over from an earlier checkout.
#
# EXIT CODES
#   0  the images agree with each other (they may still be behind HEAD — see below)
#   1  the images DISAGREE with each other, i.e. they are not from one build
#   2  nothing to compare — no images present (NOT a failure: nothing has been baked yet)
#
# Being behind HEAD is reported but does NOT fail. It is the normal mid-session state — you edit,
# then bake — so failing on it would make this check unrunnable in the one place it belongs, and a
# check people learn to skip detects nothing. Images disagreeing with EACH OTHER is never normal,
# and that is what fails.
set -uo pipefail

IMAGES=("${@:-}")
[ -z "${IMAGES[0]:-}" ] && IMAGES=(beadhive/core:dev beadhive/agent:dev)

HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"

# `docker image inspect` first: it fails fast and quietly on an absent image, where `docker run`
# would try to PULL one (and these images are never published, so that is a slow 404).
present=() ; rows=() ; versions=() ; shas=()
for img in "${IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 || continue
    line=$(docker run --rm --entrypoint jq "$img" -r \
        '[(.components[]|select(.name=="bh")|.version), .image.build_sha,
          (.components[]|select(.name=="bh")|.source)] | @tsv' \
        /etc/beadhive/image-manifest.json 2>/dev/null) || continue
    [ -z "$line" ] && continue
    present+=("$img") ; rows+=("$img	$line")
    versions+=("$(cut -f1 <<<"$line")") ; shas+=("$(cut -f2 <<<"$line")")
done

if [ ${#present[@]} -eq 0 ]; then
    echo "image-drift: no baked images found (${IMAGES[*]}) — nothing to compare."
    echo "  bake one first: just image-local"
    exit 2
fi

printf 'working tree HEAD: %s\n\n' "$HEAD_SHA"
printf '%-22s %-9s %-42s %s\n' IMAGE BH BUILD_SHA SOURCE
printf '%s\n' "${rows[@]}" | awk -F'\t' '{printf "%-22s %-9s %-42s %s\n", $1, $2, $3, $4}'
echo

rc=0

# 1. The images must agree with EACH OTHER. This is the failure that actually bit: two images
#    that are supposed to be one build, silently a release apart.
#
#    BOTH fields are compared, not just the version. Version catches the case that bit (0.6.0 vs
#    0.7.1), but two images baked from different COMMITS are not one build even when the version
#    string happens to match — which is the common case, since the version only moves on a release
#    while the code moves constantly. Comparing build_sha is what makes this check actually bite.
if [ "$(printf '%s\n' "${versions[@]}" | sort -u | wc -l)" -gt 1 ]; then
    echo "✗ images disagree on bh VERSION — they are not from one build."
    echo "  rebake both:  just image-local"
    rc=1
elif [ "$(printf '%s\n' "${shas[@]}" | sort -u | wc -l)" -gt 1 ]; then
    echo "✗ images were built from different COMMITS — they are not one build:"
    for i in "${!present[@]}"; do printf '    %-22s %s\n' "${present[$i]}" "${shas[$i]:0:12}"; done
    echo "  rebake both:  just image-local"
    rc=1
fi

# 2. Each image against the checkout — INFORMATIONAL ONLY, never sets rc. Behind HEAD is the
#    normal mid-session state, and a check that fails on normal is a check people stop running.
stale=0
for i in "${!present[@]}"; do
    if [ "$HEAD_SHA" != "unknown" ] && [ "${shas[$i]}" != "$HEAD_SHA" ]; then
        echo "• ${present[$i]} built from ${shas[$i]:0:12}, HEAD is ${HEAD_SHA:0:12} — rebake to test current code"
        stale=1
    fi
done

if [ $rc -eq 0 ] && [ $stale -eq 0 ]; then
    echo "✓ images agree with each other and with the working tree."
elif [ $rc -eq 0 ]; then
    echo "✓ images agree with each other."
fi
exit $rc
