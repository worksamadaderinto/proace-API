from datetime import timedelta
from decimal import Decimal
import pytest
from django.utils import timezone
from rest_framework.test import APIClient
from core.models import User
from payment.models import Coupon, TaxJurisdiction
from store.models import Store

pytestmark = pytest.mark.django_db

@pytest.fixture
def operator():
    actor = User.objects.create_user(email='bulk-admin@example.test', password='fixture', is_superuser=True, is_staff=True)
    client = APIClient()
    client.force_authenticate(actor)
    return client, actor


def records(resource):
    if resource in {'users', 'staff'}:
        return [User.objects.create_user(email=f'bulk-{resource}-{i}@example.test', password='fixture', is_staff=resource == 'staff') for i in range(3)]
    if resource == 'stores':
        return [Store.objects.create(user=User.objects.create_user(email=f'bulk-owner-{i}@example.test', password='fixture'), name=f'Store {i}', status=Store.STATUS_ACTIVE, verified_at=timezone.now()) for i in range(3)]
    if resource == 'coupons':
        return [Coupon.objects.create(code=f'BULK{i}', discount=10, num_available=10, valid_to=timezone.now()+timedelta(days=2)) for i in range(3)]
    return [TaxJurisdiction.objects.create(country='US', state=state, name=state, rate=Decimal('.04'), is_active=True) for state in ['TX', 'NY', 'FL']]


def active(row):
    row.refresh_from_db()
    return row.active if isinstance(row, Coupon) else row.status == Store.STATUS_ACTIVE if isinstance(row, Store) else row.is_active


@pytest.mark.parametrize('resource', ['taxes', 'coupons', 'users', 'staff', 'stores'])
def test_selected_rows_only_and_repeat_is_idempotent(operator, resource):
    client, actor = operator
    rows = records(resource)
    payload = {'resource':resource, 'ids':[rows[0].pk, rows[1].pk], 'is_active':False}
    result = client.post('/api/v1/admin/bulk-actions/', payload, format='json')
    assert result.status_code == 200, result.data
    assert result.data['updated_count'] == 2
    assert not active(rows[0]) and not active(rows[1]) and active(rows[2])
    if resource in {'users', 'staff'}:
        version = rows[0].credential_version
    repeat = client.post('/api/v1/admin/bulk-actions/', payload, format='json')
    assert repeat.status_code == 200 and repeat.data['updated_count'] == 0
    if resource in {'users', 'staff'}:
        rows[0].refresh_from_db()
        assert rows[0].credential_version == version
    restored = client.post('/api/v1/admin/bulk-actions/', {**payload, 'is_active':True}, format='json')
    assert restored.status_code == 200, restored.data
    assert all(active(row) for row in rows)


@pytest.mark.parametrize('resource', ['staff', 'stores'])
def test_ordinary_staff_cannot_bulk_manage_privileged_resources(operator, resource):
    client, _ = operator
    staff = User.objects.create_user(email='bulk-ordinary@example.test', password='fixture', is_staff=True)
    rows = records(resource)
    client.force_authenticate(staff)
    result = client.post('/api/v1/admin/bulk-actions/', {'resource':resource,'ids':[rows[0].pk],'is_active':False}, format='json')
    assert result.status_code == 403 and active(rows[0])


def test_protected_account_in_selection_rolls_back_everything(operator):
    client, actor = operator
    customer = records('users')[0]
    result = client.post('/api/v1/admin/bulk-actions/', {'resource':'users','ids':[customer.pk,actor.pk],'is_active':False}, format='json')
    assert result.status_code == 403
    assert active(customer) and active(actor)


@pytest.mark.parametrize('kind', ['official', 'pending'])
def test_protected_stores_cannot_be_changed(operator, kind):
    client, _ = operator
    rows = records('stores')
    protected = rows[1]
    protected.is_official = kind == 'official'
    protected.status = Store.STATUS_PENDING if kind == 'pending' else Store.STATUS_ACTIVE
    protected.save()
    result = client.post('/api/v1/admin/bulk-actions/', {'resource':'stores','ids':[rows[0].pk,protected.pk],'is_active':False}, format='json')
    assert result.status_code == 400 and active(rows[0])


def test_staff_cannot_change_other_operator_through_users(operator):
    client, _ = operator
    rows = records('staff')
    client.force_authenticate(rows[0])
    result = client.post('/api/v1/admin/bulk-actions/', {'resource':'users','ids':[rows[1].pk],'is_active':False}, format='json')
    assert result.status_code == 403 and active(rows[1])


def test_unverified_staff_cannot_be_activated(operator):
    client, _ = operator
    row = records('staff')[0]
    row.is_active=False
    row.staff_email_verified=False
    row.save()
    result = client.post('/api/v1/admin/bulk-actions/', {'resource':'staff','ids':[row.pk],'is_active':True}, format='json')
    assert result.status_code == 400 and not active(row)


@pytest.mark.parametrize('payload', [{'resource':'taxes','ids':[],'is_active':False}, {'resource':'taxes','ids':[1,1],'is_active':False}, {'resource':'taxes','ids':[1]}, {'resource':'taxes','ids':[1],'is_active':[]}, {'resource':'taxes','ids':[999999],'is_active':False}])
def test_invalid_selection_fails_without_mutating(operator, payload):
    client, _ = operator
    rows = records('taxes')
    assert client.post('/api/v1/admin/bulk-actions/', payload, format='json').status_code == 400
    assert all(active(row) for row in rows)


def test_customer_and_anonymous_cannot_bulk_mutate(operator):
    client, _ = operator
    customer = records('users')[0]
    row = records('taxes')[0]
    payload = {'resource':'taxes','ids':[row.pk],'is_active':False}
    client.force_authenticate(customer)
    assert client.post('/api/v1/admin/bulk-actions/', payload, format='json').status_code == 403
    client.force_authenticate(None)
    assert client.post('/api/v1/admin/bulk-actions/', payload, format='json').status_code == 401
    assert active(row)


def test_batch_limit_is_authoritative_and_rejected_before_mutation(operator, settings):
    client, _ = operator
    rows = records('taxes')
    settings.ADMIN_BULK_MAX_RECORDS = 1
    capabilities = client.get('/api/v1/admin/bulk-actions/')
    assert capabilities.status_code == 200 and capabilities.data['max_records'] == 1
    response = client.post('/api/v1/admin/bulk-actions/', {'resource':'taxes','ids':[row.pk for row in rows],'is_active':False},format='json')
    assert response.status_code == 400
    assert all(active(row) for row in rows)


def test_list_hints_are_scoped_to_the_requesting_actor(operator):
    from staff.serializers import StaffUserSerializer, BackofficeStoreSerializer
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory
    client, actor = operator
    staff = records('staff')[0]
    customer = records('users')[0]
    request = Request(APIRequestFactory().get('/'))
    request._user = actor
    assert StaffUserSerializer(actor, context={'request':request}).data['bulk_actions'] == []
    assert StaffUserSerializer(staff, context={'request':request}).data['bulk_actions'] == ['activate','deactivate']
    staff.staff_email_verified=False
    assert StaffUserSerializer(staff, context={'request':request}).data['bulk_actions'] == ['deactivate']
    request._user = staff
    assert StaffUserSerializer(customer, context={'request':request}).data['bulk_actions'] == ['activate','deactivate']
    assert StaffUserSerializer(actor, context={'request':request}).data['bulk_actions'] == []
    store=records('stores')[0]
    assert BackofficeStoreSerializer(store, context={'request':request}).data['bulk_actions'] == []
