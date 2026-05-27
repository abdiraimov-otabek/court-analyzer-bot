import hashlib
from typing import Any


class HashingService:
    def __init__(self, salt: str) -> None:
        if not salt:
            raise ValueError("salt cannot be empty")
        self._salt = salt

    def hash_value(self, value: Any) -> str:
        if value == "" or value is None:
            raise ValueError("value cannot be empty")
        digest = hashlib.sha256()
        digest.update(self._salt.encode("utf-8"))
        digest.update(str(value).encode("utf-8"))
        return digest.hexdigest()
