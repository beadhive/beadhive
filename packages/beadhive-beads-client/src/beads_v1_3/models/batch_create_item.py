from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.batch_create_dependency import BatchCreateDependency


T = TypeVar("T", bound="BatchCreateItem")


@_attrs_define
class BatchCreateItem:
    """
    Attributes:
        title (str):
        description (str | Unset):
        design (str | Unset):
        acceptance_criteria (str | Unset):
        priority (int | Unset): 0 is P0/critical. Absent means the workspace default.
        issue_type (str | Unset): Issue type. Spelled `issue_type` rather than `type`, matching the member `Issue`
            carries, and validated against the built-ins plus the workspace's configured custom types — an unknown one is a
            `400`.
        assignee (str | Unset):
        labels (list[str] | Unset):
        dependencies (list[BatchCreateDependency] | Unset): The edges this issue is created carrying. They are written
            in the same transaction as the issue, so this operation never publishes an issue whose declared relationships
            are not there yet.
    """

    title: str
    description: str | Unset = UNSET
    design: str | Unset = UNSET
    acceptance_criteria: str | Unset = UNSET
    priority: int | Unset = UNSET
    issue_type: str | Unset = UNSET
    assignee: str | Unset = UNSET
    labels: list[str] | Unset = UNSET
    dependencies: list[BatchCreateDependency] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        description = self.description

        design = self.design

        acceptance_criteria = self.acceptance_criteria

        priority = self.priority

        issue_type = self.issue_type

        assignee = self.assignee

        labels: list[str] | Unset = UNSET
        if not isinstance(self.labels, Unset):
            labels = self.labels

        dependencies: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.dependencies, Unset):
            dependencies = []
            for dependencies_item_data in self.dependencies:
                dependencies_item = dependencies_item_data.to_dict()
                dependencies.append(dependencies_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "title": title,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if design is not UNSET:
            field_dict["design"] = design
        if acceptance_criteria is not UNSET:
            field_dict["acceptance_criteria"] = acceptance_criteria
        if priority is not UNSET:
            field_dict["priority"] = priority
        if issue_type is not UNSET:
            field_dict["issue_type"] = issue_type
        if assignee is not UNSET:
            field_dict["assignee"] = assignee
        if labels is not UNSET:
            field_dict["labels"] = labels
        if dependencies is not UNSET:
            field_dict["dependencies"] = dependencies

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.batch_create_dependency import BatchCreateDependency  # noqa: PLC0415

        d = dict(src_dict)
        title = d.pop("title")

        description = d.pop("description", UNSET)

        design = d.pop("design", UNSET)

        acceptance_criteria = d.pop("acceptance_criteria", UNSET)

        priority = d.pop("priority", UNSET)

        issue_type = d.pop("issue_type", UNSET)

        assignee = d.pop("assignee", UNSET)

        labels = cast(list[str], d.pop("labels", UNSET))

        _dependencies = d.pop("dependencies", UNSET)
        dependencies: list[BatchCreateDependency] | Unset = UNSET
        if _dependencies is not UNSET:
            dependencies = []
            for dependencies_item_data in _dependencies:
                dependencies_item = BatchCreateDependency.from_dict(dependencies_item_data)

                dependencies.append(dependencies_item)

        batch_create_item = cls(
            title=title,
            description=description,
            design=design,
            acceptance_criteria=acceptance_criteria,
            priority=priority,
            issue_type=issue_type,
            assignee=assignee,
            labels=labels,
            dependencies=dependencies,
        )

        return batch_create_item
