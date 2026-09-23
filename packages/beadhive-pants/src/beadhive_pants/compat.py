"""Compatibility support for path-loaded legacy script modules."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType


class _LegacyScriptModule(ModuleType):
    """Forward reads and test patch points to the package-owned implementation."""

    def __getattr__(self, name: str) -> object:
        return getattr(self.__dict__["_implementation"], name)

    def __setattr__(self, name: str, value: object) -> None:
        implementation = self.__dict__.get("_implementation")
        if implementation is not None and hasattr(implementation, name):
            setattr(implementation, name, value)
            return
        super().__setattr__(name, value)


def install_legacy_script(module_name: str, implementation: str) -> ModuleType:
    """Turn the executing shim into a transparent proxy for ``implementation``."""

    target = importlib.import_module(implementation)
    module = sys.modules[module_name]
    module.__class__ = _LegacyScriptModule
    module.__dict__["_implementation"] = target
    return target
