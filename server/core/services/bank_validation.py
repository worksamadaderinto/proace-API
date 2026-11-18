"""
U.S. Bank Account Validation Client and Service Layer.

Validates that submitted US domestic routing and account numbers are authentic,
active, and match the specified account holder name before permitting automated withdrawals.
"""
from __future__ import annotations

import importlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from django.conf import settings
from core.runtime_config import config

logger = logging.getLogger(__name__)


@runtime_checkable
class BankValidator(Protocol):
    """Protocol defining bank account credential and ownership validation."""

    def validate_account(
        self,
        *,
        routing_number: str,
        account_number: str,
        account_holder_name: str,
        account_type: str = "checking",
    ) -> ValidationResult:
        """Validate routing and account number authentic status."""
        ...


def validate_us_routing_number(routing_number: str) -> bool:
    """
    Validates a 9-digit US Federal Reserve ABA Routing Transit Number (RTN)
    using the standard Federal Reserve checksum algorithm:
    (3*(d1 + d4 + d7) + 7*(d2 + d5 + d8) + 1*(d3 + d6 + d9)) % 10 == 0
    """
    if not routing_number or not re.fullmatch(r'[0-9]{9}', routing_number) or routing_number == '000000000':
        return False
    digits = [int(c) for c in routing_number]
    weights = [3, 7, 1, 3, 7, 1, 3, 7, 1]
    checksum = sum(d * w for d, w in zip(digits, weights))
    return checksum % 10 == 0


@dataclass
class ValidationResult:
    is_valid: bool
    status: str  # 'verified', 'failed'
    reason: str = ""
    bank_name: str = ""
    account_status: str = ""  # 'OPEN', 'CLOSED', etc.
    raw_response: dict[str, Any] | None = None


class USBankValidationService:
    """
    Service client for the U.S. Bank Account Validation API.
    Handles authentication, inquiry generation, and status mapping.
    """

    def __init__(self) -> None:
        self.client_id = config.get('USBANK_CLIENT_ID', '')
        self.client_secret = config.get('USBANK_CLIENT_SECRET', '')
        self.base_url = config.get('USBANK_API_BASE_URL', 'https://api.usbank.com').rstrip('/')
        self.environment = config.get('USBANK_ENVIRONMENT', 'sandbox')

    def validate_account(
        self,
        *,
        routing_number: str,
        account_number: str,
        account_holder_name: str,
        account_type: str = 'checking',
    ) -> ValidationResult:
        """
        Validates account credentials and ownership against U.S. Bank Account Validation API.
        """
        # Step 1: Pre-validation of US ABA routing number format and checksum
        if not validate_us_routing_number(routing_number):
            return ValidationResult(
                is_valid=False,
                status='failed',
                reason='Invalid US ABA 9-digit routing number checksum or format.',
            )

        if not account_number or not (4 <= len(account_number) <= 17) or not re.fullmatch(r'[0-9]+', account_number):
            return ValidationResult(
                is_valid=False,
                status='failed',
                reason='Account number must be between 4 and 17 numeric digits for US checking/savings accounts.',
            )

        if (
            not self.client_id or not self.client_secret
            or self.environment not in {'sandbox', 'production'}
            or (not settings.DEBUG and self.environment != 'production')
            or not self.base_url.startswith('https://')
        ):
            return ValidationResult(
                is_valid=False,
                status='failed',
                reason='Bank verification is currently unavailable. Please try again later or use Stripe Payouts.',
            )

        return self._call_usbank_api(
            routing_number=routing_number,
            account_number=account_number,
            account_holder_name=account_holder_name,
            account_type=account_type,
        )

    def _call_usbank_api(
        self,
        routing_number: str,
        account_number: str,
        account_holder_name: str,
        account_type: str,
    ) -> ValidationResult:
        try:
            token = self._get_access_token()
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Correlation-ID': str(uuid.uuid4()),
            }
            # Names can be single entity/business name or split into first/last
            name_parts = account_holder_name.strip().split(' ', 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else name_parts[0]

            payload = {
                'inquiryDetails': {
                    'accountType': account_type.lower(),
                    'accountNumber': account_number,
                    'routingNumber': routing_number,
                },
                'ownerDetails': {
                    'firstName': first_name,
                    'lastName': last_name,
                    'organizationName': account_holder_name if len(name_parts) == 1 else None,
                },
                'transactionContext': {
                    'transactionType': 'credit',
                    'purpose': 'payout_verification',
                },
            }

            url = urljoin(self.base_url, '/payments/account-validation/v1/validate')
            req = Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')

            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return self._parse_usbank_response(data)

        except HTTPError as error:
            logger.warning("Bank verification provider rejected request (HTTP %s).", error.code)
            return ValidationResult(False, 'failed', 'Bank verification was unsuccessful. Please check your details or try again later.')
        except (URLError, TimeoutError):
            logger.warning("Bank verification provider is unavailable.")
            return ValidationResult(False, 'failed', 'Bank verification is currently unavailable. Please try again later.')
        except Exception:
            logger.error("Bank verification returned an unexpected failure.")
            return ValidationResult(False, 'failed', 'Bank verification could not be completed. Please try again later.')

    def _get_access_token(self) -> str:
        """
        Retrieves OAuth2 Bearer token from U.S. Bank token endpoint.
        """
        token_url = urljoin(self.base_url, '/oauth/v1/token')
        auth_payload = {
            'grant_type': 'client_credentials',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }
        req = Request(
            token_url,
            data=json.dumps(auth_payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            method='POST',
        )
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            token = data.get('access_token')
            if not isinstance(token, str) or not token:
                raise ValueError('Missing provider access token.')
            return token

    def _parse_usbank_response(self, data: dict[str, Any]) -> ValidationResult:
        """
        Maps U.S. Bank / EWS response fields to internal VerificationResult.
        """
        # U.S. Bank typically returns accountStatus: 'OPEN' / 'CLOSED' / 'INVALID'
        # and matchResults or score for account ownership.
        status = data.get('accountStatus', '').upper()
        name_match = data.get('ownerMatchResult', data.get('matchResult', '')).upper()

        if status == 'OPEN' and name_match in ('MATCH', 'CONFIRMED'):
            return ValidationResult(
                is_valid=True,
                status='verified',
                reason='Account verified successfully.',
                account_status=status,
                raw_response=data,
            )

        reason = 'The account and account holder could not be verified. Please check your details.'
        return ValidationResult(
            is_valid=False,
            status='failed',
            reason=reason,
            account_status=status,
            raw_response=data,
        )


class MockBankValidator:
    """In-memory deterministic bank validator for offline and hermetic testing."""

    def __init__(
        self,
        default_valid: bool = True,
        status: str = "verified",
        reason: str = "Account verified successfully.",
    ) -> None:
        self.default_valid = default_valid
        self.status = status
        self.reason = reason
        self.validated_accounts: list[dict[str, Any]] = []

    def validate_account(
        self,
        *,
        routing_number: str,
        account_number: str,
        account_holder_name: str,
        account_type: str = "checking",
    ) -> ValidationResult:
        self.validated_accounts.append({
            "routing_number": routing_number,
            "account_number": account_number,
            "account_holder_name": account_holder_name,
            "account_type": account_type,
        })
        if not validate_us_routing_number(routing_number):
            return ValidationResult(
                is_valid=False,
                status="failed",
                reason="Invalid US ABA routing transit number checksum.",
            )
        return ValidationResult(
            is_valid=self.default_valid,
            status=self.status if self.default_valid else "failed",
            reason=self.reason if self.default_valid else "Account could not be verified.",
            account_status="OPEN" if self.default_valid else "CLOSED",
        )


def get_bank_validator() -> BankValidator:
    """Factory to retrieve the active bank validation service configured in settings."""
    default_backend = (
        "core.services.bank_validation.USBankValidationService"
        if (config.get("USBANK_CLIENT_ID") or not settings.DEBUG)
        else "core.services.bank_validation.MockBankValidator"
    )
    backend_path = getattr(
        settings,
        "BANK_VALIDATION_BACKEND",
        default_backend,
    )
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()

