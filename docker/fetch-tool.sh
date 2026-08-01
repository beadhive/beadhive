#!/bin/sh
# Download a pinned release artifact, verify its SHA-256, then install it.
#
#   fetch-tool.sh bin  URL SHA256 DEST [MEMBER]
#       DEST    absolute path to install an executable to, e.g. /usr/local/bin/bd
#       MEMBER  path inside a tar archive; omit when URL is a bare binary
#
#   fetch-tool.sh tree URL SHA256 PREFIX STRIP
#       PREFIX  directory to unpack a tar archive into, e.g. /usr/local
#       STRIP   leading path components to strip (tar --strip-components)
#
# SHA256 is always the digest of the DOWNLOADED FILE. A mismatch is fatal and names both
# digests, so a stale pin in docker-bake.hcl fails the build loudly instead of shipping an
# unverified binary. GNU tar auto-detects gzip/xz, so one path covers .tar.gz and .tar.xz.
set -eu

mode="$1"
url="$2"
want="$3"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fsSL --retry 3 --retry-delay 2 -o "$tmp/download" "$url"

got="$(sha256sum "$tmp/download" | cut -d' ' -f1)"
if [ "$got" != "$want" ]; then
    echo "fetch-tool: sha256 mismatch for $url" >&2
    echo "  expected $want" >&2
    echo "  actual   $got" >&2
    exit 1
fi

case "$mode" in
bin)
    dest="$4"
    member="${5:-}"
    if [ -n "$member" ]; then
        tar -xf "$tmp/download" -C "$tmp"
        install -m 0755 "$tmp/$member" "$dest"
    else
        install -m 0755 "$tmp/download" "$dest"
    fi
    ;;
tree)
    prefix="$4"
    strip="$5"
    tar -xf "$tmp/download" -C "$prefix" --strip-components="$strip" --no-same-owner
    ;;
*)
    echo "fetch-tool: unknown mode '$mode' (expected bin|tree)" >&2
    exit 1
    ;;
esac
