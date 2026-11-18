"""Local environment settings or Vault Agent files; never export file secrets."""

import json
import os
import re
import stat
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables


class RuntimeConfig:
    @property
    def vault_mode(self) -> bool:
        mode = os.environ.get("PROACE_CONFIG_MODE", "local")
        if mode not in {"local", "vault"}:
            raise ImproperlyConfigured("Select a supported configuration mode.")
        return mode == "vault"

    @staticmethod
    @lru_cache(maxsize=8)
    @sensitive_variables("content", "values")
    def _read(path: str, secret: bool) -> dict[str, Any]:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1048576:
                    raise ValueError("Invalid configuration file")
                if secret and metadata.st_mode & 0o137:
                    raise ValueError("Unsafe secret file permissions")
                content = stream.read(1048577)
                values = json.loads(content)
            if not isinstance(values, dict) or any(
                not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                for key in values
            ):
                raise ValueError("Invalid configuration object")
            return values
        except (OSError, ValueError, UnicodeError):
            raise ImproperlyConfigured(
                "Runtime configuration is unavailable or invalid; check Vault Agent and file permissions."
            ) from None

    @sensitive_variables("secrets", "value")
    def get(self, name: str, default: Any = None) -> Any:
        if not self.vault_mode:
            return os.environ.get(name, default)
        public = self._read(
            os.environ.get("PROACE_CONFIG_FILE", "/etc/proace/config.json"), False
        )
        secrets = self._read(
            os.environ.get("PROACE_SECRETS_FILE", "/run/proace-vault/api/runtime.json"),
            True,
        )
        if (
            str(secrets.get("DJANGO_DEBUG", public.get("DJANGO_DEBUG", ""))).lower()
            != "false"
        ):
            raise ImproperlyConfigured(
                "Vault production configuration requires debug disabled."
            )
        for required in (
            "SECRET_KEY",
            "PASSWORD_PEPPER_KEYS",
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
        ):
            if not secrets.get(required):
                raise ImproperlyConfigured(
                    "Required production secrets are missing from Vault."
                )
        for key, public_value in public.items():
            if isinstance(public_value, str) and "://" in public_value:
                address = urlsplit(public_value)
                if address.username or address.password:
                    raise ImproperlyConfigured(
                        "Move embedded connection credentials into Vault secrets."
                    )
            if (
                re.search(r"PASSWORD|SECRET|TOKEN|CREDENTIALS|_KEY$", key)
                and key != "PASSWORD_PEPPER_ID"
            ):
                raise ImproperlyConfigured(
                    "Move sensitive configuration into Vault secrets."
                )
        if name in {"CACHE_URL", "CELERY_BROKER_URL"}:
            database = "0" if name == "CACHE_URL" else "1"
            return (
                "redis://:"
                + quote(str(secrets["REDIS_PASSWORD"]), safe="")
                + "@redis:6379/"
                + database
            )
        value = secrets.get(name, public.get(name, default))
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        if isinstance(value, (int, float)):
            return str(value)
        return value


config = RuntimeConfig()
