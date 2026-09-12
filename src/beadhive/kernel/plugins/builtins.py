"""Declared built-in manifest source; importing it does not import integrations or read files."""

from __future__ import annotations

from importlib import resources

from .contracts import ManifestDocument, ManifestProvenance
from .discovery import BuiltInManifestSource

BUILTIN_PLUGIN_IDS = ("herdr", "hitch", "observaloop", "orca", "repowise")
_PACKAGE = "beadhive.kernel.plugins.manifests"


def builtin_manifest_documents() -> tuple[ManifestDocument, ...]:
    """Read only the five statically declared package resources, in plugin-ID order."""

    root = resources.files(_PACKAGE)
    return tuple(
        ManifestDocument(
            ManifestProvenance("built-in", f"{plugin_id}.json"),
            root.joinpath(f"{plugin_id}.json").read_bytes(),
        )
        for plugin_id in BUILTIN_PLUGIN_IDS
    )


def builtin_manifest_source() -> BuiltInManifestSource:
    return BuiltInManifestSource(builtin_manifest_documents())
