from enum import StrEnum


class ApplyItemResultKind(StrEnum):
    CLOSE = "close"
    CREATE = "create"
    DEP_ADD = "dep_add"
    UPDATE = "update"

    def __str__(self) -> str:
        return str(self.value)
