from enum import StrEnum


class CountIssuesGroupBy(StrEnum):
    ASSIGNEE = "assignee"
    LABEL = "label"
    PRIORITY = "priority"
    STATUS = "status"
    TYPE = "type"

    def __str__(self) -> str:
        return str(self.value)
