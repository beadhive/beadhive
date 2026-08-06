#!/usr/bin/env bash
# The bh version `just local-install` installs — DERIVED from this checkout, never typed.
#
# WHY IT IS DERIVED. install.sh (bh-q160.6) resolves a release TAG, clones it, and hands off to
# `just local-install`. "The tag names the PyPI release" is then true BY CONSTRUCTION rather
# than by discipline: the pin comes out of the tree the tag points at (pyproject.toml), and no
# version literal is ever spelled in the justfile. A second place to type a version is a second
# place for it to be wrong.
#
# Usage:
#   release-pin.sh            print the pin (e.g. `0.7.1`) on stdout and nothing else, so a
#                             caller can splice it: uv tool install "beadhive[otel]==$(...)"
#   release-pin.sh --verify   the same checks with a human line on stdout and NO pin — the gate
#                             `local-install` runs BEFORE `uv tool install`, so a broken release
#                             aborts before anything is installed.
#
# THE REFUSAL. If HEAD is exactly a tag and that tag does not name pyproject's version, this
# checkout is a broken release: the bh that gets installed would not be the code the tag claims.
# That is not tolerable skew, so it exits 1. An UNTAGGED checkout — every working tree between
# releases — is not skew at all: there is no claim to contradict, so it passes and pins whatever
# pyproject says.
#
# Exit-code contract:
#     0   the pin is coherent (printed on stdout, or confirmed under --verify)
#     1   tag/pyproject skew — a broken release
#     2   the pin could not be determined at all (no project, no version key, bad argument)
set -uo pipefail

# The checkout is the script's own parent, not the cwd: `just` already runs recipes from the
# justfile directory, and deriving it here keeps the script correct when run by hand from
# anywhere — and testable by copying it into a fixture tree.
root="$(cd "$(dirname "$0")/.." && pwd)"

verify=0
case "${1-}" in
  "") ;;
  --verify) verify=1 ;;
  *)
    echo "release-pin: unknown argument '$1' (expected: --verify or nothing)" >&2
    exit 2
    ;;
esac

# READ THE FILE, don't ask uv. `uv version --short` is the obvious tool and both of its forms
# are wrong here (measured, uv 0.11.23): the bare form RE-LOCKS AND SYNCS, creating a .venv —
# a mutation, and `plan=1` promises none — while `--frozen` refuses outright without a uv.lock
# beside the pyproject, which a source tarball or a fresh clone of a lock-less tree has not got.
# pyproject.toml IS the source of truth, and one key from one table is a smaller dependency
# than either. Narrow on purpose: only `[project]`'s first `version`, so `[tool.commitizen]`'s
# version_provider and every other table are invisible to it.
version="$(awk '
  /^[[:space:]]*\[/ { in_project = ($0 ~ /^[[:space:]]*\[project\][[:space:]]*$/); next }
  in_project && /^[[:space:]]*version[[:space:]]*=/ {
    if (match($0, /"[^"]*"/)) { print substr($0, RSTART + 1, RLENGTH - 2); exit }
  }
' "$root/pyproject.toml" 2>/dev/null)"
if [ -z "$version" ]; then
  echo "release-pin: no [project] version readable from $root/pyproject.toml" >&2
  exit 2
fi

# --exact-match is the whole point: a bare `git describe` reports the NEAREST tag, so every
# commit after a release would claim to BE that release. Anything other than "HEAD is exactly a
# tag" must read as untagged. A checkout without git history (a tarball) also lands here.
tag="$(cd "$root" && git describe --tags --exact-match 2>/dev/null)" || tag=""

# [tool.commitizen] tag_format = "v$version" — the one shape a release tag may take here.
if [ -n "$tag" ] && [ "$tag" != "v$version" ]; then
  echo "✗ broken release: tag $tag does not name pyproject version $version (expected v$version)." >&2
  echo "  The bh this would install is not the code the tag claims. Fix the tag or the version" >&2
  echo "  before installing — nothing is installed from a tree whose tag lies about it." >&2
  exit 1
fi

if [ "$verify" -eq 1 ]; then
  if [ -n "$tag" ]; then
    echo "✓ release pin $version — agrees with the checked-out tag $tag"
  else
    echo "✓ release pin $version — untagged checkout, no tag to contradict it"
  fi
  exit 0
fi

printf '%s\n' "$version"
