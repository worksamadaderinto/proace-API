"""Explicit selected-record actions with existing domain policies and atomic rollback."""
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema
from core.access import IsBackoffice, is_admin
from core.models import User
from payment.models import Coupon, TaxJurisdiction
from store.models import Store
from staff.serializers import StaffPermissionSerializer, StoreModerationSerializer


class BulkActionSerializer(serializers.Serializer):
    resource = serializers.ChoiceField(choices=['taxes', 'coupons', 'staff', 'users', 'stores'])
    ids = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False)
    is_active = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def validate(self, attrs):
        if type(self.initial_data.get('is_active')) is not bool:
            raise ValidationError({'is_active': 'Provide an explicit JSON boolean.'})
        ids = attrs['ids']
        limit = getattr(settings, 'ADMIN_BULK_MAX_RECORDS', 100)
        if len(ids) > limit or len(ids) != len(set(ids)):
            raise ValidationError({'ids': f'Select up to {limit} distinct records.'})
        return attrs


class BulkActionResponseSerializer(serializers.Serializer):
    selected_count = serializers.IntegerField()
    updated_count = serializers.IntegerField()
    is_active = serializers.BooleanField()


class BulkCapabilitySerializer(serializers.Serializer):
    max_records = serializers.IntegerField(min_value=1)


class BulkActionView(APIView):
    permission_classes = [IsAuthenticated, IsBackoffice]

    @extend_schema(responses=BulkCapabilitySerializer)
    def get(self, request):
        return Response({'max_records': getattr(settings, 'ADMIN_BULK_MAX_RECORDS', 100)})

    @extend_schema(request=BulkActionSerializer, responses=BulkActionResponseSerializer)
    @transaction.atomic
    def post(self, request):
        inputs = BulkActionSerializer(data=request.data)
        inputs.is_valid(raise_exception=True)
        data = inputs.validated_data
        resource, active = data['resource'], data['is_active']
        if resource in {'staff', 'stores'} and not is_admin(request.user):
            raise PermissionDenied('Administrator access is required for this action.')
        model = {'taxes': TaxJurisdiction, 'coupons': Coupon, 'staff': User, 'users': User, 'stores': Store}[resource]
        rows = list(model.objects.select_for_update().filter(pk__in=data['ids']).order_by('pk'))
        if len(rows) != len(data['ids']):
            raise ValidationError('Some selected records are unavailable. Refresh the listing and select again.')
        # Check the entire selection before making changes or queuing notifications.
        for row in rows:
            if resource in {'staff', 'users'}:
                if row.pk == request.user.pk or row.is_superuser or row.is_business_admin:
                    raise PermissionDenied('Your own account and administrator accounts cannot be changed here.')
                if resource == 'staff' and not row.is_staff:
                    raise PermissionDenied('Select staff accounts from the staff listing.')
                if row.is_staff and not is_admin(request.user):
                    raise PermissionDenied('Staff cannot change other operators.')
                if active and row.is_staff and not row.staff_email_verified:
                    raise ValidationError('Every selected staff member must verify their email before activation.')
            if resource == 'stores' and (row.is_official or row.status not in {Store.STATUS_ACTIVE, Store.STATUS_BLOCKED}):
                raise ValidationError('Official and pending-review stores must be managed individually.')
        changed = 0
        for row in rows:
            previous = row.active if resource == 'coupons' else (row.status == Store.STATUS_ACTIVE if resource == 'stores' else row.is_active)
            if previous == active:
                continue
            if resource in {'users', 'staff'}:
                serializer = StaffPermissionSerializer(data={'is_active': active})
                serializer.is_valid(raise_exception=True)
                serializer.update(row)
                if row.is_staff:
                    row.staff_blocked_at = None if active else timezone.now()
                    if not active:
                        row.staff_invitation_secret = ''
                    row.save(update_fields=['staff_blocked_at', 'staff_invitation_secret'])
            elif resource == 'stores':
                serializer = StoreModerationSerializer(data={'reason': data.get('reason', '')})
                serializer.is_valid(raise_exception=True)
                serializer.unblock(row) if active else serializer.block(row, request.user)
                from notification.views import store_moderation_notification
                from product.models import Product
                from product.cache import invalidate_product_cache
                store_moderation_notification(row, actor=request.user, blocked=not active)
                for product in Product.objects.filter(store=row).only('id', 'store_id'):
                    transaction.on_commit(lambda product=product: invalidate_product_cache(product))
            else:
                field = 'active' if resource == 'coupons' else 'is_active'
                setattr(row, field, active)
                row.save(update_fields=[field, 'updated'])
            changed += 1
        return Response({'selected_count': len(rows), 'updated_count': changed, 'is_active': active})
