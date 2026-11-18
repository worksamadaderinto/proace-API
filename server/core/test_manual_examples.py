from decimal import Decimal
from django.test import TestCase, override_settings
from django.core.management import call_command
from core.models import User
from store.models import StorePayout
from store.services import calculate_merchant_financials, process_payout_status_change


@override_settings(DEBUG=True)
class ManualExamplesTests(TestCase):
    def test_seed_preserves_credentials_and_terminal_payouts(self):
        call_command('populate_fake_data', manual_only=True, password='LocalTraining123!')
        user = User.objects.get(email='manual-partner@proace.local')
        original_password = user.password
        self.assertEqual(StorePayout.objects.filter(wallet__user=user).count(), 3)
        self.assertEqual(Decimal(calculate_merchant_financials(user)['available_balance']), Decimal('350.00'))
        payout = StorePayout.objects.get(wallet__user=user, amount=Decimal('25.00'))
        process_payout_status_change(payout, 'reject', user, notes='Training rejection')
        self.assertEqual(Decimal(calculate_merchant_financials(user)['available_balance']), Decimal('375.00'))
        call_command('populate_fake_data', manual_only=True, password='DifferentTraining123!')
        user.refresh_from_db()
        payout.refresh_from_db()
        self.assertEqual(user.password, original_password)
        self.assertEqual(payout.status, StorePayout.STATUS_REJECTED)
        self.assertEqual(StorePayout.objects.filter(wallet__user=user).count(), 3)
