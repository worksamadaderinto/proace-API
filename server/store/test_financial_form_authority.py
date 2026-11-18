"""Regression coverage for server-owned financial forms and masked snapshots."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.bank_accounts import bank_cipher
from core.models import BankAccount, User, UserWalletTransaction
from payment.models import CheckoutAttempt, Coupon, TaxJurisdiction
from payment.tax import LocalTaxProvider
from product.models import Product
from store.models import MerchantWallet, Store, StoreEarningsLedger, StorePayout
from store.serializers import StorePayoutSerializer


def verified_bank_account(user: User) -> BankAccount:
    """Create a verified, encrypted test destination without provider calls."""
    cipher = bank_cipher()
    return BankAccount.objects.create(
        user=user, country='US', account_type='checking', bank_name='Fixture Bank',
        account_holder_name='Fixture Holder', verification_status='verified',
        account_number_encrypted=cipher.encrypt(b'1234567890').decode(),
        routing_number_encrypted=cipher.encrypt(b'021000021').decode(),
        account_last4='7890', routing_last4='0021',
    )


@override_settings(ROOT_URLCONF='storefront.urls')
class PayoutAuthorityTests(TestCase):
    def setUp(self) -> None:
        self.owner = User.objects.create_user(email='payout-owner@example.test', password='fixture')
        self.other = User.objects.create_user(email='payout-other@example.test', password='fixture')
        self.store = Store.objects.create(
            user=self.owner, name='Payout fixture', status=Store.STATUS_ACTIVE,
            verified_at=timezone.now(),
        )
        self.wallet = MerchantWallet.objects.create(user=self.owner)
        StoreEarningsLedger.objects.create(
            wallet=self.wallet, store=self.store, entry_type=StoreEarningsLedger.TYPE_SALE,
            gross_amount=Decimal('100'), fee_amount=Decimal('0'), net_amount=Decimal('100'),
            status=StoreEarningsLedger.STATUS_AVAILABLE,
            available_at=timezone.now() - timedelta(days=1),
        )
        self.bank = verified_bank_account(self.owner)
        self.foreign_bank = verified_bank_account(self.other)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.paths = ('/api/v1/stores/wallet/payouts/', f'/api/v1/stores/{self.store.pk}/payouts/')

    def test_destination_requires_verified_owned_saved_record_before_reserving(self) -> None:
        for state in ('pending', 'failed', 'verified'):
            self.bank.verification_status = state
            self.bank.save(update_fields=['verification_status'])
            destinations = [
                {'account_details': {'bank_name': 'Inline', 'account_number': 'anything'}},
                {'bank_account_id': self.foreign_bank.pk},
                {'bank_account_id': max(self.bank.pk, self.foreign_bank.pk) + 1000},
            ]
            if state != 'verified':
                destinations.append({'bank_account_id': self.bank.pk})
            for path in self.paths:
                for destination in destinations:
                    response = self.client.post(path, {
                        'amount': '5.00', 'payout_method': 'bank_transfer', **destination,
                    }, format='json')
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertFalse(StorePayout.objects.exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal('0'))

    def test_saved_snapshot_masks_responses_and_preserves_ownership(self) -> None:
        for path in self.paths:
            response = self.client.post(path, {
                'amount': '5.00', 'bank_account_id': self.bank.pk,
                'account_details': {'account_number': 'client-controlled', 'secret': 'omit'},
                'status': 'completed', 'wallet': 99999,
            }, format='json')
            self.assertEqual(response.status_code, 201, response.data)
            payout = StorePayout.objects.get(pk=response.data['id'])
            self.assertEqual(payout.wallet.user_id, self.owner.pk)
            self.assertEqual(payout.status, StorePayout.STATUS_PENDING)
            self.assertEqual(payout.account_details['account_number'], '1234567890')
            self.assertEqual(payout.account_details['routing_number'], '021000021')
            self.assertNotIn('secret', payout.account_details)
            self.assertEqual(response.data['account_details'], {
                'bank_name': 'Fixture Bank', 'account_holder_name': 'Fixture Holder',
                'account_last4': '7890', 'routing_last4': '0021',
            })
        listing = self.client.get(self.paths[0])
        self.assertEqual(listing.data['count'], 2)
        self.assertNotIn('1234567890', str(listing.data))
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.paths[0]).data['count'], 0)
        self.assertEqual(self.client.get(self.paths[1]).status_code, 404)
        staff = User.objects.create_staffuser(email='payout-staff@example.test', password='fixture')
        self.client.force_authenticate(staff)
        listing = self.client.get('/api/v1/admin/staff/payouts/')
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertNotIn('1234567890', str(listing.data))
        payout.refresh_from_db()
        self.assertEqual(payout.account_details['account_number'], '1234567890')

    def test_legacy_snapshot_is_allowlisted_without_mutation(self) -> None:
        details = {'bank_name': 'Legacy', 'account_number': '987654321',
                   'routing_number': '021000021', 'provider_secret': 'omit'}
        payout = StorePayout(amount=Decimal('5'), account_details=details)
        self.assertEqual(StorePayoutSerializer(payout).data['account_details'], {
            'bank_name': 'Legacy', 'account_last4': '4321', 'routing_last4': '0021',
        })
        self.assertEqual(payout.account_details, details)


@override_settings(ROOT_URLCONF='storefront.urls')
class CouponTaxAuthorityTests(TestCase):
    def setUp(self) -> None:
        self.admin = User.objects.create_superuser(email='form-admin@example.test', password='fixture')
        self.customer = User.objects.create_user(email='form-customer@example.test', password='fixture')
        store = Store.objects.create(user=self.customer, name='Coupon fixture')
        self.product = Product.objects.create(
            store=store, title='Coupon product', description='Fixture',
            price=Decimal('20'), discount=0, available=3,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_both_coupon_routes_preserve_target_and_server_usage_creator(self) -> None:
        for index, path in enumerate(('/api/v1/admin/coupons/', '/api/v1/admin/staff/coupons/')):
            payload = {
                'code': f'TARGET{index}', 'valid_to': (timezone.now() + timedelta(days=1)).isoformat(),
                'discount': 10, 'num_available': 10, 'num_used': 9,
                'type': Coupon.PRODUCT_QUANTITY, 'product': self.product.pk,
                'minimum_quantity': 2, 'created_by': self.customer.pk,
            }
            response = self.client.post(path, payload, format='json')
            self.assertEqual(response.status_code, 201, response.data)
            coupon = Coupon.objects.get(pk=response.data['id'])
            self.assertEqual(coupon.type, Coupon.PRODUCT_QUANTITY)
            self.assertEqual(coupon.product_id, self.product.pk)
            self.assertEqual(coupon.minimum_quantity, 2)
            self.assertEqual(coupon.num_used, 0)
            self.assertEqual(coupon.created_by_id, self.admin.pk)
            payload.update(code=f'INVALID{index}', product=None)
            self.assertEqual(self.client.post(path, payload, format='json').status_code, 400)
            self.client.force_authenticate(self.customer)
            self.assertEqual(self.client.post(path, payload, format='json').status_code, 403)
            self.client.force_authenticate(self.admin)

    def test_normalized_tax_rule_is_used_and_invalid_destinations_rejected(self) -> None:
        response = self.client.post('/api/v1/admin/taxes/', {
            'country': 'us', 'state': 'California', 'postal_code': '902',
            'name': 'Prefix fixture', 'rate': '0.12345', 'tax_shipping': False,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['country'], 'US')
        self.assertEqual(response.data['state'], 'CA')
        tax = LocalTaxProvider().calculate_tax(
            [{'unit_price': '100.00', 'quantity': 1, 'is_digital': False}],
            Decimal('100'), Decimal('0'), {'country': 'US', 'state': 'CA', 'zip': '90210-1234'},
        )
        self.assertEqual(tax.rate, Decimal('0.12345'))
        self.assertEqual(tax.amount, Decimal('12.35'))
        path = f"/api/v1/admin/taxes/{response.data['id']}/"
        for change in ({'country': 'GB'}, {'state': 'ZZ'}, {'postal_code': '90210-1234'},
                       {'postal_code': 'ABCDE'}, {'postal_code': '１２３'}):
            self.assertEqual(self.client.patch(path, change, format='json').status_code, 400)
            payload = {'country': 'US', 'state': 'CA', 'name': 'Invalid', 'rate': '0.1', **change}
            self.assertEqual(self.client.post('/api/v1/admin/taxes/', payload, format='json').status_code, 400)
        self.assertEqual(TaxJurisdiction.objects.count(), 1)
        self.assertEqual(self.client.patch(path, {'postal_code': '', 'rate': '0'}, format='json').status_code, 200)
        self.client.force_authenticate(self.customer)
        self.assertEqual(self.client.patch(path, {'rate': '1'}, format='json').status_code, 403)


@override_settings(ROOT_URLCONF='storefront.urls', STRIPE_SECRET='fixture-only')
class SplitPaymentPrecisionTests(TestCase):
    def test_bad_precision_rejected_before_cart_provider_or_wallet_mutation(self) -> None:
        customer = User.objects.create_user(email='precision@example.test', password='fixture')
        client = APIClient()
        client.force_authenticate(customer)
        with patch('storefront.views.stripe.checkout.Session.create') as create:
            for amount in ('1.005', '-0.001', '', 'NaN', 'Infinity', '10000000000.00', None, True):
                response = client.post('/api/v1/checkout/wallet-session/', {
                    'checkout_key': str(uuid4()), 'wallet_amount': amount,
                }, format='json')
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn('wallet_amount', str(response.data))
            create.assert_not_called()
        self.assertFalse(CheckoutAttempt.objects.exists())
        self.assertFalse(UserWalletTransaction.objects.exists())
