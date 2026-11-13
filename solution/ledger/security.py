from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet


class PiiBox:
    def __init__(self, key: bytes):
        try:
            self.fernet = Fernet(key)
        except Exception as exc:
            raise ValueError("Invalid Fernet key. Generate one with the keygen command.") from exc
        raw = base64.urlsafe_b64decode(key)
        self.index_key = hmac.new(raw, b"assignment-a-phone-index", hashlib.sha256).digest()

    @classmethod
    def from_env_or_file(cls, key_file: str | None = None) -> "PiiBox":
        value = os.environ.get("AU_PII_KEY")
        if value:
            return cls(value.encode("ascii"))
        if key_file and Path(key_file).exists():
            return cls(Path(key_file).read_bytes().strip())
        raise ValueError("Set AU_PII_KEY or provide --key-file (create a key with keygen).")

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")

    def phone_digest(self, canonical_phone: str) -> str:
        return hmac.new(self.index_key, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_key() -> bytes:
    return Fernet.generate_key()
