from enum import StrEnum


class ListIssuesSort(StrEnum):
    LIST_ISSUES_PARAMS_SORT_CREATED = "created"
    LIST_ISSUES_PARAMS_SORT_PRIORITY = "priority"

    def __str__(self) -> str:
        return str(self.value)
