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
            printf 'git\t%s\tapt:debian-bookworm\n' "$(git --version | cut -d' ' -f3)"
            printf 'python\t%s\tdocker:python:%s\n' "${PYTHON_TAG%%-*}" "$PYTHON_TAG"
            printf 'uv\t%s\tdocker:ghcr.io/astral-sh/uv\n' "$UV_VERSION"
            printf 'bh\t%s\tpypi:beadhive[otel]\n' "$BEADHIVE_VERSION"
            printf 'bd\t%s\tgithub:gastownhall/beads\n' "$BD_VERSION"
            printf 'dolt\t%s\tgithub:dolthub/dolt\n' "$DOLT_VERSION"
            printf 'gh\t%s\tgithub:cli/cli\n' "$GH_VERSION"
            printf 'git-workspace\t%s\tcrates.io:git-workspace\n' "$GIT_WORKSPACE_VERSION"
            printf 'jq\t%s\tgithub:jqlang/jq\n' "$JQ_VERSION"
            printf 'yq\t%s\tgithub:mikefarah/yq\n' "$YQ_VERSION"
            printf 'just\t%s\tgithub:casey/just\n' "$JUST_VERSION"
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
    components=$(
        {
            printf 'node\t%s\tnodejs.org\n' "$NODE_VERSION"
            printf 'claude\t%s\tnpm:@anthropic-ai/claude-code\n' "$CLAUDE_CODE_VERSION"
            printf 'codex\t%s\tnpm:@openai/codex\n' "$CODEX_VERSION"
        } | to_components
    )
    jq --arg tag "$IMAGE_TAG" --argjson components "$components" \
        '.image.tag = $tag | .image.target = "agent" | .components += $components' \
        "$manifest" >"$manifest.new"
    mv "$manifest.new" "$manifest"
    ;;
*)
    echo "write-manifest: unknown tier '$tier' (expected core|agent)" >&2
    exit 1
    ;;
esac

chmod 0444 "$manifest"
