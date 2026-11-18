"""
Dependency Injection Provider for Third-Party Social Authentication (Google, Apple).

Decouples external identity token verification and JWKS key resolution from direct
HTTP calls to Google and Apple servers, providing robust testability and URL centralization.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Protocol, runtime_checkable

from django.conf import settings
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)


@runtime_checkable
class SocialAuthProvider(Protocol):
    """Protocol defining third-party identity token verification operations."""

    def verify_google_token(self, credential: str, client_id: str) -> dict[str, Any]:
        """Verify a Google OAuth2 / OpenID Connect ID token."""
        ...

    def verify_apple_token(
        self,
        credential: str,
        client_id: str,
        user_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify an Apple Sign-In ID token against Apple's JWKS."""
        ...


class DefaultSocialAuthProvider:
    """Production provider delegating to Google TokenInfo and Apple JWKS endpoints."""

    def verify_google_token(self, credential: str, client_id: str) -> dict[str, Any]:
        import jwt
        import requests

        tokeninfo_url = getattr(
            settings,
            "GOOGLE_TOKENINFO_URL",
            "https://oauth2.googleapis.com/tokeninfo",
        )
        resp = requests.get(
            f"{tokeninfo_url}?id_token={credential}",
            timeout=5,
        )
        if resp.status_code == 200:
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid provider response")
            email = payload.get("email", "").strip().lower()
            email_verified = payload.get("email_verified") in (True, "true")
            if not email or not email_verified:
                raise ValidationError({"detail": "Google email is not verified."})
            if client_id and payload.get("aud") != client_id:
                raise ValidationError({"detail": "Google token audience mismatch."})

            picture = str(payload.get("picture") or "")
            if not picture:
                try:
                    claims = jwt.decode(credential, options={"verify_signature": False})
                    picture = str(claims.get("picture") or "")
                except jwt.InvalidTokenError:
                    logger.warning("Verified Google token profile claims unavailable")

            return {
                "email": email,
                "first_name": payload.get("given_name") or payload.get("name") or "Google User",
                "last_name": payload.get("family_name") or "",
                "sub": payload.get("sub", ""),
                "picture": picture,
            }

        raise ValidationError({"detail": "Unable to verify Google credential. Please try again."})

    def verify_apple_token(
        self,
        credential: str,
        client_id: str,
        user_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import jwt
        from core.social_auth import RedisAppleJWKClient

        jwks_url = getattr(
            settings,
            "APPLE_JWKS_URL",
            "https://appleid.apple.com/auth/keys",
        )
        signing_key = RedisAppleJWKClient(
            jwks_url, timeout=5
        ).get_signing_key_from_jwt(credential)

        decoded = jwt.decode(
            credential,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer="https://appleid.apple.com",
        )
        iss = decoded.get("iss", "")
        if iss in ("https://appleid.apple.com", "appleid.apple.com") and decoded.get("email"):
            if decoded.get("email_verified") not in (True, "true"):
                raise ValidationError({"detail": "Apple email is not verified."})
            name_obj = (user_info or {}).get("name") or {}
            first_name = name_obj.get("firstName") or decoded.get("given_name") or "Apple User"
            last_name = name_obj.get("lastName") or decoded.get("family_name") or ""
            return {
                "email": str(decoded["email"]).strip().lower(),
                "first_name": first_name,
                "last_name": last_name,
                "sub": decoded.get("sub", ""),
            }

        raise ValidationError({"detail": "Unable to verify Apple credential. Please try again."})


class MockSocialAuthProvider:
    """Mock identity provider for automated unit tests and local sandboxes."""

    def __init__(self, override_claims: dict[str, Any] | None = None) -> None:
        self.override_claims = override_claims or {}

    def verify_google_token(self, credential: str, client_id: str) -> dict[str, Any]:
        if "error" in self.override_claims:
            raise ValidationError({"detail": self.override_claims["error"]})
        email = self.override_claims.get("email", "mock-google-user@example.com")
        return {
            "email": email,
            "first_name": self.override_claims.get("first_name", "Google"),
            "last_name": self.override_claims.get("last_name", "User"),
            "sub": self.override_claims.get("sub", f"google-sub-{email}"),
            "picture": self.override_claims.get("picture", ""),
        }

    def verify_apple_token(
        self,
        credential: str,
        client_id: str,
        user_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if "error" in self.override_claims:
            raise ValidationError({"detail": self.override_claims["error"]})
        email = self.override_claims.get("email", "mock-apple-user@example.com")
        return {
            "email": email,
            "first_name": self.override_claims.get("first_name", "Apple"),
            "last_name": self.override_claims.get("last_name", "User"),
            "sub": self.override_claims.get("sub", f"apple-sub-{email}"),
        }


def get_social_auth_provider() -> SocialAuthProvider:
    """Factory to retrieve the active social auth provider configured in settings."""
    backend_path = getattr(
        settings,
        "SOCIAL_AUTH_PROVIDER_BACKEND",
        "core.social_auth_provider.DefaultSocialAuthProvider",
    )
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()
