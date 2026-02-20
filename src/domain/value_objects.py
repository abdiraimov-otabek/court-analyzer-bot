from dataclasses import dataclass


@dataclass(frozen=True)
class UserId:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("UserId cannot be empty")


@dataclass(frozen=True)
class QueryText:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("QueryText cannot be empty")
