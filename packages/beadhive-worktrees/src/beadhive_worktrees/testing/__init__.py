"""Supported test helpers for consumers of the worktree capability (bh-qdezo.9).

Two things live here, both stdlib-only and free of any test framework:

* the provider conformance kit (:mod:`.conformance`) every ``worktree.manager`` and
  ``workspace.binding`` provider runs — native Git today, the Herdr binding (Option A), and a
  future Herdr manager (Option B);
* in-memory port doubles (:mod:`.fakes`) so a higher layer tests at the port boundary instead of
  patching this package's internals or its own adapters' private names.

Importing this subpackage never touches a repository, a process, or the network.
"""

from __future__ import annotations

from .conformance import (
    BINDING_CASES,
    MANAGER_CASES,
    BindingHarness,
    ConformanceCase,
    ConformanceFailure,
    Expectation,
    ManagerHarness,
    assert_binding_conforms,
    assert_manager_conforms,
    run_binding_case,
    run_manager_case,
)
from .fakes import InMemoryBeadStateLookup, InMemoryPresenter, InMemoryWorkspaceBinding

__all__ = [
    "BINDING_CASES",
    "MANAGER_CASES",
    "BindingHarness",
    "ConformanceCase",
    "ConformanceFailure",
    "Expectation",
    "InMemoryBeadStateLookup",
    "InMemoryPresenter",
    "InMemoryWorkspaceBinding",
    "ManagerHarness",
    "assert_binding_conforms",
    "assert_manager_conforms",
    "run_binding_case",
    "run_manager_case",
]
