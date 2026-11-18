from __future__ import annotations

import logging
from typing import Any
import jwt
import requests
from django.conf import settings
from django.core.cache import cache
from django.contrib.auth.models import update_last_login
from django.db import IntegrityError, transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from affiliates.services import record_referral
from core.activity import record_activity
from core.models import Device, SocialAccount, User

import sys

logger = logging.getLogger(__name__)
APPLE_JWKS_URL = 'https://appleid.apple.com/auth/keys'
APPLE_JWKS_CACHE_KEY = 'social:apple:jwks'


class RedisAppleJWKClient(jwt.PyJWKClient):
    """Share Apple's public keys across API processes, including key rotation."""

    def fetch_data(self) -> dict[str, Any]:
        cached = cache.get(APPLE_JWKS_CACHE_KEY)
        if isinstance(cached, dict) and cached.get('keys'):
            return cached
        payload = super().fetch_data()
        jwt.PyJWKSet.from_dict(payload)
        cache.set(APPLE_JWKS_CACHE_KEY, payload, timeout=3600)
        return payload

    def get_jwk_set(self, refresh: bool = False) -> jwt.PyJWKSet:
        if refresh:
            cache.delete(APPLE_JWKS_CACHE_KEY)
        return super().get_jwk_set(refresh=refresh)


def is_development_or_test() -> bool:
    settings_module = str(getattr(settings, 'SETTINGS_MODULE', '') or '')
    return (
        getattr(settings, 'DEBUG', False)
        or getattr(settings, 'TESTING', False)
        or 'test' in settings_module
        or 'pytest' in sys.modules
        or any('test' in arg for arg in sys.argv)
    )


def verify_google_credential(credential: str, provider: Any = None) -> dict[str, Any]:
    if not credential or not isinstance(credential, str):
        raise ValidationError({'detail': 'Please provide a valid Google credential.'})

    cred = credential.strip()
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    if not is_development_or_test() and not client_id:
        raise ValidationError({'detail': 'Google sign-in is not configured.'})

    # Supported mock and test tokens for development and automated suites
    if cred.startswith(('mock-google-', 'test-google-', 'dev-google:')):
        if not is_development_or_test():
            raise ValidationError({'detail': 'Development tokens are only permitted in development mode.'})
        if ':' in cred:
            email = cred.split(':', 1)[1].strip().lower()
        else:
            email = cred.replace('mock-google-', '').replace('test-google-', '').strip().lower()
        if not email or '@' not in email:
            email = 'google.user@example.com'
        return {
            'email': email,
            'first_name': 'Google',
            'last_name': 'Shopper',
            'sub': f'mock-google-sub-{email}',
        }

    if not client_id and not provider:
        raise ValidationError({'detail': 'Google sign-in is not configured.'})

    from core.social_auth_provider import get_social_auth_provider

    active_provider = provider or get_social_auth_provider()
    try:
        return active_provider.verify_google_token(cred, client_id or 'mock-google-client-id')
    except ValidationError:
        raise
    except Exception as exc:
        logger.warning('Google verification request failed (%s)', type(exc).__name__)
        raise ValidationError({'detail': 'Unable to verify Google credential. Please try again.'}) from exc


def verify_apple_credential(credential: str, user_info: dict[str, Any] | None = None, provider: Any = None) -> dict[str, Any]:
    if not credential or not isinstance(credential, str):
        raise ValidationError({'detail': 'Please provide a valid Apple credential.'})

    cred = credential.strip()
    client_id = getattr(settings, 'APPLE_CLIENT_ID', '')
    if not is_development_or_test() and not client_id:
        raise ValidationError({'detail': 'Apple sign-in is not configured.'})

    # Supported mock and test tokens for development and automated suites
    if cred.startswith(('mock-apple-', 'test-apple-', 'dev-apple:')):
        if not is_development_or_test():
            raise ValidationError({'detail': 'Development tokens are only permitted in development mode.'})
        if ':' in cred:
            email = cred.split(':', 1)[1].strip().lower()
        else:
            email = cred.replace('mock-apple-', '').replace('test-apple-', '').strip().lower()
        if not email or '@' not in email:
            email = 'apple.user@example.com'
        return {
            'email': email,
            'first_name': 'Apple',
            'last_name': 'Shopper',
            'sub': f'mock-apple-sub-{email}',
        }

    if not client_id and not provider:
        raise ValidationError({'detail': 'Apple sign-in is not configured.'})

    from core.social_auth_provider import get_social_auth_provider

    active_provider = provider or get_social_auth_provider()
    try:
        return active_provider.verify_apple_token(cred, client_id or 'mock-apple-client-id', user_info)
    except ValidationError:
        raise
    except Exception as exc:
        logger.warning('Apple token verification failed (%s)', type(exc).__name__)
        raise ValidationError({'detail': 'Unable to verify Apple credential. Please try again.'}) from exc


@transaction.atomic
def authenticate_social_user(provider: str, data: dict[str, Any], request: Any = None) -> dict[str, Any]:
    clean_provider = str(provider or '').strip().lower()
    if clean_provider == 'google':
        credential = str(data.get('credential') or data.get('id_token') or data.get('token') or '')
        verified = verify_google_credential(credential)
    elif clean_provider == 'apple':
        credential = str(data.get('credential') or data.get('id_token') or data.get('token') or '')
        user_info = data.get('user') if isinstance(data.get('user'), dict) else None
        verified = verify_apple_credential(credential, user_info)
    else:
        raise ValidationError({'detail': f'Unsupported social provider: {provider}'})

    email = str(verified['email']).strip().lower()
    first_name = str(verified.get('first_name') or ('Google User' if clean_provider == 'google' else 'Apple User'))[:30]
    last_name = str(verified.get('last_name') or '')[:30]
    referral_code = str(data.get('referral_code') or '').strip()

    subject = str(verified.get('sub') or '').strip()
    if not subject or len(subject) > 255:
        raise ValidationError({'detail': 'Unable to identify the provider account.'})
    identity = SocialAccount.objects.select_related('user').filter(
        provider=clean_provider, subject=subject
    ).first()
    if identity and not identity.connected:
        raise PermissionDenied('This sign-in method was disconnected. Use another method and reconnect it in settings.')
    user = identity.user if identity else User.objects.filter(email__iexact=email).first()
    if user:
        user = User.objects.select_for_update().get(pk=user.pk)
    is_new_user = False

    if user:
        # STRICT GUARDRAIL: Admin and staff accounts cannot sign in via social providers
        if user.is_staff or user.is_superuser or user.is_business_admin:
            raise PermissionDenied(
                'Administrative and staff accounts cannot sign in via social providers. '
                'Please sign in with your email and password.'
            )
        if not user.is_active:
            raise PermissionDenied('This account is inactive. Please contact support.')
    else:
        is_new_user = True
        user = User(
            email=email,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
            is_staff=False,
            is_superuser=False,
            is_business_admin=False,
        )
        user.set_unusable_password()
        try:
            with transaction.atomic():
                user.save()
        except IntegrityError:
            user = User.objects.select_for_update().filter(email__iexact=email).first()
            if user is None:
                raise ValidationError({'detail': 'Unable to create your account. Please try again.'}) from None
            from core.social_accounts import customer_only
            customer_only(user)
            is_new_user = False
        # Affiliate referral sync for new signups
        if referral_code and is_new_user:
            record_referral(referral_code, user)

    from core.social_accounts import attach_identity
    already_known = SocialAccount.objects.filter(provider=clean_provider, subject=subject).exists()
    attach_identity(user, clean_provider, verified, login=True)
    if not already_known and not is_new_user:
        from core.social_accounts import notify_method
        notify_method(user, clean_provider, 'connected')

    if clean_provider == 'google':
        from core.social_avatar import schedule_import_google_avatar
        schedule_import_google_avatar(user, str(verified.get('picture') or ''))

    update_last_login(None, user)
    if request:
        remote_ip = request.META.get('REMOTE_ADDR')
        if remote_ip:
            Device.objects.filter(user=user, device_ip=remote_ip).update(last_login=user.last_login)
    record_activity(user)
    from store.services import link_pending_store_team_invitations
    link_pending_store_team_invitations(user)

    from storefront.views import tokens
    payload = tokens(user)
    payload['is_new_user'] = is_new_user
    payload['provider'] = clean_provider
    return payload
