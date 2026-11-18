from store.transfers import UploadLimits, UploadRequest, UploadState
from core.listing_filters import ListingQuerySerializer, filter_records, record_page
from zoneinfo import ZoneInfo
from core.access import IsBusinessAdmin, is_admin, is_backoffice
from datetime import datetime, timedelta
import logging
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.utils.dateparse import parse_date, parse_datetime
from django.db import transaction
from django.db.models import prefetch_related_objects
from django.db.models import Count, DecimalField, Exists, ExpressionWrapper, F, OuterRef, Prefetch, Q, QuerySet, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from core.parsers import (
    ValidatedJSONParser as JSONParser,
    ValidatedFormParser as FormParser,
    ValidatedMultiPartParser as MultiPartParser,
)
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import BasePermission, IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view

from cart.models import CartItem
from core.models import Refund
from payment.models import Order
from payment.fulfillment import available_fulfillment_statuses
from product.models import Product, ProductImg, Specification
from store.models import (
    MerchantWallet, Schedule, Store, StoreAddress, StoreEarningsLedger,
    StoreInfo, StorePayout, StoreTeamMember,
)
from store.serializers import (
    MerchantRefundQuerySerializer,
    MerchantRefundSerializer,
    MerchantTrackingSerializer,
    DashboardQuerySerializer,
    InventorySerializer,
    MerchantImageSerializer,
    MerchantPayoutCreateSerializer,
    MerchantProductSerializer,
    MerchantProductUploadSerializer,
    MerchantSpecificationSerializer,
    MerchantWalletSerializer,
    PayoutCreateSerializer,
    ScheduleSerializer,
    StoreAddressSerializer,
    StoreEarningsLedgerSerializer,
    StoreInfoSerializer,
    StoreBrandingImageSerializer,
    StoreOnboardingSerializer,
    StorePayoutSerializer,
    StoreSerializer,
    StoreSettingsRequestSerializer,
    StoreSettingsResponseSerializer,
    StoreTeamMemberSerializer,
    StoreTeamMemberInviteSerializer,
    StoreTeamMemberUpdateSerializer,
    StoreTeamQuerySerializer,
)
from store.services import (
    calculate_merchant_financials, calculate_store_financials,
    get_or_create_merchant_wallet, request_merchant_payout,
    can_manage_products, can_manage_orders, can_manage_team, can_manage_financials,
    get_store_user_role, PRODUCT_MANAGEMENT_ROLES,
)
from utils.functions import normalize_media_url
from utils.digital_previews import is_image_digital_asset

logger = logging.getLogger(__name__)


class InvitationDeliveryUnavailable(APIException):
    status_code = 503
    default_detail = "Unable to queue the invitation email. Please try again."
    default_code = "invitation_unavailable"


class MerchantPagination(LimitOffsetPagination):
    default_limit = 20
    max_limit = 100


def merchant_date_boundary(value: str, field: str, *, end: bool = False) -> datetime:
    """Accept ISO dates/timestamps and reject malformed listing boundaries."""
    try:
        day = parse_date(value)
        boundary = (
            datetime.combine(day, datetime.max.time() if end else datetime.min.time())
            if day is not None else parse_datetime(value)
        )
        if boundary is None:
            raise ValueError
        return timezone.make_aware(boundary) if timezone.is_naive(boundary) else boundary
    except (ValueError, TypeError, OverflowError) as error:
        raise ValidationError({field: 'Enter a valid ISO date or timestamp.'}) from error


def invalidate_store_products(store):
    for product in Product.objects.filter(store=store):
        invalidate_catalog(product)


def is_protected_digital_preview_url(value: object) -> bool:
    return isinstance(value, str) and 'digital-previews/' in value


@extend_schema_view(
    list=extend_schema(parameters=[ListingQuerySerializer]),
)
class StoreReviewViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = Store.objects.none()
    serializer_class = StoreSerializer
    pagination_class = MerchantPagination

    def get_permissions(self) -> list[BasePermission]:
        if self.action in ("approve", "block"):
            return [IsBusinessAdmin()]
        return [IsAdminUser()]

    def get_queryset(self) -> QuerySet[Store]:
        stores = Store.objects.select_related('user', 'verified_by', 'blocked_by').prefetch_related('storeinfo_set', 'assigned_staff')
        status_filter = self.request.query_params.get('status', Store.STATUS_PENDING)
        if status_filter not in dict(Store.STATUS_CHOICES):
            raise ValidationError({'status': 'Choose pending, active or blocked.'})
        stores = stores.filter(status=status_filter)
        stores = filter_records(
            stores,
            self.request.query_params,
            search_fields=(
                'name',
                'username',
                'user__email',
                'user__first_name',
                'user__last_name',
            ),
            fields={'is_official': 'is_official'},
        )
        ordering = self.request.query_params.get('ordering', 'created')
        allowed_ordering = {'created', '-created', 'name', '-name', 'status', '-status'}
        if ordering not in allowed_ordering:
            raise ValidationError({'ordering': 'Order by created, name, or status.'})
        return stores.order_by(ordering, 'pk')

    @transaction.atomic
    def approve(self, request: Request, pk: str | None = None) -> Response:
        store = get_object_or_404(Store.objects.select_for_update(), pk=pk)
        if store.status == Store.STATUS_ACTIVE and store.verified_at:
            return Response(StoreSerializer(store).data)
        store.status = Store.STATUS_ACTIVE
        store.verified_at = timezone.now()
        store.verified_by = request.user
        store.blocked_reason = ''
        store.blocked_at = None
        store.blocked_by = None
        store.save(update_fields=[
            'status', 'verified_at', 'verified_by', 'blocked_reason',
            'blocked_at', 'blocked_by', 'updated',
        ])

        from notification.dispatcher import get_notification_dispatcher

        get_notification_dispatcher().notify(
            recipient=store.user,
            actor=request.user,
            target=store,
            verb='store verified',
            description=f'{store.name} has been verified and is now public.',
            level='success',
            data={'event': 'store_verified', 'store_id': store.pk},
        )
        transaction.on_commit(lambda: invalidate_store_products(store))
        return Response(StoreSerializer(store).data)

    @transaction.atomic
    def block(self, request: Request, pk: str | None = None) -> Response:
        store = get_object_or_404(Store.objects.select_for_update(), pk=pk)
        if store.is_official:
            return Response(
                {"detail": "Official company stores cannot be blocked."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reason = serializers.CharField(allow_blank=False, trim_whitespace=True).run_validation(
            request.data.get('reason')
        )
        if store.status == Store.STATUS_BLOCKED and store.blocked_reason == reason:
            return Response(StoreSerializer(store).data)
        store.status = Store.STATUS_BLOCKED
        store.blocked_reason = reason
        store.blocked_at = timezone.now()
        store.blocked_by = request.user
        store.save(update_fields=['status', 'blocked_reason', 'blocked_at', 'blocked_by', 'updated'])

        from notification.dispatcher import get_notification_dispatcher

        get_notification_dispatcher().notify(
            recipient=store.user,
            actor=request.user,
            target=store,
            verb='store blocked',
            description=f'{store.name} is unavailable. Reason: {reason}',
            level='warning',
            data={'event': 'store_blocked', 'store_id': store.pk},
        )
        transaction.on_commit(lambda: invalidate_store_products(store))
        return Response(StoreSerializer(store).data)


def invalidate_catalog(product, *, invalidator: Any = None):
    from store.invalidation import get_catalog_invalidator

    inv = invalidator or get_catalog_invalidator()
    inv.invalidate(product)


class StoreViewSet(mixins.CreateModelMixin, mixins.ListModelMixin,
                   mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                   viewsets.GenericViewSet):
    queryset = Store.objects.none()
    permission_classes = [IsAuthenticated]
    pagination_class = MerchantPagination
    serializer_class = StoreSerializer

    def get_object(self) -> Store:
        store = super().get_object()
        from core.activity import record_store_activity
        record_store_activity(store)
        return store

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return Store.objects.none()
        from store.services import link_pending_store_team_invitations
        # Refresh invitations at store selection, rather than scanning on every order query.
        if self.action in {'list', 'retrieve'}:
            link_pending_store_team_invitations(user)
        if is_admin(user):
            # Platform administrators (superusers and business admins) oversee official company stores,
            # as well as any stores they personally own or are explicitly added to.
            qs = Store.objects.select_related('user').filter(
                Q(is_official=True) | Q(user=user) | Q(team_members__user=user, team_members__status=StoreTeamMember.STATUS_ACTIVE)
            ).distinct().order_by('-created', '-pk')
        else:
            # Staff members and merchant users: access is strictly granted through ownership
            # or explicit store team membership. Staff members do NOT automatically access official
            # stores; administrators must explicitly add staff members through the store's team.
            qs = Store.objects.select_related('user').filter(
                Q(user=user) | Q(team_members__user=user, team_members__status=StoreTeamMember.STATUS_ACTIVE),
            ).distinct().order_by('-created', '-pk')
        if self.action in {'list', 'retrieve'}:
            qs = qs.prefetch_related('assigned_staff')
        return qs

    @extend_schema(responses={204: None})
    @action(detail=True, methods=['delete'], url_path='delete')
    @transaction.atomic
    def delete_store(self, request: Request, pk: str | None = None) -> Response:
        store = self.get_object()
        if store.is_official:
            if not is_admin(request.user):
                raise PermissionDenied('Only administrators can delete official stores.')
        else:
            if store.user_id != request.user.id and not request.user.is_superuser:
                raise PermissionDenied('Only the primary store owner can delete this store.')
        # Use the same wallet lock as earnings/payout mutations before checking history.
        if store.user_id:
            type(store.user).objects.select_for_update().get(pk=store.user_id)
            MerchantWallet.objects.select_for_update().filter(user_id=store.user_id).first()
        store = Store.objects.select_for_update().get(pk=store.pk)
        if (
            store.ledger_entries.exists() or store.payouts.exists()
            or store.orderfulfillment_set.exists()
        ):
            raise ValidationError({
                'detail': 'Stores with earnings or payout history cannot be deleted. Unpublish their products instead.'
            })
        products = list(Product.objects.select_for_update().filter(store=store))
        if any(product.is_blocked for product in products):
            raise ValidationError(
                {
                    "detail": "Resolve blocked product reviews before deleting this store."
                }
            )
        store.delete()
        return Response(status=204)

    @action(detail=True, methods=['put'], url_path='staff')
    def assign_staff(self, request, pk=None):
        if not is_admin(request.user):
            raise PermissionDenied('Only administrators can manage staff assignments.')
        # Staff-to-store assignment logic is deprecated: all authenticated staff members
        # have global backoffice access and automatic access to all official stores.
        return Response({
            'detail': 'Staff-to-store assignment is deprecated. All authenticated staff have global backoffice access and automatic access to all official stores.',
            'staff_ids': [],
        })

    def perform_update(self, serializer: StoreSerializer) -> None:
        if not can_manage_team(serializer.instance, self.request.user):
            raise PermissionDenied(
                "Only store owners and administrators can edit store settings."
            )
        serializer.save()

    def get_serializer_class(self):
        return StoreOnboardingSerializer if self.action in {'create', 'create_store'} else StoreSerializer

    def perform_create(self, serializer):
        user = self.request.user
        if is_backoffice(user) and not is_admin(user):
            raise PermissionDenied('Only administrators can create official stores.')
        if is_admin(user):
            serializer.save(
                user=user,
                is_official=True,
                status=Store.STATUS_ACTIVE,
                verified_at=timezone.now(),
                verified_by=user,
            )
        else:
            # Serialize creation for this owner so simultaneous requests cannot exceed the limit.
            with transaction.atomic():
                type(user).objects.select_for_update().get(pk=user.pk)
                if Store.objects.filter(user=user).count() >= 10:
                    raise ValidationError({'detail': 'You can create a maximum of 10 stores.'})
                serializer.save(user=user, is_partner=user.is_partner_merchant)
                # Create the merchant's unified wallet with their first store so
                # the account exists before its first sale or payout setup.
                get_or_create_merchant_wallet(user)

    @action(detail=False, methods=['post'], url_path='create')
    def create_store(self, request):
        return self.create(request)

    @extend_schema(request=StoreSettingsRequestSerializer, responses=StoreSettingsResponseSerializer)
    @action(detail=True, methods=['patch'], url_path='settings')
    @transaction.atomic
    def update_settings(self, request: Request, pk: str | None = None) -> Response:
        accessible_store = self.get_object()
        store = Store.objects.select_for_update().get(pk=accessible_store.pk)
        if not can_manage_team(store, request.user):
            raise PermissionDenied("Only store owners and store administrators can edit store settings.")
        context = {'request': request, 'store': store}
        inputs = StoreSettingsRequestSerializer(data=request.data, context=context)
        inputs.is_valid(raise_exception=True)
        profile = StoreInfo.objects.filter(store=store).order_by('pk').first()
        address = StoreAddress.objects.filter(store=store, is_default=True).order_by('pk').first()
        store_data = StoreSerializer(store, data={'name': inputs.validated_data['name']}, partial=True, context=context)
        profile_data = StoreInfoSerializer(profile, data=inputs.validated_data['profile'], partial=True, context=context)
        address_data = StoreAddressSerializer(address, data=inputs.validated_data['address'], context=context)
        # Validate every section before writing; the transaction also rolls back
        # unexpected persistence failures after any individual save.
        for serializer in (store_data, profile_data, address_data):
            serializer.is_valid(raise_exception=True)
        store_data.save()
        profile_data.save(store=store)
        StoreAddress.objects.filter(store=store, is_default=True).update(is_default=False)
        address_data.save(store=store, is_default=True)
        from storefront.catalog_cache import invalidate_catalog_snapshot
        transaction.on_commit(lambda: invalidate_catalog_snapshot())
        transaction.on_commit(lambda: invalidate_store_products(store))
        return Response(StoreSettingsResponseSerializer({
            'store': store, 'profile': profile_data.instance, 'address': address_data.instance,
        }, context=context).data)

    @extend_schema(methods=['GET'], responses=StoreInfoSerializer)
    @extend_schema(methods=['PATCH'], request=StoreInfoSerializer, responses=StoreInfoSerializer)
    @action(detail=True, methods=['get', 'patch'])
    def profile(self, request: Request, pk: str | None = None) -> Response:
        store = self.get_object()
        profile = StoreInfo.objects.filter(store=store).order_by('pk').first()
        if request.method == 'GET':
            return Response(StoreInfoSerializer(profile, context={'store': store}).data if profile else {})
        if not can_manage_team(store, request.user):
            raise PermissionDenied(
                "Only store owners and administrators can edit the store profile."
            )
        with transaction.atomic():
            store = Store.objects.select_for_update().get(pk=store.pk)
            profile = StoreInfo.objects.filter(store=store).order_by('pk').first()
            serializer = StoreInfoSerializer(profile, data=request.data, partial=True, context={'store': store})
            serializer.is_valid(raise_exception=True)
            serializer.save(store=store)
            Store.objects.filter(pk=store.pk).update(updated=timezone.now())
            from storefront.catalog_cache import invalidate_catalog_snapshot
            transaction.on_commit(lambda: invalidate_catalog_snapshot())
            transaction.on_commit(lambda: invalidate_store_products(store))
        return Response(serializer.data)

    @extend_schema(methods=['POST'], request=StoreBrandingImageSerializer, responses=StoreInfoSerializer)
    @extend_schema(methods=['DELETE'], request=None, responses=StoreInfoSerializer)
    @action(
        detail=True,
        methods=['post', 'delete'],
        url_path=r'profile-images/(?P<kind>avatar|banner)',
        parser_classes=[MultiPartParser, FormParser],
    )
    def profile_images(
        self,
        request: Request,
        pk: str | None = None,
        kind: str | None = None,
    ) -> Response:
        store = self.get_object()
        if not can_manage_team(store, request.user):
            raise PermissionDenied(
                "Only store owners and administrators can edit store branding."
            )
        if kind not in {'avatar', 'banner'}:
            raise ValidationError({'detail': 'Choose an avatar or banner image.'})
        image = None
        if request.method == 'POST':
            serializer = StoreBrandingImageSerializer(data=request.data, context={'kind': kind})
            serializer.is_valid(raise_exception=True)
            image = serializer.validated_data['image']
        with transaction.atomic():
            Store.objects.select_for_update().get(pk=store.pk)
            profile = StoreInfo.objects.filter(store=store).order_by('pk').first()
            if profile is None:
                profile = StoreInfo(store=store)
            setattr(profile, f'{kind}_image', image or '')
            setattr(profile, f'{kind}_url', '')
            profile.save()
        return Response(StoreInfoSerializer(profile, context={'request': request}).data)

    @action(detail=True, methods=['post'], url_path='connect-stripe')
    def connect_stripe(self, request: Request, pk: str | None = None) -> Response:
        """Create/reuse a Stripe Express account and return its onboarding URL."""
        store = self.get_object()
        if store.user_id != request.user.id:
            raise PermissionDenied('Only the store owner can connect Stripe.')
        secret = getattr(settings, 'STRIPE_SECRET', '')
        if not secret:
            raise ValidationError({'detail': 'Stripe Connect is not configured yet.'})
        try:
            from store.payout_gateway import get_payout_gateway
            payout_gw = get_payout_gateway()
            with transaction.atomic():
                type(request.user).objects.select_for_update().get(pk=request.user.pk)
                wallet, _ = MerchantWallet.objects.get_or_create(user=request.user)
                if wallet.stripe_account_id.startswith('acct_mock_'):
                    # Demo placeholders do not identify a real Stripe account.
                    wallet.stripe_account_id = ''
                    wallet.stripe_details_submitted = False
                    wallet.stripe_payouts_enabled = False
                    wallet.save(update_fields=[
                        'stripe_account_id',
                        'stripe_details_submitted',
                        'stripe_payouts_enabled',
                        'updated',
                    ])
                if wallet.stripe_account_id:
                    try:
                        account = payout_gw.retrieve_account(wallet.stripe_account_id)
                        wallet.stripe_details_submitted = bool(account.get('details_submitted', False))
                        wallet.stripe_payouts_enabled = bool(account.get('payouts_enabled', False))
                        wallet.save(update_fields=[
                            'stripe_details_submitted',
                            'stripe_payouts_enabled',
                            'updated',
                        ])
                        if wallet.stripe_payouts_enabled:
                            return Response({
                                'connected': True,
                                'pending': False,
                                'url': '',
                                'stripe_account_id': wallet.stripe_account_id,
                            })
                        requirements = account.get('requirements') or {}
                        currently_due = requirements.get('currently_due') or []
                        if wallet.stripe_details_submitted and not currently_due:
                            return Response({
                                'connected': False,
                                'pending': True,
                                'url': '',
                                'stripe_account_id': wallet.stripe_account_id,
                            })
                    except Exception as exc:
                        is_missing = getattr(exc, 'code', None) == 'resource_missing' or 'No such account' in str(exc)
                        if not is_missing:
                            raise
                        wallet.stripe_account_id = ''
                        wallet.stripe_details_submitted = False
                        wallet.stripe_payouts_enabled = False
                        wallet.save(update_fields=[
                            'stripe_account_id',
                            'stripe_details_submitted',
                            'stripe_payouts_enabled',
                            'updated',
                        ])
                if not wallet.stripe_account_id:
                    account = payout_gw.create_account(
                        account_type='express',
                        email=request.user.email,
                        metadata={
                            'store_id': str(store.pk),
                            'user_id': str(request.user.pk),
                        },
                        idempotency_key=f'merchant-connect-user-{request.user.pk}',
                    )
                    wallet.stripe_account_id = account.id if hasattr(account, 'id') else account['id']
                    wallet.save(update_fields=['stripe_account_id', 'updated'])
                stripe_account_id = wallet.stripe_account_id

            # Stripe must return to the customer-facing Next.js app, not the API
            # process (which has no /merchant/settings route).
            base = f"{settings.FRONTEND_URL.rstrip('/')}/merchant/settings"
            link = payout_gw.create_account_link(
                account_id=stripe_account_id,
                refresh_url=f'{base}?stripe_onboarding=refresh',
                return_url=f'{base}?stripe_onboarding=return',
                link_type='account_onboarding',
            )
            return Response({
                'connected': False,
                'pending': False,
                'url': getattr(link, 'url', link.get('url') if isinstance(link, dict) else ''),
                'stripe_account_id': stripe_account_id,
            })
        except Exception as exc:
            logging.getLogger(__name__).exception('Stripe Connect onboarding failed for store %s', store.pk)
            raise ValidationError({'detail': 'Unable to start Stripe onboarding. Please try again shortly.'}) from exc

    @transaction.atomic
    @action(detail=True, methods=['get'], url_path='stripe-status')
    def stripe_status(self, request: Request, pk: str | None = None) -> Response:
        """Refresh Stripe onboarding flags and return safe payout setup state."""
        store = self.get_object()
        if store.user_id != request.user.id:
            raise PermissionDenied('Only the store owner can view Stripe payout status.')
        wallet, _ = MerchantWallet.objects.select_for_update().get_or_create(user=request.user)
        if not wallet.stripe_account_id or wallet.stripe_account_id.startswith('acct_mock_'):
            wallet.stripe_details_submitted = False
            wallet.stripe_payouts_enabled = False
            wallet.save(update_fields=['stripe_details_submitted', 'stripe_payouts_enabled', 'updated'])
            return Response({
                'connected': False,
                'details_submitted': False,
                'payouts_enabled': False,
                'currently_due': [],
            })
        try:
            from store.payout_gateway import get_payout_gateway
            payout_gw = get_payout_gateway()
            account = payout_gw.retrieve_account(wallet.stripe_account_id)
            wallet.stripe_details_submitted = bool(account.get('details_submitted', False))
            wallet.stripe_payouts_enabled = bool(account.get('payouts_enabled', False))
            wallet.save(update_fields=['stripe_details_submitted', 'stripe_payouts_enabled', 'updated'])
            requirements = account.get('requirements') or {}
            return Response({
                'connected': wallet.stripe_payouts_enabled,
                'details_submitted': wallet.stripe_details_submitted,
                'payouts_enabled': wallet.stripe_payouts_enabled,
                'currently_due': requirements.get('currently_due') or [],
            })
        except ValidationError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).exception('Stripe Connect status refresh failed for store %s', store.pk)
            raise ValidationError({'detail': 'Unable to refresh Stripe payout status. Please try again shortly.'}) from exc

    @extend_schema(methods=['GET'], responses=StoreAddressSerializer)
    @extend_schema(methods=['PUT'], request=StoreAddressSerializer, responses=StoreAddressSerializer)
    @action(detail=True, methods=['get', 'put'], url_path='pickup-address')
    def pickup_address(self, request: Request, pk: str | None = None) -> Response:
        store = self.get_object()
        address = StoreAddress.objects.filter(store=store, is_default=True).order_by('pk').first()
        if request.method == 'GET':
            return Response(StoreAddressSerializer(address).data if address else {})
        if not can_manage_team(store, request.user):
            raise PermissionDenied("Only store owners and store administrators can update pickup addresses.")
        with transaction.atomic():
            store = Store.objects.select_for_update().get(pk=store.pk)
            address = StoreAddress.objects.filter(store=store, is_default=True).order_by('pk').first()
            serializer = StoreAddressSerializer(address, data=request.data)
            serializer.is_valid(raise_exception=True)
            StoreAddress.objects.filter(store=store, is_default=True).update(is_default=False)
            serializer.save(store=store, is_default=True)
        return Response(serializer.data)

    @extend_schema(
        methods=["GET"],
        responses=StoreTeamMemberSerializer(many=True),
        parameters=[StoreTeamQuerySerializer],
        description="List active store team members with search, role and date filters.",
    )
    @extend_schema(
        methods=['POST'],
        request=StoreTeamMemberInviteSerializer,
        responses={201: StoreTeamMemberSerializer},
        description="Invite/add a user as a store team member with a designated role.",
    )
    @action(detail=True, methods=['get', 'post'], url_path='team')
    @transaction.atomic
    def team(self, request: Request, pk: str | None = None) -> Response:
        store = self.get_object()
        if request.method == 'POST':
            store = Store.objects.select_for_update().get(pk=store.pk)
        user_role = get_store_user_role(store, request.user)
        if not can_manage_team(store, request.user):
            raise PermissionDenied("Only store owners and store administrators can view or manage team members.")

        if request.method == 'GET':
            members = StoreTeamMember.objects.filter(store=store).select_related('user', 'invited_by').order_by('-created', 'pk')
            members = filter_records(
                members,
                request.query_params,
                search_fields=("invited_email", "user__email", "user__first_name", "user__last_name"),
            )
            criteria = StoreTeamQuerySerializer(data=request.query_params)
            criteria.is_valid(raise_exception=True)
            role = criteria.validated_data.get("role")
            if role:
                members = members.filter(role=role)
            count, page = record_page(members, request.query_params)
            data = StoreTeamMemberSerializer(
                page, many=True, context={"request": request}
            ).data
            if request.query_params.get("paginated") == "1":
                return Response({"count": count, "results": data})
            return Response(data)

        # POST: Invite team member (does not require target user to be registered first)
        serializer = StoreTeamMemberInviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email'].strip().lower()
        role = serializer.validated_data['role']

        from django.contrib.auth import get_user_model
        User = get_user_model()
        target_user = User.objects.filter(email__iexact=email).first()

        # Check if email belongs to the store owner
        if target_user and target_user.pk == store.user_id:
            raise ValidationError({'email': 'The store owner is already the primary administrator of this store.'})
        if store.user and store.user.email and store.user.email.lower() == email:
            raise ValidationError({'email': 'The store owner is already the primary administrator of this store.'})

        # Check existing active or pending team membership
        existing = StoreTeamMember.objects.filter(store=store).filter(
            (Q(user=target_user) | Q(invited_email__iexact=email))
            if target_user else Q(invited_email__iexact=email)
        ).first()

        if existing:
            if existing.status == StoreTeamMember.STATUS_PENDING:
                raise ValidationError({'email': 'An invitation is already pending for this email address.'})
            raise ValidationError({'email': 'This user is already an active team member of this store.'})

        if user_role == StoreTeamMember.ROLE_STORE_ADMIN and role == StoreTeamMember.ROLE_STORE_ADMIN:
            raise PermissionDenied("Only the store owner can invite or appoint another Store Admin.")

        import secrets
        invite_token = secrets.token_urlsafe(32)
        is_new_user = target_user is None
        status_val = StoreTeamMember.STATUS_ACTIVE if target_user else StoreTeamMember.STATUS_PENDING

        member = StoreTeamMember.objects.create(
            store=store,
            user=target_user,
            invited_email=email,
            role=role,
            status=status_val,
            invite_token=invite_token,
            invitation_sent_at=timezone.now(),
            invited_by=request.user,
        )

        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
        query = urlencode({'email': email, 'invite': invite_token})
        invite_url = f"{frontend_url}/register?{query}" if is_new_user else f"{frontend_url}/merchant"
        inviter_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.email.split('@')[0]
        try:
            from utils.functions import send_mail
            send_mail(
                'store-team-invitation',
                email,
                {
                    'subject': f"You're invited to join {store.name} on ProAce",
                    'store_name': store.name,
                    'inviter_name': inviter_name,
                    'role_display': member.get_role_display(),
                    'is_new_user': is_new_user,
                    'invite_url': invite_url,
                },
                recipient=target_user,
            )
        except Exception as exc:
            logger.warning("Failed to dispatch team invitation email to %s: %s", email, exc)
            raise InvitationDeliveryUnavailable() from None

        return Response(StoreTeamMemberSerializer(member, context={'request': request}).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        methods=['POST'],
        responses={200: StoreTeamMemberSerializer},
        description="Resend team invitation email to a pending invitee.",
    )
    @action(detail=True, methods=['post'], url_path=r'team/(?P<member_pk>\d+)/resend')
    @transaction.atomic
    def resend_team_invitation(
        self, request: Request, pk: str | None = None, member_pk: str | None = None
    ) -> Response:
        store = self.get_object()
        if not can_manage_team(store, request.user):
            raise PermissionDenied("Only store owners and store administrators can manage team members.")

        member = get_object_or_404(StoreTeamMember.objects.select_related('user'), pk=member_pk, store=store)
        if member.status != StoreTeamMember.STATUS_PENDING:
            raise ValidationError({'detail': 'Only pending invitations can be resent.'})

        member.invitation_sent_at = timezone.now()
        member.save(update_fields=['invitation_sent_at', 'updated'])
        email = member.email
        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
        is_new_user = member.user is None
        query = urlencode({'email': email, 'invite': member.invite_token})
        invite_url = f"{frontend_url}/register?{query}" if is_new_user else f"{frontend_url}/merchant"
        inviter_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.email.split('@')[0]
        try:
            from utils.functions import send_mail
            send_mail(
                'store-team-invitation',
                email,
                {
                    'subject': f"Reminder: You're invited to join {store.name} on ProAce",
                    'store_name': store.name,
                    'inviter_name': inviter_name,
                    'role_display': member.get_role_display(),
                    'is_new_user': is_new_user,
                    'invite_url': invite_url,
                },
                recipient=member.user,
                delivery_key=f"store-team-resend:{member.pk}:{uuid4().hex}",
            )
        except Exception as exc:
            logger.warning("Failed to resend team invitation email to %s: %s", email, exc)
            raise InvitationDeliveryUnavailable() from None
        return Response({
            'detail': 'Invitation email resent successfully.',
            'member': StoreTeamMemberSerializer(member, context={'request': request}).data,
        })

    @extend_schema(
        methods=['PATCH'],
        request=StoreTeamMemberUpdateSerializer,
        responses={200: StoreTeamMemberSerializer},
        description="Update a team member's role.",
    )
    @extend_schema(
        methods=['DELETE'],
        responses={204: None},
        description="Remove a team member or revoke an invitation.",
    )
    @action(detail=True, methods=['patch', 'delete'], url_path=r'team/(?P<member_pk>\d+)')
    @transaction.atomic
    def team_member_detail(
        self, request: Request, pk: str | None = None, member_pk: str | None = None
    ) -> Response:
        store = self.get_object()
        user_role = get_store_user_role(store, request.user)
        if not can_manage_team(store, request.user):
            raise PermissionDenied("Only store owners and store administrators can manage team members.")

        member = get_object_or_404(StoreTeamMember.objects.select_related('user'), pk=member_pk, store=store)

        if member.user_id and member.user_id == request.user.pk:
            raise PermissionDenied("You cannot modify or remove your own team membership.")

        if user_role == StoreTeamMember.ROLE_STORE_ADMIN and member.role == StoreTeamMember.ROLE_STORE_ADMIN:
            raise PermissionDenied("Only the store owner can modify or remove another Store Admin.")

        if request.method == 'DELETE':
            member.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        # PATCH: Update role
        serializer = StoreTeamMemberUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_role = serializer.validated_data['role']

        if user_role == StoreTeamMember.ROLE_STORE_ADMIN and new_role == StoreTeamMember.ROLE_STORE_ADMIN:
            raise PermissionDenied("Only the store owner can promote a member to Store Admin.")

        member.role = new_role
        member.save(update_fields=['role', 'updated'])
        return Response(StoreTeamMemberSerializer(member, context={'request': request}).data)


    @action(detail=True, methods=['get'])
    def onboarding(self, request, pk=None):
        store = self.get_object()
        from store.services import has_store_address
        checks = {
            'store_created': True,
            'contact_added': StoreInfo.objects.filter(store=store).exclude(email__isnull=True).exclude(email='').exists(),
            'pickup_address_added': has_store_address(store),
            'product_added': Product.objects.filter(store=store).exists(),
            'product_published': Product.objects.filter(store=store, visibility=True).exists(),
        }
        return Response({'steps': checks, 'complete': all(checks.values())})

    @staticmethod
    def fulfillment_value(order: Order, store: Store, field: str, fallback: object) -> object:
        fulfillments = getattr(order, 'loaded_fulfillments', None)
        if fulfillments is None:
            fulfillments = list(order.fulfillments.all())
            order.loaded_fulfillments = fulfillments
        fulfillment = next((row for row in fulfillments if row.store_id == store.pk), None)
        if field == 'status' and order.status in {'cancelled', 'refunded'}:
            return order.status
        if fulfillment:
            return getattr(fulfillment, field)
        if field == 'status':
            return fallback
        store_ids = StoreViewSet.order_store_ids(order)
        if store_ids == {str(store.pk)}:
            return fallback
        return None if field in {'shipped_at', 'delivered_at'} else ''

    def store_tracking_url(self, order: Order, store: Store) -> str:
        number = self.fulfillment_value(order, store, 'tracking_number', order.tracking_number)
        if not number:
            return ''
        carrier = self.fulfillment_value(order, store, 'carrier', order.carrier)
        return Order(carrier=str(carrier or ''), tracking_number=str(number)).tracking_url

    def store_orders(self, store: Store) -> QuerySet[Order]:
        from payment.order_filters import orders_for_store

        return orders_for_store(Order.objects.all(), store.pk).order_by('-created', '-pk')

    def store_refunds(self, store: Store) -> QuerySet[Refund]:
        from django.db import connections
        from django.db.models.expressions import RawSQL

        database = connections[Refund.objects.db]
        if database.vendor == "sqlite":
            table = database.ops.quote_name(Refund._meta.db_table)
            snapshot_q = Q(
                pk__in=RawSQL(
                    f"SELECT r.id FROM {table} AS r "
                    "WHERE EXISTS (SELECT 1 FROM json_each(r.selected_items) AS item "
                    "WHERE CAST(json_extract(item.value, '$.store') AS TEXT) = %s)",
                    [str(store.pk)],
                )
            )
        else:
            snapshot_q = Q(selected_items__contains=[{"store": store.pk}]) | Q(
                selected_items__contains=[{"store": str(store.pk)}]
            )

        legacy_orders = self.store_orders(store)
        legacy_q = (Q(selected_items=[]) | Q(selected_items__isnull=True)) & Q(order__in=legacy_orders)
        return Refund.objects.filter(snapshot_q | legacy_q).distinct()


    @staticmethod
    def legacy_order_items(order: Order) -> list[CartItem]:
        if not order.cart_id:
            return []
        items = getattr(order.cart, 'loaded_order_items', None)
        if items is None:
            items = list(order.cart.cart_items.select_related('product'))
            order.cart.loaded_order_items = items
        return items

    @staticmethod
    def order_store_ids(order: Order) -> set[str]:
        if order.items_snapshot:
            return {str(row['store']) for row in order.items_snapshot if row.get('store')}
        return {
            str(item.product.store_id)
            for item in StoreViewSet.legacy_order_items(order)
        }

    def store_order_items(self, order: Order, store: Store) -> list[dict[str, Any]]:
        if order.items_snapshot:
            return [
                row for row in order.items_snapshot
                if str(row.get('store')) == str(store.pk)
            ]
        return [
            {
                'product': item.product_id,
                'title': item.product.title,
                'quantity': item.quantity,
                'is_digital': item.product.is_digital,
            }
            for item in self.legacy_order_items(order)
            if item.product.store_id == store.pk
        ]

    def store_refund_items(
        self, refund: Refund, store: Store, product_ids: set[int],
    ) -> list[dict[str, Any]]:
        items = [
            item for item in (refund.selected_items or [])
            if (
                str(item.get("store")) == str(store.pk)
                or (not item.get("store") and item.get("product") in product_ids)
            )
        ]
        if not items and refund.order:
            items = self.store_order_items(refund.order, store)
        return items

    def store_tracking_events(self, order: Order, store: Store) -> list[dict[str, Any]]:
        store_ids = self.order_store_ids(order)
        return [
            event for event in (order.tracking_events or [])
            if str(event.get('store_id')) == str(store.pk)
            or (not event.get('store_id') and store_ids == {str(store.pk)})
        ]

    @extend_schema(parameters=[
        OpenApiParameter(
            name="order_id",
            type=int,
            location=OpenApiParameter.QUERY,
            description="Return the requested order when it contains items from this store.",
        ),
    ])
    @action(detail=True, methods=['get'])
    def orders(self, request: Request, pk: str | None = None) -> Response:
        store = self.get_object()
        if not can_manage_orders(store, request.user):
            raise PermissionDenied("You do not have permission to view orders for this store.")
        orders = self.store_orders(store)
        order_id: str = str(request.query_params.get("order_id") or "").strip()
        if order_id:
            try:
                requested_order_id: int = int(order_id)
            except (TypeError, ValueError) as error:
                raise ValidationError({"order_id": "Enter a valid order ID."}) from error
            if requested_order_id < 1:
                raise ValidationError({"order_id": "Enter a valid order ID."})
            orders = orders.filter(pk=requested_order_id)

        order_status = request.query_params.get('status')
        if order_status:
            if order_status not in dict(Order.ORDER_STATUS_CHOICE):
                raise ValidationError({'status': 'Invalid order status.'})
            if order_status in {'cancelled', 'refunded'}:
                orders = orders.filter(status=order_status)
            else:
                orders = orders.filter(
                    Q(fulfillments__store=store, fulfillments__status=order_status)
                    | Q(fulfillments__isnull=True, status=order_status)
                ).exclude(status__in=['cancelled', 'refunded'])

        search = str(request.query_params.get('search') or '').strip()
        if search:
            orders = orders.filter(
                Q(orderId__icontains=search)
                | Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search)
                | Q(user__email__icontains=search)
                | Q(buyer_email__icontains=search)
                | Q(buyer_phone__icontains=search)
                | Q(tracking_number__icontains=search)
                | Q(fulfillments__tracking_number__icontains=search)
                | Q(cart__cart_items__product__title__icontains=search)
            )

        carrier = str(request.query_params.get('carrier') or '').strip()
        if carrier:
            orders = orders.filter(
                Q(fulfillments__store=store, fulfillments__carrier__iexact=carrier)
                | Q(fulfillments__isnull=True, carrier__iexact=carrier)
            )

        delivery_method = str(request.query_params.get('delivery_method') or '').strip()
        if delivery_method:
            orders = orders.filter(delivery__method__iexact=delivery_method)

        days = request.query_params.get('days')
        if days:
            num_days = serializers.IntegerField(min_value=1, max_value=365000).run_validation(days)
            orders = orders.filter(created__gte=timezone.now() - timedelta(days=num_days))

        date_from = str(request.query_params.get('date_from') or '').strip()
        if date_from:
            orders = orders.filter(created__gte=merchant_date_boundary(date_from, 'date_from'))

        date_to = str(request.query_params.get('date_to') or '').strip()
        if date_to:
            orders = orders.filter(created__lte=merchant_date_boundary(date_to, 'date_to', end=True))

        ordering = str(request.query_params.get('ordering') or '-created').strip()
        allowed_orderings = {
            '-created': ('-created', '-pk'),
            'created': ('created', 'pk'),
            '-updated': ('-updated', '-pk'),
            'updated': ('updated', 'pk'),
        }
        order_by_fields = allowed_orderings.get(ordering, ('-created', '-pk'))
        orders = orders.distinct().order_by(*order_by_fields)

        orders = orders.select_related('user', 'delivery', 'cart').prefetch_related(
            Prefetch('fulfillments', to_attr='loaded_fulfillments'),
        )
        page = self.paginate_queryset(orders)
        prefetch_related_objects(
            [order for order in page if not order.items_snapshot],
            Prefetch(
                'cart__cart_items', queryset=CartItem.objects.select_related('product'),
                to_attr='loaded_order_items',
            ),
        )
        items_by_order = {order.pk: self.store_order_items(order, store) for order in page}
        missing_image_ids = {
            int(str(item["product"]))
            for items in items_by_order.values()
            for item in items
            if not item.get("image") and str(item.get("product", "")).isascii()
            and str(item.get("product", "")).isdigit()
        }
        catalog_images = {
            str(product_id): image
            for product_id, image in Product.objects.filter(
                store=store, pk__in=missing_image_ids,
            ).values_list("pk", "image_url")
        }
        data = [
            {
                "id": order.pk,
                "reference": order.orderId,
                "status": self.fulfillment_value(order, store, "status", order.status),
                "order_status": order.status,
                "allowed_status_transitions": available_fulfillment_statuses(
                    order.status,
                    str(self.fulfillment_value(order, store, "status", order.status)),
                    any(not row.get("is_digital") for row in items_by_order[order.pk]),
                ),
                "carrier": self.fulfillment_value(
                    order, store, "carrier", order.carrier
                )
                or "USPS",
                "tracking_number": self.fulfillment_value(
                    order, store, "tracking_number", order.tracking_number
                )
                or "",
                "tracking_url": self.store_tracking_url(order, store),
                "shipped_at": self.fulfillment_value(
                    order, store, "shipped_at", order.shipped_at
                ),
                "delivered_at": self.fulfillment_value(
                    order, store, "delivered_at", order.delivered_at
                ),
                "tracking_events": self.store_tracking_events(order, store),
                "shipping_address": (order.address_snapshot or {}) if any(
                    not row.get('is_digital') for row in items_by_order[order.pk]
                ) else {},
                "customer": {
                    "name": f"{order.user.first_name} {order.user.last_name}".strip()
                    if order.user
                    else "",
                    "email": (order.user.email if order.user else "")
                    or getattr(order, "buyer_email", "")
                    or "",
                    "phone": str(
                        getattr(order.user, "phone1", "")
                        or getattr(order, "buyer_phone", "")
                        or ""
                    )
                    if order.user
                    else str(getattr(order, "buyer_phone", "") or ""),
                },
                "created": order.created,
                "ordered": order.ordered,
                "has_physical_items": any(
                    not row.get('is_digital') for row in items_by_order[order.pk]
                ),
                "items": [
                    {
                        "product": it.get("product", 0),
                        "title": it.get("title", "Item"),
                        "image": it.get("image") or catalog_images.get(str(it.get("product")), ""),
                        "quantity": it.get("quantity", 1),
                        "is_digital": bool(it.get("is_digital")),
                    }
                    for it in items_by_order[order.pk]
                ],
            }
            for order in page
        ]
        return self.get_paginated_response(data)

    @extend_schema(
        parameters=[MerchantRefundQuerySerializer],
        responses=MerchantRefundSerializer(many=True),
        description="List and filter product refunds for this store.",
    )
    @action(detail=True, methods=['get'])
    def refunds(self, request: Request, pk: str | None = None) -> Response:
        store = self.get_object()
        if not can_manage_orders(store, request.user):
            raise PermissionDenied("You do not have permission to manage refunds for this store.")
        refunds = self.store_refunds(store)

        order_id = str(request.query_params.get("order_id") or "").strip()
        if order_id:
            clean_ord = order_id.lstrip("#")
            if clean_ord.isdigit():
                refunds = refunds.filter(Q(order_id=int(clean_ord)) | Q(order__orderId=order_id))
            else:
                refunds = refunds.filter(order__orderId=order_id)

        refund_id = str(request.query_params.get("refund_id") or "").strip()
        if refund_id:
            clean_ref = refund_id.lstrip("#")
            if clean_ref.isdigit():
                refunds = refunds.filter(pk=int(clean_ref))

        status_param = request.query_params.get("status")
        if status_param:
            if status_param not in dict(Refund.STATUS_CHOICES):
                raise ValidationError({"status": "Invalid refund status."})
            refunds = refunds.filter(status=status_param)

        refund_type = request.query_params.get("refund_type")
        if refund_type:
            if refund_type not in dict(Refund.REFUND_TYPE_CHOICES):
                raise ValidationError({"refund_type": "Invalid refund type."})
            refunds = refunds.filter(refund_type=refund_type)

        store_prod_ids = set(Product.objects.filter(store=store).values_list("pk", flat=True))
        search = str(request.query_params.get("search") or "").strip()
        if search:
            # Match exactly the store-scoped items exposed in the response. Saved
            # refund/order titles take precedence over mutable catalog/cart data.
            candidates = refunds.select_related("order", "order__cart").prefetch_related(
                Prefetch(
                    "order__cart__cart_items",
                    queryset=CartItem.objects.select_related("product"),
                    to_attr="loaded_order_items",
                ),
            )
            title_matches = [
                refund.pk for refund in candidates.iterator(chunk_size=500)
                if any(
                    search.casefold() in str(item.get("title", "")).casefold()
                    for item in self.store_refund_items(refund, store, store_prod_ids)
                )
            ]
            clean_search = search.lstrip("#")
            id_q = Q(pk=int(clean_search)) if clean_search.isdigit() else Q()
            refunds = refunds.filter(
                id_q
                | Q(email__icontains=search)
                | Q(reason__icontains=search)
                | Q(processing_reason__icontains=search)
                | Q(order__orderId__icontains=search)
                | Q(order__user__email__icontains=search)
                | Q(order__user__first_name__icontains=search)
                | Q(order__user__last_name__icontains=search)
                | Q(pk__in=title_matches)
            )

        days = request.query_params.get("days")
        if days:
            num_days = serializers.IntegerField(min_value=1, max_value=365000).run_validation(days)
            refunds = refunds.filter(created__gte=timezone.now() - timedelta(days=num_days))

        date_from = str(request.query_params.get("date_from") or request.query_params.get("start_date") or "").strip()
        if date_from:
            refunds = refunds.filter(created__gte=merchant_date_boundary(date_from, 'date_from'))

        date_to = str(request.query_params.get("date_to") or request.query_params.get("end_date") or "").strip()
        if date_to:
            refunds = refunds.filter(created__lte=merchant_date_boundary(date_to, 'date_to', end=True))

        ordering = str(request.query_params.get("ordering") or "-created").strip()
        allowed_orderings = {
            "-created": ("-created", "-pk"),
            "created": ("created", "pk"),
            "-updated": ("-updated", "-pk"),
            "updated": ("updated", "pk"),
            "-amount": ("-refund_amount", "-pk"),
            "amount": ("refund_amount", "pk"),
        }
        order_by_fields = allowed_orderings.get(ordering, ("-created", "-pk"))
        refunds = refunds.distinct().order_by(*order_by_fields)

        refunds = refunds.select_related("order", "order__user", "order__delivery", "order__cart", "processed_by")
        page = self.paginate_queryset(refunds)
        prefetch_related_objects(
            [
                refund.order.cart
                for refund in page
                if refund.order and refund.order.cart_id and not refund.order.items_snapshot
            ],
            Prefetch(
                "cart_items",
                queryset=CartItem.objects.select_related("product"),
                to_attr="loaded_order_items",
            ),
        )

        data = []
        for refund in page:
            store_items = self.store_refund_items(refund, store, store_prod_ids)

            amount = sum(
                (
                    Decimal(str(item.get("line_total") or (Decimal(str(item.get("unit_price", "0"))) * int(item.get("quantity", 1)))))
                    for item in store_items
                ),
                Decimal("0.00"),
            ).quantize(Decimal(".01"))

            order = refund.order
            customer_name = ""
            if order and order.user:
                customer_name = order.user.get_full_name() or ""
            elif order and order.address_snapshot:
                customer_name = f"{order.address_snapshot.get('first_name', '')} {order.address_snapshot.get('last_name', '')}".strip()

            data.append(
                {
                    "id": refund.id,
                    "order": order.id if order else None,
                    "order_reference": order.orderId if order else "",
                    "customer_email": refund.email,
                    "customer_name": customer_name,
                    "status": refund.status,
                    "refund_type": refund.refund_type,
                    "reason": refund.reason,
                    "processing_reason": refund.processing_reason or "",
                    "amount": str(amount),
                    "total_order_refund_amount": str(amount),
                    "items": [
                        {
                            "product": item.get("product"),
                            "title": item.get("title", ""),
                            "image": item.get("image", ""),
                            "quantity": item.get("quantity", 1),
                            "unit_price": str(item.get("unit_price", "0.00")),
                            "line_total": str(
                                item.get("line_total")
                                or (
                                    Decimal(str(item.get("unit_price", "0.00")))
                                    * int(item.get("quantity", 1))
                                )
                            ),
                            "is_digital": bool(item.get("is_digital", False)),
                            "variant": item.get("variant"),
                        }
                        for item in store_items
                    ],
                    "processed_at": refund.processed_at.isoformat()
                    if refund.processed_at
                    else None,
                    "created": refund.created.isoformat() if refund.created else None,
                    "updated": refund.updated.isoformat() if refund.updated else None,
                }
            )
        return self.get_paginated_response(data)

    @action(
        detail=True,
        methods=["patch", "post"],
        url_path=r"orders/(?P<order_pk>[^/.]+)/tracking",
    )
    @extend_schema(request=MerchantTrackingSerializer, responses=dict)
    @transaction.atomic
    def update_order_tracking(
        self, request: Request, pk: str | None = None, order_pk: str | None = None
    ) -> Response:
        store = self.get_object()
        if not can_manage_orders(store, request.user):
            raise PermissionDenied("You do not have permission to fulfill orders or update tracking for this store.")
        order_filter = (
            Q(pk=int(order_pk)) if str(order_pk).isdigit() else Q(orderId=order_pk)
        )
        order = Order.objects.filter(order_filter).select_for_update().first()
        if not order:
            raise Http404("Order not found.")
        from payment.fulfillment import update_fulfillment

        belongs_to_store = self.store_orders(store).filter(pk=order.pk).exists()
        if not belongs_to_store:
            raise Http404("Order does not belong to this store.")
        serializer = MerchantTrackingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        from payment.fulfillment import ensure_fulfillments
        ensure_fulfillments(order)
        current = order.fulfillments.filter(store=store).first()
        if current is None:
            raise ValidationError({'order': 'This store has no physical items to fulfill.'})
        previous = (current.status, current.carrier, current.tracking_number)
        fulfillment = update_fulfillment(
            order,
            store,
            data.get("status"),
            data["carrier"],
            data["tracking_number"],
        )
        last_event = (order.tracking_events or [{}])[-1]
        if previous == (fulfillment.status, fulfillment.carrier, fulfillment.tracking_number) and (
            not data["event_description"] or last_event.get("description") == data["event_description"]
        ):
            return Response({"id": order.pk, "reference": order.orderId, "status": order.status})
        event_description = data["event_description"]
        if event_description or data["tracking_number"] or data.get("status"):
            events = list(order.tracking_events or [])
            events.append(
                {
                    "status": fulfillment.status,
                    "store_id": store.pk,
                    "description": event_description
                    or f"Store shipment marked {fulfillment.status}.",
                    "timestamp": timezone.now().isoformat(),
                }
            )
            order.tracking_events = events

        order.save(
            update_fields=[
                "status",
                "carrier",
                "tracking_number",
                "shipped_at",
                "delivered_at",
                "tracking_events",
                "updated",
            ]
        )

        from notification.dispatcher import get_notification_dispatcher

        get_notification_dispatcher().notify(
            recipient=order.user,
            actor=store.user,
            target=order,
            verb=f"Order #{order.orderId} — {store.name} {fulfillment.status.title()}",
            description=(
                f"Your {store.name} shipment is {fulfillment.status}. Carrier: {fulfillment.carrier}, Tracking #{fulfillment.tracking_number}"
                if fulfillment.carrier and fulfillment.tracking_number
                else f"Your {store.name} shipment is {fulfillment.status}. Tracking #{fulfillment.tracking_number}"
                if fulfillment.tracking_number
                else f"Your {store.name} shipment is {fulfillment.status}."
            ),
            data={
                "event": "order_tracking_update",
                "email": fulfillment.status not in {"shipped", "delivered", "picked up", "confirmed"},
                "order_id": order.pk,
                "store_id": store.pk,
                "tracking_number": fulfillment.tracking_number,
                "status": fulfillment.status,
            },
        )

        recipient_email = str(order.buyer_email or (order.user.email if order.user else "") or "").strip()
        if recipient_email and fulfillment.status == "shipped":
            from utils.functions import send_mail
            send_mail(
                "order-shipped",
                recipient_email,
                {
                    "firstname": (order.user.first_name if order.user else ""),
                    "orderId": order.orderId,
                    "carrier": fulfillment.carrier,
                    "tracking_number": fulfillment.tracking_number or "",
                    "tracking_url": self.store_tracking_url(order, store),
                    "store_name": store.name,
                    "subject": f"Order #{order.orderId} Has Shipped",
                },
            )
        elif recipient_email and fulfillment.status == "delivered":
            from utils.functions import send_mail
            send_mail(
                "delivery-confirmation",
                recipient_email,
                {
                    "firstname": (order.user.first_name if order.user else ""),
                    "orderId": order.orderId,
                    "carrier": fulfillment.carrier,
                    "tracking_number": fulfillment.tracking_number,
                    "store_name": store.name,
                    "subject": f"Order #{order.orderId} Delivered",
                },
            )
        elif recipient_email and fulfillment.status == "picked up":
            from utils.functions import send_mail
            send_mail(
                "order-picked-up",
                recipient_email,
                {
                    "firstname": (order.user.first_name if order.user else ""),
                    "orderId": order.orderId,
                    "store_name": store.name,
                    "subject": f"Order #{order.orderId} Picked Up",
                },
            )
        elif recipient_email and fulfillment.status == "confirmed":
            from utils.functions import send_mail
            send_mail(
                "order-confirm",
                recipient_email,
                {
                    "firstname": (order.user.first_name if order.user else ""),
                    "orderId": order.orderId,
                    "store_name": store.name,
                    "subject": f"Order #{order.orderId} Confirmed",
                },
            )

        return Response(
            {
                "id": order.pk,
                "reference": order.orderId,
                "order_status": order.status,
                "status": fulfillment.status,
                "carrier": fulfillment.carrier,
                "tracking_number": fulfillment.tracking_number,
                "tracking_url": self.store_tracking_url(order, store),
                "shipped_at": fulfillment.shipped_at.isoformat()
                if fulfillment.shipped_at
                else None,
                "delivered_at": fulfillment.delivered_at.isoformat()
                if fulfillment.delivered_at
                else None,
                "tracking_events": self.store_tracking_events(order, store),
            }
        )

    @action(detail=True, methods=["get"])
    def dashboard(self, request, pk=None):
        store = self.get_object()
        query = DashboardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        with timezone.override(ZoneInfo(query.validated_data["time_zone"])):
            days = query.validated_data["days"]
            threshold = query.validated_data["low_stock_threshold"]
            today = timezone.localdate()
            start = None if days == 'all' else today - timedelta(days=days - 1)
            products = Product.objects.filter(store=store)
            orders = self.store_orders(store).filter(created__lt=timezone.make_aware(datetime.combine(today + timedelta(days=1), datetime.min.time())))
            if start:
                orders = orders.filter(created__gte=timezone.make_aware(datetime.combine(start, datetime.min.time())))
            sales_orders = orders.filter(
                ordered=True,
                status__in=["confirmed", "shipped", "delivered", "picked up"],
            )
            items = CartItem.objects.filter(
                product__store=store, cart__in=sales_orders.values("cart_id")
            )
            line_value = ExpressionWrapper(
                F("quantity")
                * F("product__price")
                * (100 - F("product__discount"))
                / Decimal("100.0"),
                output_field=DecimalField(max_digits=24, decimal_places=2),
            )
            totals = items.aggregate(units=Sum("quantity"), value=Sum(line_value))
            financials = calculate_store_financials(store) if can_manage_financials(store, request.user) else {}
            if financials and not store.is_official:
                merchant_financials = calculate_merchant_financials(store.user)
                financials['withdrawable_balance'] = format(max(
                    Decimal('0.00'), min(
                        Decimal(financials['available_balance']),
                        Decimal(merchant_financials['available_balance']),
                    ),
                ), '.2f')

            ledger = StoreEarningsLedger.objects.filter(
                store=store,
                entry_type__in=[StoreEarningsLedger.TYPE_SALE, StoreEarningsLedger.TYPE_REFUND],
            ).filter(Q(order__isnull=True) | Q(order__paid_total__gt=0))
            if start:
                period_ledger = ledger.filter(
                    created__gte=timezone.make_aware(datetime.combine(start, datetime.min.time())),
                    created__lt=timezone.make_aware(datetime.combine(today + timedelta(days=1), datetime.min.time())),
                )
            else:
                period_ledger = ledger

            fee_percent = store.platform_fee_percent

            if period_ledger.exists():
                period_gross = period_ledger.aggregate(total=Sum("gross_amount"))["total"] or Decimal("0")
                period_fee = period_ledger.aggregate(total=Sum("fee_amount"))["total"] or Decimal("0")
            else:
                period_gross = totals["value"] or Decimal("0")
                period_fee = (period_gross * fee_percent / Decimal("100.0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            period_net = period_gross - period_fee

            if financials:
                financials["period_gross_sales"] = format(period_gross, ".2f")
                financials["period_platform_fee_deducted"] = format(period_fee, ".2f")
                financials["period_net_sales"] = format(period_net, ".2f")

            user_can_manage_products = can_manage_products(store, request.user)
            if user_can_manage_products:
                inventory = products.aggregate(
                    total=Count("id"),
                    published=Count("id", filter=Q(visibility=True)),
                    drafts=Count("id", filter=Q(visibility=False)),
                    out_of_stock=Count("id", filter=Q(is_digital=False, available=0)),
                    low_stock=Count(
                        "id", filter=Q(is_digital=False, available__gt=0, available__lte=threshold)
                    ),
                    units=Sum("available"),
                )
                inventory['units'] = inventory['units'] or 0
                low_stock_products = list(
                    products.filter(is_digital=False, available__lte=threshold)
                    .order_by("available", "pk")
                    .values("id", "title", "available")[:20]
                )
            else:
                inventory = {
                    "total": 0, "published": 0, "drafts": 0, "units": 0, "low_stock": 0, "out_of_stock": 0
                }
                low_stock_products = []
            bucket = TruncMonth('created') if days == 'all' else TruncDate('created')
            trend_rows = orders.order_by().annotate(day=bucket).values('day').annotate(count=Count('pk', distinct=True)).order_by('day')
            trend = {(row['day'].date() if isinstance(row['day'], datetime) else row['day']): row['count'] for row in trend_rows}
            if days == 'all':
                order_activity = [{'date': day, 'count': count} for day, count in trend.items()]
            else:
                order_activity = [{'date': start + timedelta(days=i), 'count': trend.get(start + timedelta(days=i), 0)} for i in range(days)]
            top_products = items.values('product_id', 'product__title').annotate(
                units=Sum('quantity')).order_by('-units', 'product_id')[:10]
            top_products_list = list(top_products) if user_can_manage_products else []
            return Response(
                {
                    "period": {
                        "from": start,
                        "to": today,
                        "days": days,
                        "granularity": 'month' if days == 'all' else 'day',
                        "time_zone": query.validated_data["time_zone"],
                    },
                    "inventory": inventory,
                    "orders": {
                        "total": orders.count(),
                        "by_status": list(
                            orders.order_by()
                            .values("status")
                            .annotate(count=Count("pk", distinct=True))
                        ),
                    },
                    "sales": {
                        "units": totals["units"] or 0,
                        "gross_sales": format(period_gross, ".2f"),
                        "platform_fee_percent": format(fee_percent, ".2f"),
                        "platform_fee_deducted": format(period_fee, ".2f"),
                        "net_sales": format(period_net, ".2f"),
                        "estimated_item_value": format(
                            totals["value"] or Decimal("0"), ".2f"
                        ),
                        "basis": "Current item prices for confirmed, shipped, delivered and picked-up orders. Excludes coupons, tax, shipping and fees; not payout revenue.",
                    },
                    "wallet": financials,
                    "orders_by_day": order_activity,
                    "top_products": top_products_list,
                    "low_stock_products": low_stock_products,
                }
            )

    @action(detail=False, methods=['get'], url_path='wallet')
    def merchant_wallet(self, request):
        """
        Consolidated financial wallet for the authenticated vendor across all their stores.
        """
        data = calculate_merchant_financials(request.user)
        return Response(MerchantWalletSerializer(data).data)

    @action(detail=False, methods=['get'], url_path='wallet/ledger')
    @extend_schema(parameters=[ListingQuerySerializer], responses=StoreEarningsLedgerSerializer(many=True))
    def merchant_ledger(self, request: Request) -> Response:
        """
        Granular ledger entries for all stores owned by the merchant.
        Supports query params: ?store_id=X, ?status=pending|available|completed|reversed, ?entry_type=sale|payout|fee|refund
        """
        user_stores = Store.objects.filter(user=request.user)
        entries = StoreEarningsLedger.objects.filter(store__in=user_stores).select_related('store', 'wallet')

        store_id = request.query_params.get('store_id')
        if store_id:
            entries = entries.filter(store_id=store_id)

        status_filter = request.query_params.get('status')
        if status_filter:
            entries = entries.filter(status=status_filter)

        entry_type_filter = request.query_params.get('entry_type')
        if entry_type_filter:
            entries = entries.filter(entry_type=entry_type_filter)

        entries = filter_records(
            entries.order_by("-created", "-pk"), request.query_params,
            search_fields=("reference", "description", "store__name"),
            fields={"status": "status", "entry_type": "entry_type"}, amount_field="net_amount",
        )
        page = self.paginate_queryset(entries)
        serializer = StoreEarningsLedgerSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    @action(detail=False, methods=['get', 'post'], url_path='wallet/payouts')
    @transaction.atomic
    @extend_schema(methods=['GET'], parameters=[ListingQuerySerializer], responses=StorePayoutSerializer(many=True))
    def merchant_payouts(self, request: Request) -> Response:
        """
        Unified payout requests and listing across all stores for the merchant.
        """
        user_stores = Store.objects.filter(user=request.user)
        wallet = get_or_create_merchant_wallet(request.user)

        if request.method == 'GET':
            payouts = StorePayout.objects.filter(
                Q(wallet=wallet) | Q(store__in=user_stores)
            ).select_related('store').distinct().order_by('-created', '-pk')
            payouts = filter_records(
                payouts, request.query_params,
                search_fields=('reference', 'notes', 'payout_method', 'store__name'),
                fields={'status': 'status', 'payout_method': 'payout_method'},
                amount_field='amount',
            )
            page = self.paginate_queryset(payouts)
            serializer = StorePayoutSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        if is_backoffice(request.user) and not is_admin(request.user):
            raise PermissionDenied('Only administrators or store owners can request payouts.')
        serializer = MerchantPayoutCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        amount = serializer.validated_data['amount']
        payout_method = serializer.validated_data['payout_method']
        account_details = serializer.validated_data.get('account_details', {})
        if serializer.validated_data.get('bank_account_id'):
            from core.bank_accounts import payout_account_details
            account_details = payout_account_details(request.user, serializer.validated_data['bank_account_id'])
        notes = serializer.validated_data.get('notes', '')

        store = None
        store_id = serializer.validated_data.get('store_id')
        if store_id:
            store = get_object_or_404(user_stores, pk=store_id)

        if store and store.is_official:
            raise ValidationError({
                'detail': 'Official company store earnings are consolidated into the Platform Treasury and cannot be withdrawn via merchant payouts. Withdrawals are managed by administrators at /admin/commission.'
            })

        # Synchronize merchant financials to ensure fresh balances
        calculate_merchant_financials(request.user)

        payout = request_merchant_payout(
            user=request.user,
            amount=amount,
            payout_method=payout_method,
            account_details=account_details,
            store=store,
            notes=notes,
        )

        from notification.dispatcher import get_notification_dispatcher
        from notification.views import notify_staff
        notify_staff(
            verb=f"New Merchant Payout Request: ${amount}",
            description=f"Merchant '{request.user.email}' requested a payout of ${amount} (Ref: {payout.reference}).",
            data={"payout_id": payout.pk, "amount": str(amount), "user_id": request.user.pk},
        )
        get_notification_dispatcher().notify(
            recipient=request.user,
            verb=f"Payout Request Submitted: ${amount}",
            description=f"Your payout request of ${amount} (Ref: {payout.reference}) has been submitted and is under review.",
            data={"payout_id": payout.pk, "amount": str(amount)},
        )

        return Response(StorePayoutSerializer(payout).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get', 'post'], url_path='payouts')
    @transaction.atomic
    @extend_schema(methods=["GET"], parameters=[ListingQuerySerializer], responses=StorePayoutSerializer(many=True))
    def payouts(self, request: Request, pk: str | None = None) -> Response:
        store = self.get_object()
        if request.method == 'GET':
            if not can_manage_financials(store, request.user):
                raise PermissionDenied("You do not have permission to view payouts for this store.")
            payouts = filter_records(
                store.payouts.order_by("-created", "-pk"), request.query_params,
                search_fields=("reference", "notes", "payout_method"),
                fields={"status": "status", "payout_method": "payout_method"}, amount_field="amount",
            )
            if request.query_params.get("paginated") == "1":
                count, page = record_page(payouts, request.query_params)
                serializer = StorePayoutSerializer(page, many=True)
                return Response({"count": count, "results": serializer.data})
            return Response(StorePayoutSerializer(payouts, many=True).data)

        if store.is_official:
            raise ValidationError({
                'detail': 'Official company store earnings are consolidated into the Platform Treasury and cannot be withdrawn via merchant payouts. Withdrawals are managed by administrators at /admin/commission.'
            })

        if store.user_id != request.user.pk:
            raise PermissionDenied('Only the store owner can initiate a withdrawal.')
        serializer = PayoutCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        amount = serializer.validated_data['amount']
        payout_method = serializer.validated_data['payout_method']
        account_details = serializer.validated_data.get('account_details', {})
        if serializer.validated_data.get('bank_account_id'):
            from core.bank_accounts import payout_account_details
            account_details = payout_account_details(request.user, serializer.validated_data['bank_account_id'])
        notes = serializer.validated_data.get('notes', '')

        # Validate store-specific balance
        store_fin = calculate_store_financials(store)
        store_available = Decimal(store_fin['available_balance'])
        if amount > store_available:
            raise ValidationError({
                'detail': f'Requested payout amount (${amount}) exceeds available cleared balance (${store_available}).'
            })

        payout = request_merchant_payout(
            user=request.user,
            amount=amount,
            payout_method=payout_method,
            account_details=account_details,
            store=store,
            notes=notes,
        )

        from notification.views import create_notification, notify_staff
        notify_staff(
            verb=f"New Merchant Payout Request: ${amount}",
            description=f"Store '{store.name}' requested a payout of ${amount} (Ref: {payout.reference}).",
            data={"payout_id": payout.pk, "store_id": store.pk, "amount": str(amount)},
        )
        from notification.dispatcher import get_notification_dispatcher

        get_notification_dispatcher().notify(
            recipient=store.user,
            verb=f"Payout Request Submitted: ${amount}",
            description=f"Your payout request of ${amount} (Ref: {payout.reference}) has been submitted and is under review.",
            data={"payout_id": payout.pk, "amount": str(amount)},
        )

        return Response(StorePayoutSerializer(payout).data, status=status.HTTP_201_CREATED)



@extend_schema_view(
    list=extend_schema(operation_id='merchant_products_list'),
    retrieve=extend_schema(operation_id='merchant_product_retrieve'),
    update=extend_schema(request={
        'application/json': MerchantProductSerializer,
        'multipart/form-data': MerchantProductUploadSerializer,
    }),
    partial_update=extend_schema(request={
        'application/json': MerchantProductSerializer,
        'multipart/form-data': MerchantProductUploadSerializer,
    }),
)
class MerchantProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.none()
    permission_classes = [IsAuthenticated]
    serializer_class = MerchantProductSerializer
    pagination_class = MerchantPagination
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['title', 'description', 'tags__name']
    ordering_fields = ['created', 'updated', 'price', 'available', 'title', 'sales']
    ordering = ['-created', '-pk']

    def get_store(self) -> Store:
        store = get_object_or_404(
            StoreViewSet.get_queryset(self),
            pk=self.kwargs['store_pk'],
        )
        if not can_manage_products(store, self.request.user):
            raise PermissionDenied('You do not have permission to manage products for this store.')
        from core.activity import record_store_activity
        record_store_activity(store)
        return store

    def validate_store_publication(self, serializer: MerchantProductSerializer) -> None:
        from store.services import has_store_address
        visible = serializer.validated_data.get('visibility', getattr(serializer.instance, 'visibility', False))
        if visible and not has_store_address(self.get_store()):
            raise ValidationError({'visibility': 'Add a complete default store address before publishing physical or digital products.'})

    def get_queryset(self):
        products = Product.objects.filter(store=self.get_store()).prefetch_related('tags', 'images').select_related('specification')
        products = filter_records(
            products, self.request.query_params,
            fields={"is_digital": "is_digital", "product_type": "product_type", "is_blocked": "is_blocked"},
        )
        if self.action in {'update', 'partial_update'}:
            # Lock only the product: the optional specification uses an outer join.
            products = products.select_for_update(of=('self',))
        if self.action == 'list':
            visibility = self.request.query_params.get('visibility')
            if visibility is not None:
                if visibility not in {'true', 'false'}:
                    raise ValidationError({'visibility': 'Use true or false.'})
                products = products.filter(visibility=visibility == 'true')
            category = self.request.query_params.get('category')
            if category:
                products = products.filter(category=category)
            stock = self.request.query_params.get('stock')
            filters = {'out': Q(is_digital=False, available=0), 'low': Q(is_digital=False, available__gt=0, available__lte=5), 'in': Q(is_digital=True) | Q(available__gt=0)}
            if stock is not None:
                if stock not in filters:
                    raise ValidationError({'stock': 'Use in, low or out.'})
                products = products.filter(filters[stock])
        return products

    def get_serializer(self, *args, **kwargs):
        data = kwargs.get('data')
        if data is not None and self.request.content_type.startswith('multipart/form-data') and 'product' in data:
            upload = MerchantProductUploadSerializer(data=data)
            upload.is_valid(raise_exception=True)
            payload = dict(upload.validated_data['product'])
            for field in ('cover_image', 'digital_file', 'informational_video'):
                if field in upload.validated_data:
                    payload[field] = upload.validated_data[field]
            files = upload.validated_data.get('variant_files', [])
            indexes = upload.validated_data.get('variant_indexes', [])
            if not isinstance(indexes, list) or len(indexes) != len(files):
                raise ValidationError({'variant_indexes': 'Match each uploaded file to one product option.'})
            if files:
                payload['variant_uploads'] = [{'index': index, 'file': file} for index, file in zip(indexes, files)]
            kwargs['data'] = payload
        return super().get_serializer(*args, **kwargs)

    @extend_schema(request={
        'application/json': MerchantProductSerializer,
        'multipart/form-data': MerchantProductUploadSerializer,
    })
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        self.get_store()
        return super().create(request, *args, **kwargs)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @transaction.atomic
    def perform_create(self, serializer):
        store = self.get_store()
        if serializer.validated_data.get('product_type') == Product.PRODUCT_TYPE_AFFILIATE_CLICKBANK and not store.can_list_clickbank:
            raise ValidationError({'product_type': 'ClickBank offers are available to Partner and Official stores.'})
        self.validate_store_publication(serializer)
        product = serializer.save(store=store)
        if product.is_digital or product.product_type == Product.PRODUCT_TYPE_AFFILIATE_CLICKBANK:
            Specification.objects.filter(product=product).delete()
        transaction.on_commit(lambda: invalidate_catalog(product))

    @transaction.atomic
    def perform_update(self, serializer):
        store = self.get_store()
        if serializer.validated_data.get('product_type') == Product.PRODUCT_TYPE_AFFILIATE_CLICKBANK and not store.can_list_clickbank:
            raise ValidationError({'product_type': 'ClickBank offers are available to Partner and Official stores.'})
        self.validate_store_publication(serializer)
        existing = self.get_object()
        old_price = str(existing.price)
        old_stock = existing.available
        product = serializer.save()
        if product.is_digital or product.product_type == Product.PRODUCT_TYPE_AFFILIATE_CLICKBANK:
            Specification.objects.filter(product=product).delete()
        transaction.on_commit(lambda: invalidate_catalog(product))
        from product.tasks import dispatch_wishlist_alerts_task
        transaction.on_commit(lambda: dispatch_wishlist_alerts_task.delay(product.pk, old_price, old_stock))

    def perform_destroy(self, instance):
        if instance.is_blocked:
            raise ValidationError({'detail': 'Blocked products cannot be deleted while under platform moderation.'})
        raise ValidationError({'detail': 'Unpublish products with visibility=false to preserve carts and order history.'})

    @action(detail=True, methods=['patch'])
    def inventory(self, request, store_pk=None, pk=None):
        serializer = InventorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            product = get_object_or_404(Product.objects.select_for_update(), pk=pk, store=self.get_store())
            if product.is_digital:
                raise ValidationError({'available': 'Digital products have unlimited inventory.'})
            old_stock = product.available
            product.available = serializer.validated_data['available']
            product.save(update_fields=['available', 'updated'])
            transaction.on_commit(lambda: invalidate_catalog(product))
            from product.tasks import dispatch_wishlist_alerts_task
            transaction.on_commit(lambda: dispatch_wishlist_alerts_task.delay(product.pk, str(product.price), old_stock))
        return Response(self.get_serializer(product).data)

    @action(detail=True, methods=['get', 'post'])
    def images(self, request, store_pk=None, pk=None):
        product = self.get_object()
        if request.method == 'GET':
            return Response(MerchantImageSerializer(product.images.all(), many=True, context={'request': request}).data)
        serializer = MerchantImageSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        image = serializer.validated_data['image']
        should_protect_preview = (
            product.is_digital
            and product.digital_preview_watermark
            and is_image_digital_asset(product.digital_file_url)
        )
        if should_protect_preview:
            from django.core.files.storage import default_storage
            from utils.digital_previews import create_digital_preview

            preview_path = create_digital_preview(image, product.store_id, product.pk)
            if not preview_path:
                raise ValidationError({
                    'image': 'Unable to create a protected preview from this image.'
                })
            # ProductImg keeps the public derivative in the ordinary gallery. The
            # submitted artwork is never saved as a publicly-addressable gallery file.
            img_obj = ProductImg.objects.create(product=product, image=preview_path)
        else:
            img_obj = serializer.save(product=product)
        is_cover = request.data.get('is_cover') in {'true', '1', True} or not product.image_url
        if is_cover and img_obj.image:
            image_url = normalize_media_url(request.build_absolute_uri(img_obj.image.url) if request else img_obj.image.url)
            product.image_url = image_url
            product.save(update_fields=['image_url', 'updated'])
        invalidate_catalog(product)
        return Response(
            MerchantImageSerializer(img_obj, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        methods=['POST'],
        request=MerchantImageSerializer,
        responses={201: serializers.DictField(child=serializers.CharField())},
    )
    @action(
        detail=True,
        methods=['post'],
        url_path='variant-image',
        parser_classes=[MultiPartParser, FormParser],
    )
    def variant_image(self, request, store_pk=None, pk=None):
        """Upload a gallery image for one digital product option."""
        product = self.get_object()
        if not product.is_digital:
            raise ValidationError({
                'detail': 'Variant image uploads are available for digital products only.'
            })

        serializer = MerchantImageSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        image = serializer.validated_data['image']

        from utils.digital_previews import is_image_digital_asset

        should_watermark = (
            product.digital_preview_watermark
            and is_image_digital_asset(product.digital_file_url)
        )
        if should_watermark:
            from django.core.files.storage import default_storage
            from utils.digital_previews import create_digital_preview

            preview_path = create_digital_preview(image, product.store_id, product.pk)
            if not preview_path:
                raise ValidationError({
                    'image': 'Unable to create a protected preview from this image.'
                })
            image_url = normalize_media_url(request.build_absolute_uri(default_storage.url(preview_path)))
        else:
            image_record = serializer.save(product=product)
            image_url = normalize_media_url(request.build_absolute_uri(image_record.image.url))

        invalidate_catalog(product)
        return Response({'image_url': image_url}, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=['post'],
        url_path='variant-file',
        parser_classes=[MultiPartParser, FormParser],
    )
    def variant_file(
        self,
        request: Request,
        store_pk: str | None = None,
        pk: str | None = None,
    ) -> Response:
        """Store a private downloadable asset for one digital product option."""
        product = self.get_object()
        if not product.is_digital:
            raise ValidationError({'detail': 'Variant files are available for digital products only.'})
        file_obj = request.FILES.get('file')
        if not file_obj:
            raise ValidationError({'file': 'Select a variant digital file to upload.'})
        if file_obj.size > 50 * 1024 * 1024:
            raise ValidationError({'file': 'Variant digital files must be 50 MB or smaller.'})
        asset_type = serializers.ChoiceField(
            choices=['image', 'audio', 'video', 'document'],
        ).run_validation(request.data.get('asset_type'))

        from django.core.files.storage import default_storage
        from uuid import uuid4
        from utils.digital_previews import (
            create_digital_preview,
            digital_asset_kind,
            is_image_digital_asset,
        )

        from utils.digital_previews import allowed_variant_asset_types
        if asset_type not in allowed_variant_asset_types(product.digital_file_url):
            raise ValidationError({'asset_type': 'Choose a variant type compatible with the main digital file.'})

        upload_kind = digital_asset_kind(file_obj.name)
        if upload_kind != asset_type:
            raise ValidationError({
                'file': f'Upload a {asset_type} file for this variant.'
            })

        response_data = {}
        preview_path = None
        if is_image_digital_asset(file_obj.name):
            preview_path = create_digital_preview(file_obj, product.store_id, product.pk)
            if not preview_path:
                raise ValidationError({'file': 'Unable to create a protected preview from this image.'})
            preview_url = normalize_media_url(request.build_absolute_uri(default_storage.url(preview_path)))
            response_data['image_url'] = preview_url
        safe_name = f'digital_products/{product.store_id}/{product.pk}/variants/{uuid4().hex[:12]}_{file_obj.name}'
        try:
            digital_file_url = default_storage.save(safe_name, file_obj)
        except Exception as error:
            if preview_path:
                try:
                    default_storage.delete(preview_path)
                except Exception:
                    logger.exception('Failed to remove preview after variant file upload failed for product %s', product.pk)
            logger.exception('Variant digital upload failed for product %s', product.pk)
            raise ValidationError({'file': 'Unable to store this variant file. Please try again.'}) from error
        response_data['digital_file_url'] = digital_file_url
        return Response(response_data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'], url_path=r'images/(?P<image_pk>\d+)/cover')
    def set_cover_image(self, request, store_pk=None, pk=None, image_pk=None):
        product = self.get_object()
        img = get_object_or_404(ProductImg, pk=image_pk, product=product)
        if img.image:
            image_url = normalize_media_url(request.build_absolute_uri(img.image.url) if request else img.image.url)
            product.image_url = image_url
            product.save(update_fields=['image_url', 'updated'])
            invalidate_catalog(product)
        return Response(self.get_serializer(product).data)

    @extend_schema(methods=["GET"], responses=UploadLimits)
    @extend_schema(methods=["POST"], request=UploadRequest, responses=UploadState)
    @action(detail=True, methods=['get', 'post'], url_path='digital-upload', parser_classes=[JSONParser, MultiPartParser, FormParser])
    def digital_upload(self, request: Request, store_pk: str | None = None, pk: str | None = None) -> Response:
        from store.transfers import CHUNK_BYTES, maximum_bytes, signed_part_url, state, transfer
        product = self.get_object()
        if request.method == "GET":
            return Response({"max_size": maximum_bytes(), "chunk_size": CHUNK_BYTES})
        serializer = UploadRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            upload = transfer(product, request.user, serializer.validated_data)
        except (APIException, Http404):
            raise
        except Exception:
            logger.exception('Digital transfer failed for product %s', product.pk)
            return Response({'detail': 'Unable to store this upload part. Retry the same file to resume.'}, status=503)
        if upload.status == 'complete':
            invalidate_catalog(product)
        result = state(upload)
        if serializer.validated_data["operation"] == "sign":
            url = signed_part_url(upload, serializer.validated_data["part"])
            if url:
                result["part_url"] = url
        return Response(UploadState(result).data)

    @action(detail=True, methods=['get', 'post'], url_path='digital-file', parser_classes=[MultiPartParser, FormParser])
    def digital_file(self, request, store_pk=None, pk=None):
        product = self.get_object()
        if request.method == 'GET':
            from django.core.files.storage import default_storage
            from django.http import FileResponse
            from urllib.parse import urlparse
            raw = (product.digital_file_url or '').strip()
            path = urlparse(raw).path.lstrip('/')
            bucket = getattr(settings, 'MINIO_BUCKET_NAME', 'commerce-images')
            known_prefixes = (
                f'{bucket}/media/',
                f'{bucket}/',
                'commerce-images/media/',
                'commerce-images/',
                'commerce-media/media/',
                'commerce-media/',
                'media/',
            )
            for prefix in known_prefixes:
                if path.startswith(prefix):
                    path = path[len(prefix):]
                    break

            variants = [path]
            if path.startswith('media/'):
                variants.append(path[len('media/'):])
            else:
                variants.append(f'media/{path}')

            resolved_path = None
            for candidate in variants:
                if candidate and default_storage.exists(candidate):
                    resolved_path = candidate
                    break

            if not resolved_path:
                return Response({'detail': 'This product has no uploaded digital file.'}, status=404)
            from utils.ranged_files import managed_file_response
            return managed_file_response(request, resolved_path)
        file_obj = request.FILES.get('file')
        if not file_obj:
            raise ValidationError({'file': 'Please select a digital file to upload.'})

        # Max file size 50 MB
        if file_obj.size > 50 * 1024 * 1024:
            raise ValidationError({'file': 'Digital files must be 50 MB or smaller.'})

        from django.core.files.storage import default_storage
        import os
        from uuid import uuid4

        from utils.digital_previews import allowed_variant_asset_types, digital_asset_kind
        allowed_types = allowed_variant_asset_types(file_obj.name)
        if any(
            (variant.get('digital_file_type') and variant['digital_file_type'] not in allowed_types)
            or (variant.get('digital_file_url') and digital_asset_kind(variant['digital_file_url']) not in allowed_types)
            for variant in product.variants if isinstance(variant, dict)
        ):
            raise ValidationError({'file': 'Remove incompatible variant files before changing the main digital file type.'})
        safe_name = f"digital_products/{product.store_id}/{product.id}/{uuid4().hex[:12]}_{file_obj.name}"
        try:
            saved_path = default_storage.save(safe_name, file_obj)
        except Exception:
            logger.exception("Digital file upload failed for product %s", product.pk)
            return Response(
                {
                    "detail": "Unable to upload this file right now. Please try again shortly."
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        # Store the stable object key. Signed URLs are generated only after an
        # authenticated purchase or a backoffice audit request.
        file_url = saved_path

        product.is_digital = True
        product.digital_file_url = file_url
        product.weight = Decimal('0.00')
        from utils.digital_previews import is_image_digital_asset

        update_fields = ['is_digital', 'digital_file_url', 'weight', 'updated']
        if is_image_digital_asset(file_obj.name):
            product.preview_status = Product.PREVIEW_STATUS_PENDING
            product.preview_error = ''
            update_fields.extend(['preview_status', 'preview_error'])
        else:
            product.preview_status = Product.PREVIEW_STATUS_NONE
            product.preview_error = ''
            product.digital_preview_watermark = False
            update_fields.extend(['preview_status', 'preview_error', 'digital_preview_watermark'])
            cleaned_variants = [
                {
                    **variant,
                    'image_url': '',
                } if isinstance(variant, dict) and is_protected_digital_preview_url(variant.get('image_url')) else variant
                for variant in product.variants
            ]
            if cleaned_variants != product.variants:
                product.variants = cleaned_variants
                update_fields.append('variants')
        product.save(update_fields=update_fields)
        if product.preview_status == Product.PREVIEW_STATUS_PENDING:
            from product.tasks import schedule_digital_preview
            schedule_digital_preview(product.pk, file_url)

        invalidate_catalog(product)

        return Response({
            'digital_file_url': file_url,
            'image_url': product.image_url,
            'digital_preview_watermark': product.digital_preview_watermark,
            'preview_status': product.preview_status,
            'file_name': file_obj.name,
            'file_size': file_obj.size,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['delete'], url_path=r'images/(?P<image_pk>\d+)')
    def delete_image(self, request, store_pk=None, pk=None, image_pk=None):
        with transaction.atomic():
            product = get_object_or_404(Product.objects.select_for_update(), pk=pk, store=self.get_store())
            image = get_object_or_404(ProductImg, pk=image_pk, product=product)
            remaining = product.images.exclude(pk=image.pk).exclude(image='')
            image_url = normalize_media_url(image.image.url) if image.image else ''
            is_cover = bool(image_url and normalize_media_url(product.image_url) in {
                image_url, normalize_media_url(request.build_absolute_uri(image_url)),
            })
            if not remaining.exists() and (not product.image_url or is_cover):
                raise ValidationError({'image': 'Keep at least one product image. Add a replacement before deleting this image.'})
            if is_cover:
                product.image_url = normalize_media_url(remaining.first().image.url)
                product.save(update_fields=['image_url', 'updated'])
            image.delete()
            transaction.on_commit(lambda: invalidate_catalog(product))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get', 'put'])
    def specifications(self, request, store_pk=None, pk=None):
        with transaction.atomic():
            product = get_object_or_404(Product.objects.select_for_update(), pk=pk, store=self.get_store())
            if product.is_digital or product.product_type == Product.PRODUCT_TYPE_AFFILIATE_CLICKBANK:
                return Response(
                    {'error': 'Digital and affiliate partner products cannot have physical specifications.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            specification = Specification.objects.filter(product=product).first()
            if request.method == 'GET':
                return Response(MerchantSpecificationSerializer(specification).data if specification else {})
            serializer = MerchantSpecificationSerializer(specification, data=request.data, context={'product': product})
            serializer.is_valid(raise_exception=True)
            serializer.save(product=product)
            transaction.on_commit(lambda: invalidate_catalog(product))
        return Response(serializer.data)

    @extend_schema(
        operation_id='merchant_product_analytics',
        parameters=[
            OpenApiParameter('time_zone', str, description='IANA display timezone', default='UTC'),
            OpenApiParameter('days', str, description='Period window: 1, 7, 30, 90, 365, all', default='30'),
        ],
    )
    @action(detail=True, methods=['get'])
    def analytics(self, request, store_pk=None, pk=None):
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        from django.utils import timezone
        from django.db.models import Avg, Count
        from payment.models import Order
        from payment.order_filters import orders_for_store, orders_for_product, refunds_for_product
        from core.models import Wishlist, Review, Refund
        from store.serializers import ProductAnalyticsQuerySerializer

        store = self.get_store()
        product = get_object_or_404(
            Product.objects.filter(store=store).select_related('specification'),
            pk=pk,
        )

        query = ProductAnalyticsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        time_zone = query.validated_data["time_zone"]
        days = query.validated_data["days"]

        with timezone.override(ZoneInfo(time_zone)):
            today = timezone.localdate()
            if days == 1:
                start_date = today
                start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
                end_dt = timezone.make_aware(datetime.combine(today + timedelta(days=1), datetime.min.time()))
            elif days == "all":
                start_date = None
                start_dt = None
                end_dt = timezone.make_aware(datetime.combine(today + timedelta(days=1), datetime.min.time()))
            else:
                start_date = today - timedelta(days=days - 1)
                start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
                end_dt = timezone.make_aware(datetime.combine(today + timedelta(days=1), datetime.min.time()))

            store_orders = orders_for_store(Order.objects.all(), store.pk)
            orders = orders_for_product(store_orders, product.id).filter(
                ordered=True,
                status__in=["confirmed", "shipped", "delivered", "picked up"],
                created__lt=end_dt,
            )
            if start_dt:
                orders = orders.filter(created__gte=start_dt)

            orders = orders.select_related('cart').prefetch_related('cart__cart_items')

            timeline_map = {}
            if days == 1:
                for hour in range(24):
                    label = f"{hour:02d}:00"
                    timeline_map[label] = {
                        "date": today.isoformat(),
                        "label": label,
                        "units": 0,
                        "revenue": Decimal("0.00"),
                    }
            elif days != "all":
                curr = start_date
                while curr <= today:
                    label = curr.isoformat()
                    timeline_map[label] = {
                        "date": label,
                        "label": curr.strftime("%b %d"),
                        "units": 0,
                        "revenue": Decimal("0.00"),
                    }
                    curr += timedelta(days=1)

            gross_revenue = Decimal("0.00")
            units_sold = 0
            order_ids = set()
            variant_map = {}

            for order in orders:
                order_has_product = False
                order_local_dt = timezone.localtime(order.created)
                if days == 1:
                    bucket_key = order_local_dt.strftime("%H:00")
                else:
                    bucket_key = order_local_dt.date().isoformat()

                if order.items_snapshot:
                    for item in order.items_snapshot:
                        if str(item.get("product")) == str(product.id):
                            order_has_product = True
                            qty = int(item.get("quantity") or 1)
                            price = Decimal(str(item.get("unit_price") or item.get("price") or product.price or "0.00"))
                            line_tot = Decimal(str(item.get("line_total") or (price * qty)))
                            var_name = str(item.get("variant_name") or item.get("variant") or "Standard")

                            gross_revenue += line_tot
                            units_sold += qty

                            if bucket_key not in timeline_map:
                                timeline_map[bucket_key] = {
                                    "date": order_local_dt.date().isoformat(),
                                    "label": order_local_dt.strftime("%b %d") if days != 1 else bucket_key,
                                    "units": 0,
                                    "revenue": Decimal("0.00"),
                                }
                            timeline_map[bucket_key]["units"] += qty
                            timeline_map[bucket_key]["revenue"] += line_tot

                            if var_name not in variant_map:
                                variant_map[var_name] = {"name": var_name, "units": 0, "revenue": Decimal("0.00")}
                            variant_map[var_name]["units"] += qty
                            variant_map[var_name]["revenue"] += line_tot
                elif order.cart_id:
                    for item in order.cart.cart_items.all():
                        if item.product_id == product.id:
                            order_has_product = True
                            qty = item.quantity
                            price = Decimal(str(product.price or "0.00"))
                            line_tot = price * qty
                            var_name = str(getattr(item, "variant", "") or "Standard")

                            gross_revenue += line_tot
                            units_sold += qty

                            if bucket_key not in timeline_map:
                                timeline_map[bucket_key] = {
                                    "date": order_local_dt.date().isoformat(),
                                    "label": order_local_dt.strftime("%b %d") if days != 1 else bucket_key,
                                    "units": 0,
                                    "revenue": Decimal("0.00"),
                                }
                            timeline_map[bucket_key]["units"] += qty
                            timeline_map[bucket_key]["revenue"] += line_tot

                            if var_name not in variant_map:
                                variant_map[var_name] = {"name": var_name, "units": 0, "revenue": Decimal("0.00")}
                            variant_map[var_name]["units"] += qty
                            variant_map[var_name]["revenue"] += line_tot

                if order_has_product:
                    order_ids.add(order.id)

            refunds = refunds_for_product(Refund.objects.all(), product.id, orders).filter(
                created__lt=end_dt,
            )
            if start_dt:
                refunds = refunds.filter(created__gte=start_dt)

            refund_count = refunds.count()
            refund_amount = Decimal("0.00")
            refund_units = 0
            for ref in refunds:
                if ref.selected_items:
                    for item in ref.selected_items:
                        if str(item.get("product")) == str(product.id):
                            refund_units += int(item.get("quantity") or 1)
                            refund_amount += Decimal(str(item.get("refund_amount") or item.get("line_total") or "0.00"))
                else:
                    refund_units += 1
                    refund_amount += ref.refund_amount

            refund_rate = round((refund_units / units_sold * 100), 1) if units_sold > 0 else 0.0

            wishlist_count = Wishlist.objects.filter(product=product, liked=True).count()
            review_stats = Review.objects.filter(product=product).aggregate(avg=Avg('rating'), count=Count('id'))
            avg_rating = float(review_stats["avg"] or product.average_rating or 0.0)
            total_reviews = review_stats["count"] or 0

            available_stock = product.available
            effective_days = days if isinstance(days, int) else 30
            daily_run_rate = round(units_sold / effective_days, 2) if effective_days > 0 else 0.0
            if product.is_digital:
                days_stock_remaining = None
            elif daily_run_rate > 0:
                days_stock_remaining = int(round(available_stock / daily_run_rate))
            else:
                days_stock_remaining = None

            timeline = [
                {
                    "date": val["date"],
                    "label": val["label"],
                    "units": val["units"],
                    "revenue": f"{val['revenue']:.2f}",
                }
                for val in timeline_map.values()
            ]
            if days == "all":
                timeline.sort(key=lambda x: x["date"])

            variants = []
            for v_data in variant_map.values():
                pct = round(float(v_data["units"]) / units_sold * 100, 1) if units_sold > 0 else 0.0
                variants.append({
                    "name": v_data["name"],
                    "units": v_data["units"],
                    "revenue": f"{v_data['revenue']:.2f}",
                    "percentage": pct,
                })
            variants.sort(key=lambda x: x["units"], reverse=True)

            return Response({
                "product": {
                    "id": product.id,
                    "title": product.title,
                    "price": f"{product.price:.2f}",
                    "image_url": product.image_url,
                    "is_digital": product.is_digital,
                    "available": product.available,
                    "visibility": product.visibility,
                },
                "summary": {
                    "gross_revenue": f"{gross_revenue:.2f}",
                    "units_sold": units_sold,
                    "orders_count": len(order_ids),
                    "refund_count": refund_count,
                    "refund_units": refund_units,
                    "refund_amount": f"{refund_amount:.2f}",
                    "refund_rate": refund_rate,
                    "available_stock": available_stock,
                    "daily_run_rate": daily_run_rate,
                    "days_stock_remaining": days_stock_remaining,
                    "wishlist_count": wishlist_count,
                    "average_rating": round(avg_rating, 1),
                    "total_reviews": total_reviews,
                },
                "timeline": timeline,
                "variants": variants,
            })



class VisibilityScheduleViewSet(viewsets.ModelViewSet):
    queryset = Schedule.objects.none()
    permission_classes = [IsAuthenticated]
    serializer_class = ScheduleSerializer
    pagination_class = MerchantPagination
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Schedule.objects.none()
        membership = StoreTeamMember.objects.filter(
            store_id=OuterRef('store_id'),
            user=user,
            status=StoreTeamMember.STATUS_ACTIVE,
            role__in=PRODUCT_MANAGEMENT_ROLES,
        )
        allowed = Q(store__user=user) | Q(Exists(membership))
        if is_admin(user):
            allowed |= Q(store__is_official=True)
        return Schedule.objects.filter(allowed).order_by('make_visible_at', 'pk')

    def perform_create(self, serializer):
        product = serializer.validated_data['product']
        if not StoreViewSet.get_queryset(self).filter(pk=product.store_id).exists():
            raise ValidationError({'product': 'Choose a product from your store.'})
        if not can_manage_products(product.store, self.request.user):
            raise PermissionDenied('You do not have permission to schedule visibility for this store.')
        if serializer.validated_data['make_visible_at'] <= timezone.now():
            raise ValidationError({'make_visible_at': 'Choose a future time.'})
        serializer.save(store=product.store)

    def perform_destroy(self, instance):
        if not can_manage_products(instance.store, self.request.user):
            raise PermissionDenied('You do not have permission to delete visibility schedules for this store.')
        instance.delete()

