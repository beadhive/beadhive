from enum import StrEnum


class GetDependencyTreeDirection(StrEnum):
    BOTH = "both"
    DOWN = "down"
    UP = "up"

    def __str__(self) -> str:
        return str(self.value)
