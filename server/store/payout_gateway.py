"""
Dependency Injection Gateway for Merchant Payouts & Stripe Connect.

Decouples store payout processing and connect account onboarding from the concrete
Stripe SDK, allowing deterministic offline testing, sandboxing, and alternative payout providers.
"""

from __future__ import annotations

import importlib
import logging
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from django.conf import settings
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)


@runtime_checkable
class PayoutGateway(Protocol):
    """Protocol defining merchant payout and connect onboarding operations."""

    def retrieve_account(self, account_id: str) -> dict[str, Any]:
        """Retrieve connect account details (status, payouts_enabled, etc.)."""
        ...

    def create_account(
        self,
        email: str,
        country: str = "US",
        account_type: str = "express",
        capabilities: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Create a new connect merchant account."""
        ...

    def create_account_link(
        self,
        account_id: str,
        refresh_url: str,
        return_url: str,
        link_type: str = "account_onboarding",
    ) -> Any:
        """Generate an onboarding or update link for a connect account."""
        ...

    def create_transfer(
        self,
        amount_cents: int,
        currency: str,
        destination: str,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute a funds transfer to a merchant's connected account."""
        ...


class StripePayoutGateway:
    """Production Payout Gateway delegating to Stripe Connect API."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or getattr(settings, "STRIPE_SECRET", "")

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise ValidationError(
                {"detail": "Stripe Connect is not configured yet."}
            )
        import stripe

        stripe.api_key = self.api_key

    def retrieve_account(self, account_id: str) -> Any:
        self._ensure_api_key()
        import stripe

        try:
            account = stripe.Account.retrieve(account_id)
            return account
        except stripe.InvalidRequestError:
            raise
        except Exception as exc:
            logger.warning("Stripe account retrieval failed for %s: %s", account_id, exc)
            raise ValidationError(
                {"detail": "Unable to verify connected Stripe account."}
            ) from exc

    def create_account(
        self,
        email: str,
        country: str = "US",
        account_type: str = "express",
        capabilities: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        self._ensure_api_key()
        import stripe

        params: dict[str, Any] = {
            "type": account_type,
            "email": email,
        }
        if country:
            params["country"] = country
        if capabilities:
            params["capabilities"] = capabilities
        else:
            params["capabilities"] = {
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            }
        if metadata:
            params["metadata"] = metadata
        if idempotency_key:
            params["idempotency_key"] = idempotency_key

        try:
            account = stripe.Account.create(**params)
            return account
        except Exception as exc:
            logger.error("Stripe account creation failed: %s", exc)
            raise ValidationError(
                {"detail": "Failed to initiate Stripe Connect setup."}
            ) from exc

    def create_account_link(
        self,
        account_id: str,
        refresh_url: str,
        return_url: str,
        link_type: str = "account_onboarding",
    ) -> Any:
        self._ensure_api_key()
        import stripe

        try:
            link = stripe.AccountLink.create(
                account=account_id,
                refresh_url=refresh_url,
                return_url=return_url,
                type=link_type,
            )
            return link
        except Exception as exc:
            logger.error("Stripe account link creation failed: %s", exc)
            raise ValidationError(
                {"detail": "Failed to generate Stripe onboarding link."}
            ) from exc

    def create_transfer(
        self,
        amount_cents: int,
        currency: str,
        destination: str,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_api_key()
        import stripe

        kwargs: dict[str, Any] = {
            "amount": amount_cents,
            "currency": currency.lower(),
            "destination": destination,
        }
        if metadata:
            kwargs["metadata"] = metadata
        if idempotency_key:
            kwargs["idempotency_key"] = idempotency_key

        try:
            transfer = stripe.Transfer.create(**kwargs)
            return dict(transfer)
        except Exception as exc:
            logger.exception("Stripe payout transfer failed to %s: %s", destination, exc)
            raise ValidationError(
                {"detail": "Unable to verify or complete the Stripe transfer. Please retry."}
            ) from exc


class MockPayoutGateway:
    """In-memory deterministic payout gateway for testing and offline development."""

    def __init__(
        self,
        default_details_submitted: bool = True,
        default_payouts_enabled: bool = True,
    ) -> None:
        self.default_details_submitted = default_details_submitted
        self.default_payouts_enabled = default_payouts_enabled
        self.accounts: dict[str, dict[str, Any]] = {}
        self.transfers: list[dict[str, Any]] = []
        self.account_links: list[dict[str, Any]] = []

    def retrieve_account(self, account_id: str) -> dict[str, Any]:
        if account_id in self.accounts:
            return self.accounts[account_id]
        return {
            "id": account_id,
            "details_submitted": self.default_details_submitted,
            "payouts_enabled": self.default_payouts_enabled,
        }

    def create_account(
        self,
        email: str,
        country: str = "US",
        account_type: str = "express",
        capabilities: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        acct_id = f"acct_mock_{len(self.accounts) + 1}_{email.split('@')[0]}"
        class _MockAccount(dict):
            id = acct_id

        account = _MockAccount({
            "id": acct_id,
            "email": email,
            "country": country,
            "type": account_type,
            "metadata": metadata or {},
            "details_submitted": self.default_details_submitted,
            "payouts_enabled": self.default_payouts_enabled,
        })
        self.accounts[acct_id] = account
        return account

    def create_account_link(
        self,
        account_id: str,
        refresh_url: str,
        return_url: str,
        link_type: str = "account_onboarding",
    ) -> Any:
        class _MockLink:
            url = f"https://connect.stripe.mock/setup/{account_id}"

        link = _MockLink()
        self.account_links.append({
            "account": account_id,
            "refresh_url": refresh_url,
            "return_url": return_url,
            "type": link_type,
            "url": link.url,
        })
        return link

    def create_transfer(
        self,
        amount_cents: int,
        currency: str,
        destination: str,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        transfer = {
            "id": f"tr_mock_{len(self.transfers) + 1}",
            "amount": amount_cents,
            "currency": currency.lower(),
            "destination": destination,
            "metadata": metadata or {},
            "idempotency_key": idempotency_key,
            "status": "paid",
        }
        self.transfers.append(transfer)
        return transfer


def get_payout_gateway() -> PayoutGateway:
    """Factory to retrieve the active payout gateway configured in settings."""
    backend_path = getattr(
        settings,
        "PAYOUT_GATEWAY_BACKEND",
        "store.payout_gateway.StripePayoutGateway",
    )
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()
