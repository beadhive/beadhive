from enum import StrEnum


class CountDependencyEdgesDirection(StrEnum):
    IN = "in"
    OUT = "out"

    def __str__(self) -> str:
        return str(self.value)
