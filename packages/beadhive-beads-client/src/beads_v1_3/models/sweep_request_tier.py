from enum import StrEnum


class SweepRequestTier(StrEnum):
    DURABLE = "durable"
    EPHEMERAL = "ephemeral"

    def __str__(self) -> str:
        return str(self.value)
