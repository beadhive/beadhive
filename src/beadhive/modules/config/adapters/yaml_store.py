"""Round-trip YAML implementation of the configuration document ports."""

from __future__ import annotations

import fcntl
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from ..domain.ports import ConfigDocument, ConfigScope, EditResult


def round_trip_yaml() -> YAML:
    """Construct an operation-scoped parser with the public formatting contract."""

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


class RoundTripYamlStore:
    """Thread/process-safe, atomic persistence for host and fleet documents."""

    def __init__(
        self,
        *,
        path_for: Callable[[ConfigScope], Path],
        binary_alias: str = "bh",
        yaml_factory: Callable[[], Any] = round_trip_yaml,
        yaml_lock: threading.Lock | threading.RLock | None = None,
        mutation_lock: threading.RLock | None = None,
    ) -> None:
        self._path_for = path_for
        self._binary_alias = binary_alias
        self._yaml_factory = yaml_factory
        self._yaml_lock = yaml_lock
        self._mutation_lock = mutation_lock or threading.RLock()

    @contextmanager
    def _parser(self):
        if self._yaml_lock is None:
            yield self._yaml_factory()
            return
        with self._yaml_lock:
            yield self._yaml_factory()

    def load_path(self, path: Path, *, missing_ok: bool = False) -> ConfigDocument:
        if not path.is_file():
            if missing_ok:
                return CommentedMap()
            raise FileNotFoundError(
                f"{self._binary_alias} config not found at {path}\n"
                f"  scaffold it with:  {self._binary_alias} config init"
            )
        text = path.read_text()
        with self._parser() as yaml:
            return yaml.load(text) or CommentedMap()

    def load_document(self, scope: ConfigScope, *, missing_ok: bool = False) -> ConfigDocument:
        return self.load_path(self._path_for(scope), missing_ok=missing_ok)

    @contextmanager
    def transaction(self, scope: ConfigScope):
        path = self._path_for(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.lock")
        with self._mutation_lock, lock_path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def save_document(self, scope: ConfigScope, document: Mapping[str, Any]) -> None:
        self.save_path(document, self._path_for(scope))

    def save_path(self, document: Mapping[str, Any], path: Path) -> None:
        """Replace *path* atomically; a failed dump never truncates live bytes."""

        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as stream:
                temporary = stream.name
                with self._parser() as yaml:
                    yaml.dump(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
            temporary = None
            self._fsync_directory(path.parent)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def edit_document(
        self,
        scope: ConfigScope,
        edit: Callable[[ConfigDocument], EditResult],
        *,
        missing_ok: bool = False,
    ) -> EditResult:
        with self.transaction(scope):
            document = self.load_document(scope, missing_ok=missing_ok)
            result = edit(document)
            self.save_document(scope, document)
            return result

    @staticmethod
    def _fsync_directory(directory_path: Path) -> None:
        try:
            directory = os.open(directory_path, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass


__all__ = ("RoundTripYamlStore", "round_trip_yaml")
