from unittest.mock import patch

from django.test import TestCase
from core.services.bank_validation import ValidationResult
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from core.bank_accounts import (
    BankAccountsView,
    StripePayoutDestinationsView,
    payout_account_details,
    stripe_payout_destination_details,
)
from core.models import BankAccount, StripePayoutDestination, User


class BankAccountsTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email='bank@test.com', password='test-password')
        self.other = User.objects.create_user(email='other-bank@test.com', password='test-password')
        self.factory = APIRequestFactory()
        self.bank_validation = patch('core.bank_accounts.USBankValidationService.validate_account', return_value=ValidationResult(True, 'verified')).start()
        self.addCleanup(patch.stopall)

    def request(
        self,
        method: str,
        data: dict | None = None,
        pk: int | None = None,
        user: User | None = None,
    ) -> Response:
        request = getattr(self.factory, method)('/bank-accounts/', data or {}, format='json')
        force_authenticate(request, user or self.user)
        return BankAccountsView.as_view()(request, **({'pk': pk} if pk else {}))

    def create(self, name: str = 'Example Bank', routing_number: str = '021000021') -> Response:
        return self.request('post', {
            'bank_name': name, 'account_holder_name': 'Account Owner',
            'account_number': '1234567890', 'routing_number': routing_number,
        })

    def test_encrypted_storage_masked_responses_and_owner_scope(self) -> None:
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn('account_number', response.data)
        self.assertEqual(response.data['account_last4'], '7890')
        account = BankAccount.objects.get(pk=response.data['id'])
        self.assertNotIn('1234567890', account.account_number_encrypted)
        self.assertEqual(payout_account_details(self.user, account.pk)['account_number'], '1234567890')
        self.assertEqual(self.request('get', pk=account.pk, user=self.other).status_code, 404)
        self.assertEqual(self.request('patch', {'bank_name': 'Changed'}, pk=account.pk, user=self.other).status_code, 404)
        self.assertEqual(self.request('delete', pk=account.pk, user=self.other).status_code, 404)

    def test_default_switch_edit_and_delete_preserve_one_default(self) -> None:
        first = self.create().data['id']
        second = self.create('Second Bank').data['id']
        self.assertEqual(self.request('patch', {'is_default': True}, pk=second).status_code, 200)
        self.assertFalse(BankAccount.objects.get(pk=first).is_default)
        self.assertTrue(BankAccount.objects.get(pk=second).is_default)
        self.assertEqual([row['id'] for row in self.request('get').data if row['is_default']], [second])
        self.assertEqual(self.request('patch', {'bank_name': 'Renamed'}, pk=second).status_code, 200)
        self.assertEqual(self.request('delete', pk=second).status_code, 204)
        self.assertTrue(BankAccount.objects.get(pk=first).is_default)

    def test_unsupported_collection_and_detail_methods_return_405(self) -> None:
        self.assertEqual(self.request('patch', {'bank_name': 'Invalid'}).status_code, 405)
        self.assertEqual(self.request('delete').status_code, 405)
        self.assertEqual(self.request('post', {}, pk=123).status_code, 405)

    def test_search_and_default_filter_are_combined_with_owner_scope(self) -> None:
        self.create('First Bank')
        second = self.create('Second Bank').data['id']
        response = self.request('get', {'search': 'Second', 'is_default': 'false'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([account['id'] for account in response.data], [second])


    def test_invalid_routing_checksum_or_closed_account_is_rejected(self) -> None:
        # Invalid checksum
        bad_checksum = self.request('post', {
            'bank_name': 'Bad Routing Bank', 'account_holder_name': 'Owner',
            'account_number': '1234567890', 'routing_number': '123456789',
        })
        self.assertEqual(bad_checksum.status_code, 400)
        self.assertIn('routing_number', bad_checksum.data['error']['details'])

        # Non-US rejection
        non_us = self.request('post', {
            'bank_name': 'Foreign Bank', 'account_holder_name': 'Owner',
            'account_number': '1234567890', 'routing_number': '021000021',
            'country': 'NG',
        })
        self.assertEqual(non_us.status_code, 400)
        self.assertIn('country', non_us.data['error']['details'])

        self.bank_validation.return_value = ValidationResult(False, "failed", "Account could not be verified.")
        # The provider rejects a closed account.
        closed_account = self.request('post', {
            'bank_name': 'Closed Account Bank', 'account_holder_name': 'Owner',
            'account_number': '123450000', 'routing_number': '021000021',
        })
        self.assertEqual(closed_account.status_code, 400)
        self.assertIn('non_field_errors', closed_account.data['error']['details'])

    def test_partial_number_edit_revalidates_with_stored_counterpart(self) -> None:
        account_id = self.create().data['id']
        response = self.request('patch', {'account_number': '9876543210'}, pk=account_id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.bank_validation.call_args.kwargs['routing_number'], '021000021')
        self.assertEqual(self.bank_validation.call_args.kwargs['account_number'], '9876543210')

    def test_failed_provider_edit_preserves_account(self) -> None:
        account_id = self.create().data['id']
        self.bank_validation.return_value = ValidationResult(False, 'failed', 'Bank verification unavailable.')
        response = self.request('patch', {'account_number': '9876543210'}, pk=account_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payout_account_details(self.user, account_id)['account_number'], '1234567890')



class StripePayoutDestinationsTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email='stripe-dest@test.com', password='test-password')
        self.other = User.objects.create_user(email='other-stripe-dest@test.com', password='test-password')
        self.factory = APIRequestFactory()

    def request(
        self,
        method: str,
        data: dict | None = None,
        pk: int | None = None,
        user: User | None = None,
    ) -> Response:
        request = getattr(self.factory, method)('/stripe-payout-destinations/', data or {}, format='json')
        force_authenticate(request, user or self.user)
        return StripePayoutDestinationsView.as_view()(request, **({'pk': pk} if pk else {}))

    def create(
        self,
        label: str = 'Treasury Stripe',
        account_id: str = 'acct_1234567890ABCDE',
    ) -> Response:
        return self.request('post', {'label': label, 'stripe_account_id': account_id})

    def test_stripe_account_id_is_encrypted_and_owner_scoped(self) -> None:
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn('stripe_account_id', response.data)
        self.assertEqual(response.data['account_id_last4'], 'BCDE')
        destination = StripePayoutDestination.objects.get(pk=response.data['id'])
        self.assertNotIn('acct_1234567890ABCDE', destination.account_id_encrypted)
        self.assertEqual(
            stripe_payout_destination_details(self.user, destination.pk),
            {
                'destination_type': 'stripe',
                'destination': 'acct_1234567890ABCDE',
                'destination_label': 'Treasury Stripe',
            },
        )
        self.assertEqual(self.request('get', pk=destination.pk, user=self.other).status_code, 404)
        self.assertEqual(self.request('patch', {'label': 'Other'}, pk=destination.pk, user=self.other).status_code, 404)
        self.assertEqual(self.request('delete', pk=destination.pk, user=self.other).status_code, 404)

    def test_default_switch_and_delete_keep_one_default(self) -> None:
        first = self.create().data['id']
        second = self.create('Second Stripe', 'acct_ABCDEFGHIJK12345').data['id']
        self.assertEqual(self.request('patch', {'is_default': True}, pk=second).status_code, 200)
        self.assertFalse(StripePayoutDestination.objects.get(pk=first).is_default)
        self.assertEqual(self.request('patch', {'is_default': False}, pk=second).status_code, 400)
        self.assertEqual(self.request('delete', pk=second).status_code, 204)
        self.assertTrue(StripePayoutDestination.objects.get(pk=first).is_default)

    def test_invalid_connected_account_id_is_rejected(self) -> None:
        response = self.create(account_id='not_a_stripe_account')
        self.assertEqual(response.status_code, 400)
