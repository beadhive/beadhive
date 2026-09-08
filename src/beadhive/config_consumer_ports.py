"""Outward adapters from narrow capability settings to ``beadhive.config``.

Only this composition module knows the compatibility facade.  Attribute lookup and replacement
remain dynamic, preserving supported ``monkeypatch``/``patch`` behavior while production
consumers stop importing the full facade directly.
"""

from __future__ import annotations

import importlib
from typing import Any

from .modules.config.application.consumer_settings import CapabilitySettings


class _LegacyFacadeSource:
    """Resolve the outward facade at use time so inward consumers never close a cycle.

    This deferred composition is the compatibility requirement itself: patches applied to
    ``beadhive.config`` after a consumer import must remain visible.  It does not move an inward
    dependency behind a lazy import; this outward adapter is the sole approved facade owner.
    """

    @staticmethod
    def _facade():
        return importlib.import_module("beadhive.config")

    def get_value(self, name: str) -> Any:
        return getattr(self._facade(), name)

    def set_value(self, name: str, value: Any) -> None:
        setattr(self._facade(), name, value)

    def delete_value(self, name: str) -> None:
        delattr(self._facade(), name)


_SOURCE = _LegacyFacadeSource()

daemon_settings = CapabilitySettings(
    "host-daemon",
    {
        "home",
        "load",
        "otel_flush_timeout",
    },
    _SOURCE,
)

work_settings = CapabilitySettings(
    "work",
    {
        "BINARY_ALIAS",
        "DEFAULT_LEDGER_TTL",
        "batch_max_size",
        "claim_authority",
        "codex_default_sandbox_covers",
        "codex_sandbox_active",
        "demo_cmd",
        "dispatch_auto_budget",
        "dispatch_envelope_grace",
        "dispatch_max_action_retries",
        "dispatch_max_beads_per_session",
        "dispatch_max_concurrency",
        "dispatch_max_depth",
        "dispatch_max_run_seconds",
        "dispatch_mode",
        "dispatch_poll_interval",
        "dispatch_reviewer_cross_seat",
        "dispatch_seat_bundle",
        "dispatch_seat_command",
        "dispatch_terminate_grace",
        "duration_seconds",
        "enforce_signing",
        "integration_branch",
        "harness_name",
        "ledger_ttl",
        "load",
        "max_commits",
        "observaloop_enabled",
        "observaloop_profile_name",
        "otel_protocol",
        "otel_genai_model",
        "otel_genai_system",
        "pr_base",
        "push_remote",
        "precious_globs",
        "junk_globs",
        "precious_min_bytes",
        "release_conflict_estimator",
        "release_fix_churn_budget",
        "release_value",
        "review_gate",
        "routing_policy",
        "routing_tiers",
        "union_globs",
        "validate_cmd",
        "validation_mode",
        "work_identity",
        "work_landing",
        "work_runtime",
        "work_value",
        "worktrees_cfg",
        "worktrees_root",
    },
    _SOURCE,
)

telemetry_settings = CapabilitySettings(
    "telemetry",
    {
        "BINARY_ALIAS",
        "BINARY_NAME",
        "OTEL_METRICS_TEMPORALITY_ENV",
        "OTEL_PROTOCOLS",
        "OTEL_PROTOCOL_GRPC",
        "OTEL_PROTOCOL_HTTP",
        "OTEL_TEMPORALITY_DELTA",
        "load",
        "managed_repos",
        "observaloop_profile",
        "otel_compose_file",
        "otel_enabled",
        "otel_endpoint",
        "otel_export_timeout",
        "otel_flush_timeout",
        "otel_headers",
        "otel_hive",
        "otel_metrics_temporality",
        "otel_protocol",
        "otel_role",
    },
    _SOURCE,
)

plugin_settings = CapabilitySettings(
    "plugins",
    {
        "BINARY_ALIAS",
        "BINARY_NAME",
        "KNOWN_SECTIONS",
        "OTEL_PROTOCOL_GRPC",
        "harness_name",
        "herdr_kind",
        "host_lease_ttl",
        "hq_dir",
        "load",
        "managed_repos",
        "observaloop_cfg",
        "observaloop_enabled",
        "observaloop_profile_name",
        "orca_data_path",
        "orca_enabled",
        "orca_worktrees_enabled",
        "orca_worktrees_fallback",
        "otel_protocol",
        "repowise_enabled",
        "worktrees_root",
    },
    _SOURCE,
)

__all__ = ("daemon_settings", "plugin_settings", "telemetry_settings", "work_settings")
