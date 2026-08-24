"""Lazy, explicitly bindable access to the stable config facade."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType


class FacadeBinding:
    """Resolve direct-module calls lazily while allowing facade composition to bind explicitly."""

    def __init__(self, module_name: str):
        self._module_name = module_name
        self._api: ModuleType | None = None

    def bind(self, api: ModuleType) -> None:
        self._api = api

    def get(self) -> ModuleType:
        if self._api is None:
            self._api = import_module(self._module_name)
        return self._api

    def __getattr__(self, name: str):
        return getattr(self.get(), name)
