"""Versioned HMAC-SHA256 prehash followed by salted Argon2id."""

import hashlib
import hmac
from typing import Any
from django.conf import settings
from django.contrib.auth.hashers import Argon2PasswordHasher
from django.views.decorators.debug import sensitive_variables


class PepperedArgon2PasswordHasher(Argon2PasswordHasher):
    # Short identifier keeps encoded values inside Django's 128-character field.
    algorithm = "argon2p"
    time_cost = 3
    memory_cost = 65536
    parallelism = 1

    @sensitive_variables("password", "key")
    def _prehash(self, password: str, key_id: str) -> str:
        key = bytes.fromhex(settings.PASSWORD_PEPPER_KEYS[key_id])
        return hmac.new(key, password.encode("utf-8"), hashlib.sha256).hexdigest()

    def _unwrap(self, encoded: str) -> tuple[str, str]:
        algorithm, key_id, rest = encoded.split("$", 2)
        if algorithm != self.algorithm or key_id not in settings.PASSWORD_PEPPER_KEYS:
            raise ValueError("Unknown password hash key.")
        return key_id, f"{self.algorithm}${rest}"

    @sensitive_variables("password")
    def encode(self, password: str, salt: str) -> str:
        key_id = settings.PASSWORD_PEPPER_ID
        inner = super().encode(self._prehash(password, key_id), salt)
        return f"{self.algorithm}${key_id}${inner.split('$', 1)[1]}"

    @sensitive_variables("password")
    def verify(self, password: str, encoded: str) -> bool:
        try:
            key_id, inner = self._unwrap(encoded)
            return super().verify(self._prehash(password, key_id), inner)
        except (ValueError, KeyError):
            return False

    def decode(self, encoded: str) -> dict[str, Any]:
        key_id, inner = self._unwrap(encoded)
        return {**super().decode(inner), "pepper_id": key_id}

    def must_update(self, encoded: str) -> bool:
        try:
            return self.decode(encoded)[
                "pepper_id"
            ] != settings.PASSWORD_PEPPER_ID or super().must_update(encoded)
        except (ValueError, KeyError):
            return True
