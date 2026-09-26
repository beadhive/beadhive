from enum import StrEnum


class QueryIssuesSort(StrEnum):
    ASSIGNEE = "assignee"
    CLOSED = "closed"
    CREATED = "created"
    ID = "id"
    PRIORITY = "priority"
    STATUS = "status"
    TITLE = "title"
    TYPE = "type"
    UPDATED = "updated"

    def __str__(self) -> str:
        return str(self.value)
