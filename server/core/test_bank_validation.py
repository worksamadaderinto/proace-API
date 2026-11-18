from unittest.mock import patch

import pytest
from django.test import override_settings

from core.services import credit_user_wallet
from core.services.bank_validation import USBankValidationService, validate_us_routing_number


def test_wallet_import_survives_services_package_conversion() -> None:
    assert callable(credit_user_wallet)


@pytest.mark.parametrize('routing', ['000000000', '021000021\n', '０２１００００２１', '123456789'])
def test_invalid_routing_is_rejected(routing: str) -> None:
    assert not validate_us_routing_number(routing)


@pytest.mark.parametrize('environment', ['production', 'sandbox', 'test'])
def test_missing_credentials_never_simulate_verified_accounts(environment: str) -> None:
    service = USBankValidationService()
    service.client_id = ''
    service.client_secret = ''
    service.environment = environment
    with patch.object(service, '_call_usbank_api') as provider:
        result = service.validate_account(routing_number='021000021', account_number='123456789', account_holder_name='Fixture Owner')
    assert not result.is_valid
    assert result.status == 'failed'
    provider.assert_not_called()


@pytest.mark.parametrize('match', ['', 'PARTIAL_MATCH', 'ACCEPTABLE', 'MISMATCH'])
def test_incomplete_owner_match_cannot_verify_account(match: str) -> None:
    result = USBankValidationService()._parse_usbank_response({'accountStatus': 'OPEN', 'ownerMatchResult': match, 'message': 'sensitive provider detail'})
    assert not result.is_valid
    assert 'sensitive' not in result.reason


@override_settings(DEBUG=False)
def test_sandbox_credentials_cannot_verify_production_accounts() -> None:
    service = USBankValidationService()
    service.client_id = 'fixture'
    service.client_secret = 'fixture'
    service.environment = 'sandbox'
    with patch.object(service, '_call_usbank_api') as provider:
        result = service.validate_account(routing_number='021000021', account_number='123456789', account_holder_name='Fixture Owner')
    assert not result.is_valid
    provider.assert_not_called()
