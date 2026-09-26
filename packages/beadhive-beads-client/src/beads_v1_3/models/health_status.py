from enum import StrEnum


class HealthStatus(StrEnum):
    OK = "ok"

    def __str__(self) -> str:
        return str(self.value)
