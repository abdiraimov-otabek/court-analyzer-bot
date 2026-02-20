from src.domain.value_objects import UserId
from src.infrastructure.access_repository import AllowedUsersRepository


class DatabaseAccessStore:
    def __init__(self, repository: AllowedUsersRepository) -> None:
        self._repository = repository

    def grant(self, user_id: UserId) -> None:
        self._repository.grant(user_id.value)

    def revoke(self, user_id: UserId) -> None:
        self._repository.revoke(user_id.value)

    def is_allowed(self, user_id: UserId) -> bool:
        return self._repository.is_allowed(user_id.value)
