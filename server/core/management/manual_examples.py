"""Development-only, additive examples for the screenshot training manual."""
from decimal import Decimal

from django.utils import timezone
from django.db.models import Q

from core.models import User
from product.models import Product
from store.models import MerchantWallet, Store, StoreAddress, StoreEarningsLedger, StoreInfo, StorePayout
from store.services import request_merchant_payout


def seed_manual_examples(password: str) -> None:
    user, created = User.objects.get_or_create(
        email='manual-partner@proace.local',
        defaults={'first_name': 'Cedar', 'last_name': 'Partner', 'is_active': True, 'is_partner_merchant': True},
    )
    if created:
        user.set_password(password)
        user.save(update_fields=['password'])
    store, _ = Store.objects.get_or_create(
        username='manual-cedar',
        defaults={'user': user, 'name': 'Cedar Partner Training', 'status': Store.STATUS_ACTIVE,
                  'is_partner': True, 'verified_at': timezone.now()},
    )
    StoreInfo.objects.get_or_create(store=store, defaults={'email': user.email, 'bio': 'Local training store for the web manual.'})
    StoreAddress.objects.get_or_create(store=store, is_default=True, defaults={
        'address': '100 N Tampa Street', 'city': 'Tampa', 'state': 'Florida', 'country': 'United States', 'zip': '33602',
    })
    source = Product.objects.filter(visibility=True, image_url__gt='').first()
    Product.objects.get_or_create(store=store, title='Partner business workbook', defaults={
        'description': 'A workbook for planning a small business. Local training example.',
        'discount': 0, 'category': 'books', 'price': Decimal('25.00'), 'available': 20, 'visibility': True,
        'image_url': source.image_url if source else '',
    })
    wallet, _ = MerchantWallet.objects.get_or_create(user=user)
    StoreEarningsLedger.objects.get_or_create(reference='MANUAL-CLEARED-SALES', store=store, defaults={
        'wallet': wallet, 'entry_type': StoreEarningsLedger.TYPE_SALE,
        'gross_amount': Decimal('500.00'), 'fee_amount': Decimal('0.00'), 'net_amount': Decimal('500.00'),
        'status': StoreEarningsLedger.STATUS_AVAILABLE, 'available_at': timezone.now(), 'cleared_at': timezone.now(),
        'description': 'Local training opening sales balance',
    })
    for label, amount in [('approval', '50.00'), ('decline', '25.00'), ('pending', '75.00')]:
        note = f'Local manual training: {label}'
        if not StorePayout.objects.filter(Q(account_details__training_case=label) | Q(notes=note) | Q(amount=Decimal(amount), store=store), wallet=wallet).exists():
            request_merchant_payout(user, Decimal(amount), StorePayout.METHOD_BANK_TRANSFER,
                                    {'training_case': label, 'bank_name': 'Training bank', 'account_holder_name': 'Cedar Partner', 'account_last4': '6789'},
                                    store=store, notes=note)
    from core.models import BankAccount
    from core.bank_accounts import bank_cipher
    from payment.models import Order, OrderFulfillment
    from staff.models import PlatformCommissionWithdrawal
    for email in ['shopper@proace.local', 'business-admin@proace.local', 'merchant@proace.local']:
        owner = User.objects.filter(email=email).first()
        if owner:
            BankAccount.objects.get_or_create(user=owner, bank_name='Manual training bank', defaults={
                'account_holder_name': 'Training Account', 'account_last4': '6789', 'routing_last4': '0021',
                'account_number_encrypted': bank_cipher().encrypt(b'0000123456789').decode(),
                'routing_number_encrypted': bank_cipher().encrypt(b'021000021').decode(),
            })
    admin = User.objects.filter(email='business-admin@proace.local').first()
    if admin:
        PlatformCommissionWithdrawal.objects.get_or_create(reference='PCW-MANUAL-01', defaults={
            'amount': Decimal('100.00'), 'payout_method': 'bank', 'requested_by': admin,
            'account_details': {'bank_name': 'Manual training bank', 'account_last4': '6789'},
            'processing_note': 'Local manual training example',
        })
    merchant_store = Store.objects.filter(username='demo-booster').first()
    if merchant_store and not StorePayout.objects.filter(store=merchant_store, notes='Local manual merchant example').exists():
        request_merchant_payout(merchant_store.user, Decimal('10.00'), StorePayout.METHOD_BANK_TRANSFER,
                                {'bank_name': 'Manual training bank', 'account_number': '0000123456789'},
                                store=merchant_store, notes='Local manual merchant example')
    buyer = User.objects.filter(email='shopper@proace.local').first()
    if buyer:
        product = Product.objects.get(store=store, title='Partner business workbook')
        order, _ = Order.objects.get_or_create(orderId='MANUAL-RETURN', defaults={
            'user': buyer, 'status': 'delivered', 'ordered': True, 'total': Decimal('25.00'),
            'subtotal': Decimal('25.00'), 'paid_total': Decimal('25.00'), 'wallet_paid': Decimal('25.00'),
            'payment_type': 'wallet', 'delivered_at': timezone.now(), 'buyer_email': buyer.email,
            'items_snapshot': [{'product': product.pk, 'product_id': product.pk, 'title': product.title,
                                'store': store.pk, 'store_id': store.pk, 'quantity': 1, 'unit_price': '25.00',
                                'price': '25.00', 'total': '25.00', 'is_digital': False}],
            'address_snapshot': {'address': '100 N Tampa Street', 'city': 'Tampa', 'state': 'Florida', 'zip': '33602', 'country': 'United States'},
        })
        OrderFulfillment.objects.get_or_create(order=order, store=store, defaults={'status': 'delivered', 'delivered_at': order.delivered_at})
