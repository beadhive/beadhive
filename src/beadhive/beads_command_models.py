"""Strict contracts for the JSON emitted by pinned Beads commands.

The generated models in :mod:`beadhive.beads_models` describe canonical storage records.
Command output is a different contract: it is flat JSON which composes those canonical fields
with command-only decorations.  Build fresh models from the generated field definitions instead
of subclassing or relaxing the canonical models, so upstream drift remains visible at its source.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, RootModel, StrictInt, StrictStr, create_model
from pydantic.experimental.missing_sentinel import MISSING

from beadhive.beads_models import BdBriefIssue, BdDependencyRecord, BdIssueRecord


class _StrictCommandRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _fields_from(model: type[BaseModel], *, omit: frozenset[str] = frozenset()) -> dict:
    """Copy field contracts from a generated model without creating an inheritance relation."""

    return {
        name: (field.rebuild_annotation(), field.default)
        for name, field in model.model_fields.items()
        if name not in omit
    }


def _command_model(name: str, base: type[BaseModel], **decorations: tuple) -> type[BaseModel]:
    return create_model(
        name,
        __base__=_StrictCommandRecord,
        __module__=__name__,
        **_fields_from(base),
        **decorations,
    )


_PARENT = (StrictStr | MISSING, MISSING)
_COUNT = (StrictInt, ...)
_LIST_DECORATIONS = {
    "parent": _PARENT,
    "dependency_count": _COUNT,
    "dependent_count": _COUNT,
    "comment_count": _COUNT,
}

# ``list`` and ``ready`` return bare arrays: there is deliberately no top-level schema_version.
BdListRow = _command_model("BdListRow", BdIssueRecord, **_LIST_DECORATIONS)
BdReadyRow = _command_model("BdReadyRow", BdIssueRecord, **_LIST_DECORATIONS)
BdBriefListRow = _command_model("BdBriefListRow", BdBriefIssue, **_LIST_DECORATIONS)
BdBriefReadyRow = _command_model("BdBriefReadyRow", BdBriefIssue, **_LIST_DECORATIONS)


_DEPENDENCY_TYPE = (
    BdDependencyRecord.model_fields["type"].rebuild_annotation(),
    ...,
)

# Full ``show`` dependencies are shallow Issue-shaped rows decorated with dependency_type.
# They are not the issue_id/depends_on_id records represented by BdDependencyRecord.
BdShowDependency = create_model(
    "BdShowDependency",
    __base__=_StrictCommandRecord,
    __module__=__name__,
    **_fields_from(BdIssueRecord, omit=frozenset({"dependencies"})),
    dependency_type=_DEPENDENCY_TYPE,
)

_BRIEF_DEPENDENCY_FIELDS = (
    "id",
    "title",
    "status",
    "priority",
    "issue_type",
    "created_at",
    "updated_at",
)
BdBriefDependency = create_model(
    "BdBriefDependency",
    __base__=_StrictCommandRecord,
    __module__=__name__,
    **{
        name: (field.rebuild_annotation(), field.default)
        for name in _BRIEF_DEPENDENCY_FIELDS
        if (field := BdBriefIssue.model_fields[name])
    },
    dependency_type=_DEPENDENCY_TYPE,
)

_SHOW_BASE_FIELDS = _fields_from(BdIssueRecord, omit=frozenset({"dependencies"}))
_SHOW_DECORATIONS = {
    "parent": _PARENT,
    "dependent_count": _COUNT,
    "dependency_count": _COUNT,
    "comment_count": _COUNT,
    "revision": (StrictStr, ...),
}
BdShowRow = create_model(
    "BdShowRow",
    __base__=_StrictCommandRecord,
    __module__=__name__,
    **_SHOW_BASE_FIELDS,
    dependencies=(list[BdShowDependency] | MISSING, MISSING),
    **_SHOW_DECORATIONS,
)
BdBriefDepsShowRow = create_model(
    "BdBriefDepsShowRow",
    __base__=_StrictCommandRecord,
    __module__=__name__,
    **_SHOW_BASE_FIELDS,
    dependencies=(list[BdBriefDependency] | MISSING, MISSING),
    **_SHOW_DECORATIONS,
)


class BdListResult(RootModel[list[BdListRow]]):
    """Bare array returned by ``bd list --json`` (no version envelope)."""


class BdReadyResult(RootModel[list[BdReadyRow]]):
    """Bare array returned by ``bd ready --json`` (no version envelope)."""


class BdBriefListResult(RootModel[list[BdBriefListRow]]):
    """Bare array returned by ``bd list --brief --json``."""


class BdBriefReadyResult(RootModel[list[BdBriefReadyRow]]):
    """Bare array returned by ``bd ready --brief --json``."""


class BdShowResult(RootModel[list[BdShowRow]]):
    """Bare array returned by ``bd show --json`` (no version envelope)."""


class BdBriefDepsShowResult(RootModel[list[BdBriefDepsShowRow]]):
    """Bare array returned by ``bd show --brief-deps --json``."""


class BdCommandError(_StrictCommandRecord):
    """Versioned JSON error emitted by commands which support a typed error response."""

    error: StrictStr
    hint: StrictStr | MISSING = MISSING
    schema_version: Literal[1]
