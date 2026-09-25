from enum import StrEnum


class ListReadyWorkSort(StrEnum):
    HYBRID = "hybrid"
    OLDEST = "oldest"
    PRIORITY = "priority"

    def __str__(self) -> str:
        return str(self.value)
