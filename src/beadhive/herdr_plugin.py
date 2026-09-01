"""Deprecated import-compatible facade and composition root for the Herdr integration.

Implementation and Typer projection live under ``beadhive.integrations.herdr``. Historical
imports resolve to the implementation module so supported monkeypatch targets keep operating on
the same globals during the bounded compatibility window.
"""

from __future__ import annotations

import importlib
import sys

from . import (
    bd,
    config,
    guard,
    host,
    host_adopt,
    host_lease,
    hosts,
    identity,
    jsonout,
    operator_actions,
    operator_agents,
    plugins,
    registry,
    run,
    store_locator,
    work_group,
    worktree,
)
from .agent_launch_profile import AgentLaunchReceipt
from .herdr_launch_profile import (
    HerdrAgentLaunchProfile,
    HerdrPaneCreateTarget,
    build_herdr_launch_receipt,
    launch_spec_digest,
    resolve_herdr_launch_profile,
    validate_herdr_observation,
)
from .integrations.herdr import cli_application as _implementation

_implementation.configure_legacy_dependencies(
    AgentLaunchReceipt=AgentLaunchReceipt,
    HerdrAgentLaunchProfile=HerdrAgentLaunchProfile,
    HerdrPaneCreateTarget=HerdrPaneCreateTarget,
    bd=bd,
    build_herdr_launch_receipt=build_herdr_launch_receipt,
    config=config,
    guard=guard,
    host=host,
    host_adopt=host_adopt,
    host_lease=host_lease,
    hosts=hosts,
    identity=identity,
    jsonout=jsonout,
    launch_spec_digest=launch_spec_digest,
    operator_actions=operator_actions,
    operator_agents=operator_agents,
    registry=registry,
    resolve_herdr_launch_profile=resolve_herdr_launch_profile,
    run=run,
    store_locator=store_locator,
    validate_herdr_observation=validate_herdr_observation,
    work=importlib.import_module(".work", __package__),
    work_group=work_group,
    worktree=worktree,
)

from .integrations.herdr import cli as _cli_adapter  # noqa: E402

PLUGIN = plugins.Plugin(
    name="herdr",
    cli=_cli_adapter.cli,
    enabled=lambda cfg, entry: _implementation.server_up(),
    on_onboard=_implementation._on_onboard,
    onboard_requires_opt_in=True,
    readiness=_implementation._readiness,
)

_implementation.cli = _cli_adapter.cli
_implementation.PLUGIN = PLUGIN
_implementation.session_scoped = _cli_adapter.session_scoped
sys.modules[__name__] = _implementation

from . import herdr_views as _herdr_views  # noqa: E402

_implementation.cli.add_typer(_herdr_views.cli, name="view")
