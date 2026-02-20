from __future__ import annotations

from typing import Protocol

from src.domain.value_objects import UserId


class AccessStore(Protocol):
    def grant(self, user_id: UserId) -> None:
        ...

    def revoke(self, user_id: UserId) -> None:
        ...

    def is_allowed(self, user_id: UserId) -> bool:
        ...


class InMemoryAccessStore:
    def __init__(self) -> None:
        self._allowed: set[UserId] = set()

    def grant(self, user_id: UserId) -> None:
        self._allowed.add(user_id)

    def revoke(self, user_id: UserId) -> None:
        self._allowed.discard(user_id)

    def is_allowed(self, user_id: UserId) -> bool:
        return user_id in self._allowed


class AccessControlList:
    def __init__(self, store: AccessStore) -> None:
        self._store = store

    def grant(self, user_id: UserId) -> None:
        self._store.grant(user_id)

    def revoke(self, user_id: UserId) -> None:
        self._store.revoke(user_id)

    def is_allowed(self, user_id: UserId) -> bool:
        return self._store.is_allowed(user_id)
