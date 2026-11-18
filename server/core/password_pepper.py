"""Validate the secret keyring without exposing its contents in errors."""

import json
import re
from django.core.exceptions import ImproperlyConfigured


def parse_pepper_keys(raw: str, current: str) -> dict[str, str]:
    try:
        keys = json.loads(raw)
    except (ValueError, TypeError):
        raise ImproperlyConfigured(
            "Configure PASSWORD_PEPPER_KEYS as a secret JSON keyring."
        ) from None
    if (
        not isinstance(keys, dict)
        or not keys
        or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", key)
            or not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in keys.items()
        )
    ):
        raise ImproperlyConfigured(
            "Pepper IDs must be 1-8 safe characters; each key must be 64 lowercase hex characters."
        )
    if current not in keys:
        raise ImproperlyConfigured(
            "PASSWORD_PEPPER_ID must identify a configured pepper key."
        )
    return keys
