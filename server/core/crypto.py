"""
Dependency Injection Gateway for Field-Level Data Encryption.

Decouples sensitive record encryption (such as bank account and routing numbers)
from concrete cryptographic libraries, facilitating safe offline testing and key rotation.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet
from django.conf import settings


@runtime_checkable
class DataEncryptor(Protocol):
    """Protocol for field-level symmetric encryption and decryption."""

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext string into base64 ciphertext."""
        ...

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt base64 ciphertext back to plaintext string."""
        ...


class FernetEncryptor:
    """Production data encryptor using Fernet AES-128-CBC."""

    def __init__(self, key: bytes | None = None, scope: str = "payout-destinations") -> None:
        if key is None:
            digest = hashlib.sha256(f"{scope}:{settings.SECRET_KEY}".encode()).digest()
            key = base64.urlsafe_b64encode(digest)
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()


class MockEncryptor:
    """Deterministic in-memory mock encryptor for testing."""

    def __init__(self, prefix: str = "enc:") -> None:
        self.prefix = prefix
        self.encryption_calls: list[str] = []
        self.decryption_calls: list[str] = []

    def encrypt(self, plaintext: str) -> str:
        self.encryption_calls.append(plaintext)
        return f"{self.prefix}{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        self.decryption_calls.append(ciphertext)
        if ciphertext.startswith(self.prefix):
            return ciphertext[len(self.prefix) :]
        return ciphertext


def get_data_encryptor(scope: str = "payout-destinations") -> DataEncryptor:
    """Factory to retrieve the active data encryptor configured in settings."""
    backend_path = getattr(settings, "DATA_ENCRYPTOR_BACKEND", "core.crypto.FernetEncryptor")
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    try:
        return cls(scope=scope)
    except TypeError:
        return cls()
