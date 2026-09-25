from enum import StrEnum


class ClaimNextIssueSort(StrEnum):
    HYBRID = "hybrid"
    OLDEST = "oldest"
    PRIORITY = "priority"

    def __str__(self) -> str:
        return str(self.value)
