# Beadhive container image — the SINGLE source of truth for every pinned component version.
#
# Build interface is `docker buildx bake` (no raw `docker build`, no Makefile wrapper, no
# platform list in a CI workflow). Two targets from one Dockerfile:
#
#   core   bh, bd, dolt, git, gh, git-workspace, jq, yq, just — the scheduler / HQ-sync node
#   agent  core + harness POLICY; no harness is shipped (bh-lnrn) — the default
#
# LOCAL BAKE IS THE PATH OF RECORD — registry publishing is deferred, so every consumer
# (laptop and future factory host alike) builds from this file:
#
#   just image          native arch only, --load'ed into the local image store
#   just image-cross    linux/amd64 + linux/arm64 (cannot --load; see the recipe)
#   docker buildx bake  builds the `default` group (core + agent), both platforms
#
# Override any pin for one invocation without editing this file:
#
#   docker buildx bake agent --set agent.args.CLAUDE_CODE_VERSION=2.1.221
#
# Every variable also takes its value from an environment variable of the same name, which is
# how the justfile injects BUILD_SHA.
#
# THE TOOLCHAIN IS NO LONGER PINNED HERE (bh-8b8o.1). bd, dolt, gh, git-workspace, jq, yq and just
# each needed a version plus two per-arch SHA256s — twenty-one variables and a fetch script that
# refused a mismatch. They now arrive in the nix closure `flake.nix` defines, pinned by
# `flake.lock`. That is a stronger guarantee than the digests were, not a weaker one: it fixes the
# whole dependency graph rather than seven tarballs. To bump one, move nixpkgs in flake.lock.
#
# What remains pinned here still needs keeping in step by hand: UV_DIGEST is an index digest, and
# PYTHON_TAG / NIX_TAG name base images.

# ---- image identity ---------------------------------------------------------------------

variable "REGISTRY" { default = "beadhive" }
variable "TAG" { default = "dev" }

# Commit that produced the image; recorded in the image manifest and the OCI revision label.
variable "BUILD_SHA" { default = "unknown" }

# ---- base images ------------------------------------------------------------------------

# Debian TRIXIE for the runtime. The git-workspace builder stage that used to constrain this is
# gone (bh-8b8o.1) — nixpkgs supplies it prebuilt, so there is no longer a second Debian release
# to keep in step.
#
# THE DEBIAN RELEASE IS A GLIBC FLOOR, NOT A STYLE CHOICE (bh-m4nn8). The packed baml seats the
# `local` runtime tier spawns are dynamically linked and `objdump -T` shows them requiring up to
# GLIBC_2.39. Bookworm is 2.36, so `bh-developer` inside the image died on
#
#     /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found
#
# and the whole tier was unrunnable there. Trixie is 2.41, which clears it — verified by a live
# seat turn returning a full typed result. Rebuilding the seats elsewhere does NOT help: `baml
# pack` downloads a matching prebuilt release "pack host" ELF rather than compiling one, so the
# floor comes from baml's published artifact and not from the packing machine. The static escape
# (`--target x86_64-unknown-linux-musl`) would remove the floor entirely and currently fails in
# the pack host's ELF writer; revisit when that is fixed upstream.
#
# Python patch level is UNCHANGED across this move (3.12.13 either way) so the only variable that
# moved is the Debian release. `scripts/image-glibc-floor.sh` is the gate that stops it drifting
# back down; `just image-seat-exec` runs a real packed seat inside the built image.
#
# python:*-slim is a convenient source of a maintained CPython, not a statement that this is a
# Python application image — it is a polyglot tool image, and Python is bh's implementation
# detail. uv is therefore pinned INDEPENDENTLY, as its own two variables rather than a coupled
# uv:python*-trixie-slim tag, and by index digest so the pin cannot move under a tag.
# Bump both halves together: docker buildx imagetools inspect ghcr.io/astral-sh/uv:<version>
variable "NIX_TAG" { default = "2.31.2" }
variable "PYTHON_TAG" { default = "3.12.13-slim-trixie" }
variable "UV_VERSION" { default = "0.12.1" }
variable "UV_DIGEST" { default = "sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded" }

# ---- runtime user ---------------------------------------------------------------------------

# Non-root is not negotiable — both harnesses refuse their in-container bypass-permission mode
# as root — but the identity itself is a knob. On a Linux host that bind-mounts instead of using
# the named volumes, files land owned by AGENT_UID, and matching it to the host user is the
# difference between a working mount and a permissions mess.
#
# 8335, not 1000: 1000 is whatever account the distro handed to the first human login, so the
# default collided with a real person on every single-user host. 8335 is a DEDICATED id for the
# agent — the native install path creates the same `bees`:8335 account (bh-q160), so the two
# branches agree and a bind mount works without an override. It sits inside login.defs'
# UID_MIN..UID_MAX (1000..60000), so `useradd` treats it as a regular user everywhere.
variable "AGENT_USER" { default = "bees" }
variable "AGENT_UID" { default = "8335" }
variable "AGENT_GID" { default = "8335" }

# ---- component licence policy (bh-pc2a.21) -------------------------------------------------
#
# THE IMAGE SHIPS REDISTRIBUTABLE COMPONENTS ONLY. Every component pinned below is declared here
# with the licence it actually carries, MEASURED from its own source of truth rather than assumed.
# tests/test_component_licenses.py enforces two things against this block: every pinned component
# appears, and every declared licence is on the allowed set. Adding a component therefore requires
# an explicit, reviewed licence decision — it cannot arrive by accident.
#
# SCOPE — read this before extending it. This governs the components WE PIN. It does NOT govern
# the Debian base layer, which carries hundreds of GPL/LGPL packages (git itself is GPL-2) as any
# Debian-derived image does. Those are separate programs invoked as programs: GPL-2 §3 imposes
# source-availability obligations on redistribution, not licence contamination of our code. That
# layer is acknowledged, not audited here, and docs/ASSURANCE.md says so. Two different layers,
# two different mechanisms; neither supersedes the other.
#
# ALLOWED SET — permissive, plus public-domain-equivalent. Deliberately NOT the same set as the
# wheel's `license_allow` in the justfile: that governs Python dependencies LINKED into our
# distribution, while these are standalone binaries we redistribute alongside it. Same policy
# intent, different exposure, so the sets differ on purpose.
#
#   MIT · Apache-2.0 · BSD-2-Clause · BSD-3-Clause · ISC · PSF-2.0 · CC0-1.0 · Artistic-2.0
#
# NOT ALLOWED: any copyleft (GPL/LGPL/AGPL/MPL) and anything without an SPDX identifier. A
# proprietary component is never allowed — bh-pc2a.36 removed the one that was here, and
# `bh harness install` is how a user brings it themselves.
#
#   component       licence       source of the declaration
#   ---------       -------       -------------------------
#   python          PSF-2.0       python:3.12-slim base image, LICENSE.txt
#   uv              Apache-2.0    github astral-sh/uv (dual MIT/Apache; GitHub reports Apache)
#   beadhive        MIT           this project
#
# THE TOOLCHAIN IS NO LONGER DECLARED HERE (bh-8b8o.2). bd, dolt, gh, git-workspace, jq, yq and
# just now arrive in a nix closure, and their licences come from nixpkgs' own `meta.license` via
# docker/toolchain-metadata.json — generated from flake.nix, committed like a lockfile, and
# regenerated-and-diffed by the docker build so it cannot go stale. What is left above is what
# nix does NOT supply, and it is the only part still declared by hand.
#
# The git_workspace OVERRIDE that lived here is retired with them. It existed because the
# published crate carries no `license` field, so no automated reader could classify it and the
# repo's MIT had to be asserted by hand. nixpkgs declares it, the export reads MIT, and an
# exception maintained by memory becomes a fact read from metadata.

# ---- core components ---------------------------------------------------------------------

# The RELEASED bh the image installs — not this working tree.
#
# STILL NOT THE FINAL VALUE, and deliberately so. The manifest reader that makes `bh setup check`
# read /etc/beadhive/image-manifest.json is `feat(setup)` f8557ed, which lives on THIS epic
# branch and has not landed on main — so it is in NO released version yet, 0.7.1 included.
# Checked against the published artifact, not assumed. So this default cannot satisfy the proof
# gate's manifest check — use BEADHIVE_WHEEL below, which installs a locally-built wheel.
# (An earlier revision of this comment said to `--set core.args.BEADHIVE_VERSION=<local build>`.
# That was WRONG and is the reason bh-pc2a.25 exists: BEADHIVE_VERSION only selects a version
# PUBLISHED on PyPI — it could never name a local build.)
#
# Moved 0.6.0 -> 0.7.1 anyway, because 0.6.0 was actively misleading for developing this epic:
# it predates the entire multi-host model, so a plain `docker buildx bake` produced an image
# whose bh has no `bh host` / `bh hq` verbs and no fleet/host config split — i.e. missing the
# very code that bh-pc2a.6 (container-mode detection), .10 (container-aware first run) and .11
# (headroom gating) are written against. Testing those in a 0.6.0 image tests the wrong binary.
#
# FINAL VALUE COMES LATER: once this epic lands, f8557ed reaches main; being a `feat` it makes
# the next release 0.8.0 (cz derives it — major_version_zero=true, so a feat is MINOR). Pin to
# that release and this comment can shrink to a normal version pin.
variable "BEADHIVE_VERSION" { default = "0.7.1" }

# Install bh from a LOCALLY-BUILT WHEEL instead of PyPI. Empty (default) = ordinary PyPI install,
# so a normal bake is unchanged and this costs nothing.
#
# The path is relative to the NAMED CONTEXT `wheelsrc`, not the build context — the build
# context is ./docker and `dist/` is not inside it. Point wheelsrc at ./dist and name the file:
#     just image-local            # does all of this for you
#     # or by hand:
#     uv build
#     docker buildx bake core \
#       --set core.contexts.wheelsrc=./dist \
#       --set core.args.BEADHIVE_WHEEL=beadhive-0.7.1-py3-none-any.whl
# The wheel is bind-mounted, never COPY'd, so it leaves no layer behind. The image manifest
# records `local-wheel:<file>` rather than `pypi:…` and reads the version from the INSTALLED bh,
# so such an image can never masquerade as a released one.
variable "BEADHIVE_WHEEL" { default = "" }



# Built from crates.io in a builder stage, NOT fetched as a release binary: upstream publishes
# only a Linux x86_64 asset (never arm64) and its newest releases — 1.5.0 on GitHub vs 1.10.1
# on crates.io — are crates.io-only. Source is the one channel that is both arch-uniform and
# version-uniform; `cargo install --locked` verifies the registry checksums.





# ---- agent components ---------------------------------------------------------------------


# npm, not the devcontainer Feature — the Feature always installs latest and defeats pinning.
variable "CLAUDE_CODE_VERSION" { default = "2.1.220" }
variable "CODEX_VERSION" { default = "0.146.0" }

# ---- targets --------------------------------------------------------------------------------

# The shared definition. `agent` inherits it, so context/dockerfile/platforms/labels/args are
# declared exactly once — and both arches are a property of this file, not of a caller's flags.
target "core" {
  context    = "./docker"
  # Named context the local-wheel install mounts (bh-pc2a.25). Defaults to the build context
  # itself so an ordinary bake mounts something harmless and BEADHIVE_WHEEL stays empty;
  # override to ./dist to install a locally-built wheel.
  contexts   = { wheelsrc = "./docker", flakesrc = "." }
  dockerfile = "Dockerfile"
  target     = "core"
  platforms  = ["linux/amd64", "linux/arm64"]
  tags       = ["${REGISTRY}/core:${TAG}"]

  labels = {
    "org.opencontainers.image.title"       = "beadhive-core"
    "org.opencontainers.image.description" = "Beadhive scheduler / HQ-sync node: bh, bd, dolt, git, gh, git-workspace"
    "org.opencontainers.image.source"      = "https://github.com/beadhive/beadhive"
    "org.opencontainers.image.url"         = "https://beadhive.ai"
    "org.opencontainers.image.licenses"    = "MIT"
    "org.opencontainers.image.revision"    = BUILD_SHA
    "org.opencontainers.image.version"     = BEADHIVE_VERSION
  }

  args = {
    NIX_TAG    = NIX_TAG
    PYTHON_TAG = PYTHON_TAG
    UV_VERSION = UV_VERSION
    UV_DIGEST  = UV_DIGEST

    AGENT_USER = AGENT_USER
    AGENT_UID  = AGENT_UID
    AGENT_GID  = AGENT_GID

    BEADHIVE_VERSION = BEADHIVE_VERSION
    BEADHIVE_WHEEL   = BEADHIVE_WHEEL

    IMAGE_TAG = "${REGISTRY}/core:${TAG}"
    BUILD_SHA = BUILD_SHA
  }
}

target "agent" {
  inherits = ["core"]
  target   = "agent"
  tags     = ["${REGISTRY}/agent:${TAG}"]

  labels = {
    "org.opencontainers.image.title"       = "beadhive-agent"
    "org.opencontainers.image.description" = "Beadhive agent node: core + harness policy; no harness is shipped (bh-lnrn)"
  }

  args = {
    CLAUDE_CODE_VERSION = CLAUDE_CODE_VERSION
    CODEX_VERSION       = CODEX_VERSION

    IMAGE_TAG = "${REGISTRY}/agent:${TAG}"
  }
}

group "default" {
  targets = ["core", "agent"]
}
