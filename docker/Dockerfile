# syntax=docker/dockerfile:1
#
# Beadhive in a box. Two targets from one file:
#
#   core   bh, bd, dolt, git, gh, git-workspace, jq, yq, just
#   agent  core + Node LTS + a pinned Claude Code + the Codex CLI
#
# NO VERSION LITERAL BELONGS IN THIS FILE — every pin is an ARG fed from docker-bake.hcl,
# which is the single pin source. Build with `docker buildx bake` (or `just image`), never
# `docker build`: a bare `docker build` leaves every ARG empty and the build fails on the
# first digest check.
#
# The image runs as the non-root user `bee`: both Claude Code and the Codex CLI refuse their
# in-container bypass-permission mode when running as root.

ARG PYTHON_TAG
ARG UV_DIGEST
ARG RUST_TAG

# ---- uv ------------------------------------------------------------------------------------
# The official distroless uv image is just the two binaries; copying them out beats a curl|sh
# installer, and pinning by index digest (not tag) makes the pin immovable. uv is pinned
# separately from Python on purpose — a uv:python* base would fuse the two into one bump.
FROM ghcr.io/astral-sh/uv@${UV_DIGEST} AS uv

# ---- git-workspace -------------------------------------------------------------------------
# Built from crates.io rather than fetched as a release binary: upstream publishes only a
# Linux x86_64 asset (never arm64), and its newest versions ship to crates.io only. Source is
# the one channel that is both arch-uniform and version-uniform, and `cargo install --locked`
# verifies the registry checksums. Bookworm here matches the runtime base so the resulting
# binary links against the same glibc/OpenSSL.
FROM rust:${RUST_TAG} AS git-workspace
ARG GIT_WORKSPACE_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates cmake libssl-dev pkg-config zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
# This stage compiles vendored C (libgit2, libssh2, ring). That is native and fine on the
# platform you are on, but a foreign-arch leg runs it under emulation, and QEMU crashes both
# gcc's cc1 and clang's integrated assembler on it — see the `image-cross` recipe for the fix.
RUN cargo install --locked --root /out git-workspace --version "${GIT_WORKSPACE_VERSION}"

# ---- core ------------------------------------------------------------------------------------
FROM python:${PYTHON_TAG} AS core

ARG TARGETARCH
ARG PYTHON_TAG
ARG UV_VERSION
ARG BEADHIVE_VERSION
ARG BD_VERSION
ARG BD_SHA256_AMD64
ARG BD_SHA256_ARM64
ARG DOLT_VERSION
ARG DOLT_SHA256_AMD64
ARG DOLT_SHA256_ARM64
ARG GIT_WORKSPACE_VERSION
ARG GH_VERSION
ARG GH_SHA256_AMD64
ARG GH_SHA256_ARM64
ARG JQ_VERSION
ARG JQ_SHA256_AMD64
ARG JQ_SHA256_ARM64
ARG YQ_VERSION
ARG YQ_SHA256_AMD64
ARG YQ_SHA256_ARM64
ARG JUST_VERSION
ARG JUST_SHA256_AMD64
ARG JUST_SHA256_ARM64
ARG IMAGE_TAG
ARG BUILD_SHA

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git less libssl3 openssh-client procps \
    && rm -rf /var/lib/apt/lists/*

# uv installs bh's wheels (seconds, no toolchain).
#   UV_TOOL_BIN_DIR   NOT uv's documented /root/.local/bin — this image runs as `bee`, who
#                     cannot read root's home, so that default builds clean then fails at
#                     runtime with "bh: command not found". /usr/local/bin is on every PATH.
#   UV_PYTHON_DOWNLOADS  keeps bh on the base image's CPython instead of provisioning a second,
#                     non-standard interpreter a container will never switch away from.
#   UV_LINK_MODE      the cache below is a BuildKit mount on another filesystem, where uv's
#                     default hardlinking cannot work. The mount also keeps the cache OUT of
#                     the image — this thing already carries Node plus six static binaries.
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_TOOL_DIR=/opt/uv/tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
# BEADHIVE_WHEEL selects the install SOURCE. Empty (the default) installs the released version
# from PyPI exactly as before, so an ordinary bake is byte-for-byte unchanged.
#
# Non-empty names a wheel path inside the build context and the image installs THAT. This exists
# because the proof gate (bh-pc2a.17) must verify behaviour that is not released yet — notably
# `bh setup check` reading /etc/beadhive/image-manifest.json (feat(setup) f8557ed), which cannot
# reach PyPI until this epic lands, which the gate gates. Without a local-source path that circle
# has no exit; docker-bake.hcl previously told readers to "--set it against a local build", a
# capability the build did not actually have.
#
# BIND-MOUNTED, not COPY'd: a COPY bakes the wheel into a layer, so the image would ship a
# redundant copy of its own distribution. The mount leaves nothing behind.
#
# It mounts the NAMED CONTEXT `wheelsrc`, not the build context, because the build context is
# ./docker (see docker-bake.hcl) — `dist/` is not inside it and never will be. The bake file
# defaults wheelsrc to ./docker so an ordinary build is unaffected; point it at ./dist to use a
# real wheel. Getting this wrong is easy and silent: the mount would succeed and the wheel
# simply would not be there.
ARG BEADHIVE_WHEEL=""
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETARCH},sharing=locked \
    --mount=type=bind,from=wheelsrc,target=/ctx,ro \
    if [ -n "${BEADHIVE_WHEEL}" ]; then \
        echo "bh source: local wheel ${BEADHIVE_WHEEL}"; \
        uv tool install "/ctx/${BEADHIVE_WHEEL}[otel]"; \
    else \
        echo "bh source: pypi beadhive[otel]==${BEADHIVE_VERSION}"; \
        uv tool install "beadhive[otel]==${BEADHIVE_VERSION}"; \
    fi

COPY --from=git-workspace /out/bin/git-workspace /usr/local/bin/git-workspace

RUN --mount=type=bind,source=fetch-tool.sh,target=/tmp/fetch-tool.sh <<'EOT'
set -eu
case "$TARGETARCH" in
    amd64) rust_arch=x86_64 ;;
    arm64) rust_arch=aarch64 ;;
    *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 1 ;;
esac
# Pick the digest for the arch being built; both are passed in as build args.
pick() { if [ "$TARGETARCH" = amd64 ]; then echo "$1"; else echo "$2"; fi; }
fetch() { sh /tmp/fetch-tool.sh "$@"; }

fetch bin \
    "https://github.com/gastownhall/beads/releases/download/v${BD_VERSION}/beads_${BD_VERSION}_linux_${TARGETARCH}.tar.gz" \
    "$(pick "$BD_SHA256_AMD64" "$BD_SHA256_ARM64")" /usr/local/bin/bd bd
fetch bin \
    "https://github.com/dolthub/dolt/releases/download/v${DOLT_VERSION}/dolt-linux-${TARGETARCH}.tar.gz" \
    "$(pick "$DOLT_SHA256_AMD64" "$DOLT_SHA256_ARM64")" /usr/local/bin/dolt "dolt-linux-${TARGETARCH}/bin/dolt"
fetch bin \
    "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${TARGETARCH}.tar.gz" \
    "$(pick "$GH_SHA256_AMD64" "$GH_SHA256_ARM64")" /usr/local/bin/gh "gh_${GH_VERSION}_linux_${TARGETARCH}/bin/gh"
fetch bin \
    "https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-${TARGETARCH}" \
    "$(pick "$JQ_SHA256_AMD64" "$JQ_SHA256_ARM64")" /usr/local/bin/jq
fetch bin \
    "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_${TARGETARCH}" \
    "$(pick "$YQ_SHA256_AMD64" "$YQ_SHA256_ARM64")" /usr/local/bin/yq
fetch bin \
    "https://github.com/casey/just/releases/download/${JUST_VERSION}/just-${JUST_VERSION}-${rust_arch}-unknown-linux-musl.tar.gz" \
    "$(pick "$JUST_SHA256_AMD64" "$JUST_SHA256_ARM64")" /usr/local/bin/just just
EOT

# The image's own statement of what was built together — `bh setup check` reads it instead of
# probing every `--version` on PATH.
RUN --mount=type=bind,source=write-manifest.sh,target=/tmp/write-manifest.sh \
    sh /tmp/write-manifest.sh core

# Non-root from here on, as a CONFIGURABLE user — so a Linux host that bind-mounts an area can
# match its own UID/GID — and EVERY path docker-compose.yml mounts a volume over is created
# here, owned by that user. Both halves are load-bearing:
#
#   configurable  a literal `bee` anywhere below would strand an AGENT_USER=worker build.
#   pre-created   the two cases are NOT symmetric. Docker copies an existing image directory's
#                 ownership into an empty named volume, but a mount point that does NOT exist
#                 in the image is created root-owned — so the agent user cannot write to a
#                 directory inside its own home, and the container is inert.
#
# Hence all four areas of the volume split, and every one derived from the ARGs: /workspace
# (GIT_WORKSPACE), /worktrees (BH_WORKTREES), ~/.beadhive (BH_HOME / BH_HQ), ~/.claude
# (CLAUDE_CONFIG_DIR). Keep this list in step with docker-compose.yml — tests/test_image_build.py
# cross-checks the two so a new mount point cannot land unowned.
#
# `useradd -d` states the home path explicitly rather than inheriting /etc/default/useradd's
# HOME, so the paths below cannot drift from where the account actually lives. `install -o/-g`
# takes the NUMERIC ids, which are valid at this point whether or not name lookup is.
ARG AGENT_USER
ARG AGENT_UID
ARG AGENT_GID

# The explicit signal bh reads to know it is running in the image (bh-pc2a.6 —
# compose.CONTAINER_MARKER). A baked ENV rather than bh sniffing /.dockerenv or /proc/1/cgroup:
# those differ across docker, podman, containerd and nerdctl and have changed shape between
# versions of each, so a detector built on them fails quietly on whichever runtime nobody tested.
# Being an env var also means `docker run -e BH_IN_CONTAINER=0` turns the behaviour off to debug.
#
# Declared in `core` only — `agent` is FROM core and inherits it.
#
# It must sit ABOVE the `install -d` below, not between that and USER: the mount-point contract
# test (tests/test_compose_volumes.py) parses everything from `install -d` to `\nUSER ` as the
# directory list, so anything in that gap is read as a path. That test caught this exact mistake.
#
# What it changes: bh refuses to drive a container runtime from in here (there is none, and the
# host's docker socket is deliberately unmounted), and dolt.backend defaults to `none`.
ENV BH_IN_CONTAINER=1

RUN groupadd -g "${AGENT_GID}" "${AGENT_USER}" \
    && useradd -m -d "/home/${AGENT_USER}" -u "${AGENT_UID}" -g "${AGENT_GID}" \
        -s /bin/bash "${AGENT_USER}" \
    && install -d -o "${AGENT_UID}" -g "${AGENT_GID}" \
        /workspace \
        /worktrees \
        "/home/${AGENT_USER}/.beadhive" \
        "/home/${AGENT_USER}/.claude"
USER ${AGENT_USER}
WORKDIR /workspace
CMD ["bash"]

# ---- agent -------------------------------------------------------------------------------
FROM core AS agent

ARG TARGETARCH
ARG NODE_VERSION
ARG NODE_SHA256_AMD64
ARG NODE_SHA256_ARM64
ARG CLAUDE_CODE_VERSION
ARG CODEX_VERSION
ARG IMAGE_TAG
ARG AGENT_USER

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=bind,source=fetch-tool.sh,target=/tmp/fetch-tool.sh <<'EOT'
set -eu
case "$TARGETARCH" in
    amd64) node_arch=x64; node_sha="$NODE_SHA256_AMD64" ;;
    arm64) node_arch=arm64; node_sha="$NODE_SHA256_ARM64" ;;
    *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 1 ;;
esac
sh /tmp/fetch-tool.sh tree \
    "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
    "$node_sha" /usr/local 1
EOT

# npm, NOT the devcontainer Feature: the Feature always installs latest and defeats pinning.
#
# CLAUDE CODE IS DELIBERATELY NOT INSTALLED HERE (bh-pc2a.36). Its package declares
# "SEE LICENSE IN README.md" rather than an SPDX identifier — it is proprietary, and baking it in
# would make anyone who publishes this image a redistributor of it under Anthropic's commercial
# terms. The image ships the RUNTIME (node) and the MEANS (`bh harness install claude`), so the
# user installs it themselves and accepts those terms as their own choice. Same category as
# repowise (AGPL) in bh-pc2a.21, with a stronger reason.
#
# codex stays: it declares a permissive SPDX licence (Apache), so it is freely redistributable.
# The rule is about PROPRIETARY components, and stretching it to cover a permissively-licensed one
# would cost users a working default for nothing. `bh harness install codex` overrides this pin.
#
# CLAUDE_CODE_VERSION survives as the DEFAULT the install verb uses — it is no longer a baked
# component, so do not re-add it to the manifest.
RUN npm install -g --no-fund --no-audit \
        "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && rm -rf /root/.npm

# The pinned claude version, readable at runtime by `bh harness install` so the image still names
# ONE validated version rather than letting `@latest` drift. An ENV, not an installed package.
ENV BH_CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}

# A user-writable npm global prefix, INSIDE the bh-harness volume. Two problems, one fix:
#
#   1. The container runs as ${AGENT_USER}, and /usr/local/lib/node_modules is root-owned, so a
#      runtime `npm install -g` dies with EACCES. Measured, not predicted:
#        npm error EACCES: permission denied, mkdir '/usr/local/lib/node_modules/@anthropic-ai'
#   2. A harness installed at runtime must SURVIVE a rebuild, or the user reinstalls it every
#      time. Anywhere outside the four volumes is discarded — the same silent-loss class as
#      CODEX_HOME (bh-pc2a.7) and GH_CONFIG_DIR (bh-pc2a.29), both already fixed this way.
#
# ~/.claude is the harness volume, which is exactly what a harness install is. Deliberately NOT
# pre-created in the `install -d` block above: the volume mounts OVER ~/.claude, so anything
# created there in the image is masked anyway. npm creates the prefix itself, and can, because
# ~/.claude is owned by the agent user.
ENV NPM_CONFIG_PREFIX=/home/${AGENT_USER}/.claude/npm-global
ENV PATH=/home/${AGENT_USER}/.claude/npm-global/bin:${PATH}

# ENV PATH alone is not enough: a LOGIN shell (`bash -l`, which is what `docker compose exec bh
# bash -l` and most interactive entry gives you) sources /etc/profile, which OVERWRITES PATH and
# discards the line above. Measured — `bash -lc 'echo $PATH'` came back without the npm prefix,
# so a freshly installed harness was invisible while `bh harness install` reported success.
# /etc/profile.d is the documented seam for putting it back.
RUN printf '%s\n' \
    '# Re-add the npm global prefix (bh-pc2a.36): /etc/profile resets PATH for login shells,' \
    '# which would hide any harness installed by `bh harness install`.' \
    'case ":$PATH:" in' \
    '  *":${NPM_CONFIG_PREFIX}/bin:"*) ;;' \
    '  *) PATH="${NPM_CONFIG_PREFIX}/bin:$PATH" ;;' \
    'esac' \
    'export PATH' \
    > /etc/profile.d/10-beadhive-npm-prefix.sh \
    && chmod 0644 /etc/profile.d/10-beadhive-npm-prefix.sh

# Fleet policy, highest precedence in Claude Code's settings hierarchy. Set in both places so a
# pinned CLI stays pinned however it is launched.
#
# DISABLE_AUTOUPDATER alone is NOT enough to hold a pin: per Anthropic's env-var reference it
# only stops the BACKGROUND update check, and manual `claude update` / `claude install` still
# work. DISABLE_UPDATES blocks every path, and is the documented setting for exactly this case —
# "distributing Claude Code through your own channels [where] users should not self-update".
# An image whose whole promise is a validated component set is that case.
COPY managed-settings.json /etc/claude-code/managed-settings.json
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1

RUN --mount=type=bind,source=write-manifest.sh,target=/tmp/write-manifest.sh \
    sh /tmp/write-manifest.sh agent

USER ${AGENT_USER}
WORKDIR /workspace
CMD ["bash"]
