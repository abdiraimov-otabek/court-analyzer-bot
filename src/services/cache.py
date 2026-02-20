from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    expires_at: datetime


class TtlCache(Generic[T]):
    def __init__(self, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._entries: dict[str, CacheEntry[T]] = {}

    def get(self, key: str, now: datetime) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            self._entries.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: T, now: datetime) -> None:
        self._entries[key] = CacheEntry(value=value, expires_at=now + self._ttl)

    def delete(self, key: str) -> None:
        self._entries.pop(key, None)
