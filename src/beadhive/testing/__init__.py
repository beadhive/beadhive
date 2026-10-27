"""Supported helpers for testing Beadhive extension contracts.

The package intentionally depends only on the Python standard library.  A plugin, adapter, or
capability module can therefore import it without constructing Beadhive's runtime or inheriting
repository-root pytest fixtures.

The :mod:`beadhive.testing.impact` subpackage (the ``ImpactBackend`` conformance kit) is imported
explicitly, never from here: it depends on the work module's impact contract. Likewise the
:mod:`beadhive.testing.tracker` subpackage (``MemoryTracker`` and the tracker adapter conformance
cases) depends on the tracker module's contracts and is imported explicitly.
"""

from .conformance import (
    ConformanceCase,
    ConformanceFailure,
    LifecycleCase,
    PluginCase,
    PluginSubject,
    PortCase,
    SchemaArtifact,
    assert_lifecycle_conforms,
    assert_plugin_conforms,
    assert_port_conforms,
    assert_schema_artifact_conforms,
    canonical_json_bytes,
)

__all__ = (
    "ConformanceCase",
    "ConformanceFailure",
    "LifecycleCase",
    "PluginCase",
    "PluginSubject",
    "PortCase",
    "SchemaArtifact",
    "assert_lifecycle_conforms",
    "assert_plugin_conforms",
    "assert_port_conforms",
    "assert_schema_artifact_conforms",
    "canonical_json_bytes",
)
