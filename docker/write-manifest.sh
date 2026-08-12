#!/bin/sh
# Write /etc/beadhive/image-manifest.json — the image's own statement of which component
# versions were built together, and under which tag + commit.
#
#   write-manifest.sh core     writes the manifest from the core stage's build args
#   write-manifest.sh agent    appends the agent components and re-tags the manifest
#
# `bh setup check` reads this file instead of shelling out to every `--version` on PATH, so
# the in-container gate reports the VALIDATED set rather than whatever happens to be installed.
# The agent stage amends rather than regenerates, because Docker does not carry the core
# stage's ARGs into a stage that derives FROM it.
set -eu

tier="${1:?usage: write-manifest.sh core|agent}"
manifest=/etc/beadhive/image-manifest.json

# name<TAB>version<TAB>source rows -> [{name, version, source}, …]
to_components() {
    jq -Rn '[inputs | split("\t") | {name: .[0], version: .[1], source: .[2]}]'
}

case "$tier" in
core)
    mkdir -p /etc/beadhive
    components=$(
        {
            # git comes from the base image's apt, so its version is a property of
            # PYTHON_TAG rather than a pin of its own — read it rather than assert it.
            #
            # THE CODENAME IS READ TOO (bh-m4nn8). It used to be the literal `debian-bookworm`,
            # which went stale the moment the base moved to trixie for the packed seats' glibc
            # floor — a row asserting the wrong provenance, in the very file `bh setup check`
            # trusts INSTEAD of probing. Both halves are now facts about the running image.
            printf 'git\t%s\tapt:debian-%s\n' \
                "$(git --version | cut -d' ' -f3)" \
                "$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-unknown}")"
            printf 'python\t%s\tdocker:python:%s\n' "${PYTHON_TAG%%-*}" "$PYTHON_TAG"
            printf 'uv\t%s\tdocker:ghcr.io/astral-sh/uv\n' "$UV_VERSION"
            # bh is the ONE component whose source can vary: BEADHIVE_WHEEL (bh-pc2a.25) swaps
            # the PyPI install for a locally-built wheel so the proof gate can exercise
            # unreleased behaviour. The manifest must say WHICH — an image built from a local
            # wheel but labelled as a released PyPI version is worse than an unlabelled one,
            # because `bh setup check` and everything downstream would trust the claim.
            # The version is read from the INSTALLED bh, not from the pin, so a local build
            # cannot report a version it was not built from.
            if [ -n "${BEADHIVE_WHEEL:-}" ]; then
                printf 'bh\t%s\tlocal-wheel:%s\n' \
                    "$(bh --version 2>/dev/null | head -1)" "$(basename "$BEADHIVE_WHEEL")"
            else
                printf 'bh\t%s\tpypi:beadhive[otel]\n' "$BEADHIVE_VERSION"
            fi
            # READ FROM NIXPKGS, NOT FROM A PIN AND NOT FROM `--version` (bh-8b8o.2). These seven
            # arrive in the nix closure flake.nix defines, so there is no *_VERSION build arg left
            # to quote — and bh-8b8o.1's first answer, running each binary and parsing its output,
            # meant seven different formats and got two of them wrong on the first build:
            #
            #   bd  ->  "bd version 1.1.0 (dev)"   recorded whole, prefix and all
            #   yq  ->  "v4.53.3"                  recorded with a leading v the others lack
            #
            # flake.nix now emits name/package/version/spdx for exactly this set, so the version
            # is nixpkgs' own field rather than something recovered from prose. Same data the
            # licence gate reads, which is the point: one export, one truth, two consumers.
            #
            # `nix:<package>` as the source, not `github:<repo>`: nixpkgs is where the pin lives
            # now (flake.lock), so naming upstream would point at a repo this image never fetched.
            # bd's package reads `beads` because that IS the nixpkgs attribute it overrides.
            jq -r '.[] | [.name, .version, "nix:" + .package] | @tsv' \
                /etc/beadhive/toolchain-metadata.json
        } | to_components
    )
    jq -n \
        --arg tag "$IMAGE_TAG" \
        --arg sha "$BUILD_SHA" \
        --argjson components "$components" \
        '{schema: 1, image: {tag: $tag, target: "core", build_sha: $sha}, components: $components}' \
        >"$manifest"
    ;;
agent)
    # THE AGENT TIER ADDS NO COMPONENTS (bh-lnrn). It recorded node and codex; neither is shipped
    # any more — codex by decision, node because removing codex left it with no consumer. What is
    # left in this stage is harness POLICY (managed-settings.json, the two BH_*_VERSION bootstrap
    # defaults, DISABLE_UPDATES), and policy is configuration, not a redistributed component.
    #
    # So this re-tags and does not append. The rule it is obeying is bh-pc2a.36's, unchanged:
    # listing a component the image does not ship would make `bh setup check` report a tool that
    # is not there — a lie the in-image path structurally cannot catch, because it trusts this
    # manifest INSTEAD of probing. An empty append would be equally correct and would read as an
    # oversight; saying nothing, with the reason, does not.
    jq --arg tag "$IMAGE_TAG" \
        '.image.tag = $tag | .image.target = "agent"' \
        "$manifest" >"$manifest.new"
    mv "$manifest.new" "$manifest"
    ;;
*)
    echo "write-manifest: unknown tier '$tier' (expected core|agent)" >&2
    exit 1
    ;;
esac

chmod 0444 "$manifest"
