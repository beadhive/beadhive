# syntax=docker/dockerfile:1
# check=skip=InvalidDefaultArgInFrom
#
# THE TWO LINES ABOVE ARE PARSER DIRECTIVES AND MUST STAY AT THE VERY TOP, ADJACENT, WITH NO
# COMMENT OR BLANK LINE BEFORE OR BETWEEN THEM. Anything else ends the directive section and the
# directive silently becomes an ordinary comment — it still LOOKS right while doing nothing.
# (Measured: an explanatory comment placed above `check=` left all three warnings firing.)
#
# Why the check is skipped rather than satisfied: the linter reads each ARG's DEFAULT, and by
# design there is none — see the single-pin-source rule below. The warning therefore fires even
# on a correct bake, and the only way to satisfy it is to add defaults, i.e. to create exactly
# the second pin source that rule forbids and that
# tests/test_image_build.py::test_no_version_literal_in_the_dockerfile enforces. (bh-xoaw)
#
# Beadhive in a box. Two targets from one file:
#
#   core   bh, bd, dolt, git, gh, git-workspace, jq, yq, just
#   agent  core + harness POLICY — no harness, and after bh-lnrn no node either
#
# The agent target ships NO agent harness at all, for two different reasons that both still hold.
# Claude Code (bh-pc2a.36) declares "SEE LICENSE IN README.md" rather than an SPDX identifier, so
# baking it would make anyone who publishes this image a redistributor of proprietary software.
# Codex (bh-lnrn) declares a permissive SPDX licence (Apache) and is freely redistributable — and
# is excluded anyway, by decision: the image carries the runtime and the MEANS, the user installs
# the harness themselves. Node left with codex, which was its only consumer. Full reasoning sits
# in the agent stage.
#
# NO VERSION LITERAL BELONGS IN THIS FILE — every pin is an ARG fed from docker-bake.hcl,
# which is the single pin source. Build with `docker buildx bake` (or `just image`), never
# `docker build`: a bare `docker build` leaves every ARG empty and the build fails on the
# first digest check.
#
# The image runs as the non-root user `bees`: the Codex CLI, and Claude Code once a user
# installs it, both refuse their in-container bypass-permission mode when running as root.

ARG NIX_TAG
ARG PYTHON_TAG
ARG UV_DIGEST

# ---- toolchain ------------------------------------------------------------------------------
# flake.nix is the ONE definition of this toolchain (bh-8b8o.1). It already existed for the
# local-install plane; the image used to hand-fetch the same seven tools from GitHub releases with
# a version and two SHA256s each, which is the same toolchain maintained twice and drifting —
# bh-q160.4 is what that drift looks like in production: the image shipped exactly the bd release
# bh's own advisory flags for the dolt-pull hang. `beadsHead` in the flake is already the fix, so
# this closes it by construction rather than by another pin bump.
#
# A BUILDER STAGE, not dockerTools.buildLayeredImage. The nix build runs INSIDE the docker build,
# so buildx/colima supplies the Linux VM and `just image` still works on a Mac with no nix on it.
# dockerTools would need a nix-darwin linux-builder VM to emit a Linux image — a VM added to avoid
# one already present — and would mean re-expressing the configurable UID/GID useradd, the
# pre-created mount points and /etc/profile.d as nix expressions: a rewrite of the one thing whose
# failure mode is a container that comes up healthy and inert.
#
# It builds `.#image`, NOT `.#default`: the image set is the local-install set MINUS git (copyleft,
# and it arrives via the base image's apt where the licence gate scopes it out) and MINUS uv
# (already copied by index digest below), PLUS jq and yq-go. That derivation lives in flake.nix
# beside the list it derives from, so there is no second list here to drift.
#
# `path:/src` rather than a git ref: the flake is bind-mounted from the `flakesrc` named context
# (the build context is ./docker, and flake.nix is not inside it — same reason the wheel uses a
# named context). Nothing is written back, hence ro.
FROM nixos/nix:${NIX_TAG} AS toolchain
RUN --mount=type=bind,from=flakesrc,target=/src,ro <<'EOT'
set -eu
export NIX_CONFIG="experimental-features = nix-command flakes"
nix build "path:/src#image" --out-link /tmp/out
mkdir -p /closure/opt/toolchain
# The closure first, then the buildEnv's own tree. `cp -a` preserves bin/* as symlinks into
# /nix/store, which is why /nix has to come across too and why this is not just a bin/ copy.
nix-store -qR /tmp/out | xargs -I{} cp -a --parents {} /closure
cp -a /tmp/out/. /closure/opt/toolchain/
EOT

# ---- uv ------------------------------------------------------------------------------------
# The official distroless uv image is just the two binaries; copying them out beats a curl|sh
# installer, and pinning by index digest (not tag) makes the pin immovable. uv is pinned
# separately from Python on purpose — a uv:python* base would fuse the two into one bump.
FROM ghcr.io/astral-sh/uv@${UV_DIGEST} AS uv

# git-workspace HAD ITS OWN rust: STAGE HERE, compiling from crates.io because upstream publishes
# no arm64 release asset. It is gone (bh-8b8o.1): nixpkgs carries git-workspace prebuilt, so the
# toolchain stage substitutes it from cache.nixos.org instead of building it.
#
# That deleted a whole failure class, not just a stage. The stage compiled vendored C (libgit2,
# libssh2, ring), and a foreign-arch leg ran that under emulation where QEMU crashes both gcc's
# cc1 and clang's integrated assembler — the reason `image-cross` needs its own builder. A
# substituted binary is not compiled at all, on either arch.

# ---- core ------------------------------------------------------------------------------------
FROM python:${PYTHON_TAG} AS core

ARG TARGETARCH
ARG PYTHON_TAG
ARG UV_VERSION
ARG BEADHIVE_VERSION
ARG IMAGE_TAG
ARG BUILD_SHA

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git less libssl3 openssh-client procps \
    && rm -rf /var/lib/apt/lists/*

# uv installs bh's wheels (seconds, no toolchain).
#   UV_TOOL_BIN_DIR   NOT uv's documented /root/.local/bin — this image runs as `bees`, who
#                     cannot read root's home, so that default builds clean then fails at
#                     runtime with "bh: command not found". /usr/local/bin is on every PATH.
#   UV_PYTHON_DOWNLOADS  keeps bh on the base image's CPython instead of provisioning a second,
#                     non-standard interpreter a container will never switch away from.
#   UV_LINK_MODE      the cache below is a BuildKit mount on another filesystem, where uv's
#                     default hardlinking cannot work. The mount also keeps the cache OUT of
#                     the image — this thing already carries six static binaries.
COPY --from=uv /uv /uvx /usr/local/bin/

# The nix-supplied toolchain: bd, dolt, gh, git-workspace, jq, just, yq. /nix must land too —
# every /opt/toolchain/bin entry is a symlink into the store.
COPY --from=toolchain /closure/nix /nix
COPY --from=toolchain /closure/opt/toolchain /opt/toolchain
ENV PATH=/opt/toolchain/bin:${PATH}

# ENV PATH ALONE IS NOT ENOUGH, and this file has now paid for that lesson twice. A LOGIN shell
# (`docker compose exec bh bash -l`, and most interactive entry) sources /etc/profile, which
# OVERWRITES PATH and discards the line above. Measured on the first build of this stage:
#
#   sh -c    'echo $PATH'  → /opt/toolchain/bin:/usr/local/bin:…   bd resolves
#   bash -lc 'echo $PATH'  → /usr/local/bin:/usr/bin:/bin:…        bd, dolt, gh, jq, yq, just and
#                                                                  git-workspace ALL missing
#
# The build still SUCCEEDED, which is the part worth remembering: every RUN uses `/bin/sh -c`
# (non-login) and so kept the ENV PATH, so the image was assembled by a shell that could see the
# toolchain and shipped for one that could not. bh-pc2a.36 hit this with the npm prefix and
# bh-dy4g hit it with ~/.local; same seam, applied a layer earlier because this PATH belongs to
# core rather than to a harness.
RUN printf '%s\n' \
    '# Re-add the nix toolchain (bh-8b8o.1): /etc/profile resets PATH for login shells, which' \
    '# would hide bd, dolt, gh, git-workspace, jq, yq and just from every interactive session.' \
    'case ":$PATH:" in' \
    '  *":/opt/toolchain/bin:"*) ;;' \
    '  *) PATH="/opt/toolchain/bin:$PATH" ;;' \
    'esac' \
    'export PATH' \
    > /etc/profile.d/09-beadhive-toolchain-path.sh \
    && chmod 0644 /etc/profile.d/09-beadhive-toolchain-path.sh

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
# bh IS INSTALLED LAST ON PURPOSE (bh-41tj) — it is the most volatile input in the stage, and a
# cache miss invalidates every layer after it. Installed before the fetch above, a one-line change
# to bh source discarded all six third-party downloads (dolt alone is tens of MB) on every
# `just image-local`. Stable first, volatile last, per Docker's own ordering guidance.
#
# It stays ABOVE write-manifest.sh, which is not stylistic: the manifest records the installed bh's
# version by running `bh --version`, so it must follow the install. Nothing between the fetch and
# here depends on bh, so the move is safe.
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

# The image's own statement of what was built together — `bh setup check` reads it instead of
# probing every `--version` on PATH.
RUN --mount=type=bind,source=write-manifest.sh,target=/tmp/write-manifest.sh \
    sh /tmp/write-manifest.sh core

# Non-root from here on, as a CONFIGURABLE user — so a Linux host that bind-mounts an area can
# match its own UID/GID — and EVERY path docker-compose.yml mounts a volume over is created
# here, owned by that user. Both halves are load-bearing:
#
#   configurable  a literal `bees` anywhere below would strand an AGENT_USER=worker build.
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

ARG CLAUDE_CODE_VERSION
ARG CODEX_VERSION
ARG IMAGE_TAG
ARG AGENT_USER

USER root

# NEITHER HARNESS IS SHIPPED (bh-lnrn). Claude Code was already excluded (bh-pc2a.36): its package
# declares "SEE LICENSE IN README.md" rather than an SPDX identifier, so baking it would make
# anyone who publishes this image a redistributor of it under Anthropic's commercial terms. Codex
# declares a permissive SPDX licence (Apache) and is freely redistributable — and is excluded
# anyway, by decision. The
# image ships the RUNTIME and the MEANS, never the harness. One rule with no "except the
# permissive one" clause is the rule that survives the next harness.
#
# NODE WENT WITH IT, and that is why this stage shrank rather than just lost a line. bh-hsus.1 had
# already moved `bh dep install` off npm — claude installs via its own installer, and codex's
# route names brew / a GitHub release / nixpkgs#codex, none of them npm. The baked
# `npm install -g @openai/codex` was node's LAST consumer, so removing codex left node with none.
# Retired together: the Node LTS tarball, the xz-utils apt layer, NPM_CONFIG_PREFIX, and
# /etc/profile.d/10-beadhive-npm-prefix.sh. The PATH problem that workaround existed for is real
# and did NOT retire — bh-dy4g's ~/.local redirect below now serves it, for the native installers
# both harnesses actually use.
#
# THE TWO PINS SURVIVE AS ENV, not as installed packages, so the image still names ONE validated
# version for `bh dep install` to bootstrap to rather than letting `latest` drift. They are not
# components — nothing here is redistributed — which is why write-manifest.sh records neither and
# why both are exempt from the licence allowlist in docker-bake.hcl.
ENV BH_CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}
ENV BH_CODEX_VERSION=${CODEX_VERSION}

# THE NATIVE INSTALL TARGET (bh-dy4g). bh-hsus.1 moved `bh dep install claude` off npm and onto
# claude's own installer, which writes under ~/.local — and ~/.local is neither a volume nor on
# PATH, so a harness installed at runtime was invisible AND discarded on recreate. Measured on
# beadhive/agent:dev before this line existed:
#
#   bash -lc 'echo $PATH'  → /home/bee/.claude/npm-global/bin:/usr/local/bin:/usr/bin:/bin:…
#   ls -d ~/.local/bin     → No such file or directory        (and not on PATH)
#
# Nothing failed loudly because the npm prefix — now gone with node (bh-lnrn) — happened to be
# volume-backed and on PATH, so the ONE surviving harness route masked the native one. That is
# why bh-dy4g landed FIRST: removing the mask before fixing what it hid would have shipped an
# image where installing a harness silently did nothing.
#
# The npm prefix solved the same two problems this does, and its reasoning carries over intact:
#
#   1. A harness installed at runtime must SURVIVE a recreate, or the user reinstalls it every
#      time. Anywhere outside the four volumes is discarded — the same silent-loss class as
#      CODEX_HOME (bh-pc2a.7) and GH_CONFIG_DIR (bh-pc2a.29), both already fixed this way.
#   2. It must be on PATH for a LOGIN shell (`docker compose exec bh bash -l`), which sources
#      /etc/profile and OVERWRITES whatever `ENV PATH` set.
#
# THE SYMLINK, not a fifth volume. ~/.claude IS the harness volume — a harness binary is exactly
# what belongs in it — so ~/.local redirects there rather than adding a mount point to
# docker-compose.yml, the `install -d` block, and test_compose_volumes.py for one directory.
# The link itself lives OUTSIDE any mount (so it survives in the image layer) while its target
# resolves INSIDE the harness volume (so the installed binary survives a recreate). The
# installer's own `mkdir -p` creates the target through the link, and can, because the volume
# inherits ~/.claude's agent-user ownership from the `install -d` block above.
#
# NOT verified here: the installer's exact prefix under ~/.local. Confirming it means installing
# proprietary software, which is the one thing this image refuses to do on the user's behalf —
# and it does not change the fix, since both failures hold for any path under ~/.local.
RUN ln -s "/home/${AGENT_USER}/.claude/local" "/home/${AGENT_USER}/.local"
ENV PATH=/home/${AGENT_USER}/.local/bin:${PATH}

RUN printf '%s\n' \
    '# Re-add the native harness bin dir (bh-dy4g): /etc/profile resets PATH for login shells,' \
    '# which would hide a harness installed by `bh dep install`. ~/.local redirects into the' \
    '# ~/.claude volume, so what lands here survives a container recreate.' \
    'case ":$PATH:" in' \
    '  *":$HOME/.local/bin:"*) ;;' \
    '  *) PATH="$HOME/.local/bin:$PATH" ;;' \
    'esac' \
    'export PATH' \
    > /etc/profile.d/11-beadhive-harness-path.sh \
    && chmod 0644 /etc/profile.d/11-beadhive-harness-path.sh

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
