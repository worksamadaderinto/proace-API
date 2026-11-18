"""Atomic store settings, rollback and tenant isolation."""
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import User
from product.models import Product
from store.models import Store, StoreAddress, StoreInfo, StoreTeamMember
from store.serializers import StoreSerializer, StoreInfoSerializer, StoreAddressSerializer

pytestmark = pytest.mark.django_db


@pytest.fixture
def settings_case():
    owner = User.objects.create_user(email='settings-owner@example.test', password='fixture')
    store = Store.objects.create(user=owner, name='Original', status=Store.STATUS_ACTIVE, verified_at=timezone.now())
    profile = StoreInfo.objects.create(store=store, email='old@example.test', bio='Before')
    address = StoreAddress.objects.create(store=store, address='Old road', city='Austin', state='TX', country='United States', zip='78701', is_default=True)
    client = APIClient()
    client.force_authenticate(owner)
    payload = {
        'name': 'Updated',
        'profile': {'email': 'new@example.test', 'bio': 'After', 'pinned_products': [], 'phone1': '+12025550123', 'whatsapp': '', 'website': 'https://example.test'},
        'address': {'address': 'New road', 'city': 'Tampa', 'state': 'FL', 'country': 'United States', 'zip': '33602'},
    }
    return client, store, profile, address, payload


def persisted(store):
    store.refresh_from_db()
    return (store.name, list(StoreInfo.objects.filter(store=store).values('email', 'bio')),
            list(StoreAddress.objects.filter(store=store).values('address', 'city', 'state', 'zip', 'is_default')))


def test_settings_save_all_sections_and_preserve_scope(settings_case):
    client, store, profile, address, payload = settings_case
    other = Store.objects.create(name='Other', username='other-settings')
    response = client.patch(f'/api/v1/stores/{store.pk}/settings/', payload, format='json')
    assert response.status_code == 200, response.data
    store.refresh_from_db()
    profile.refresh_from_db()
    address.refresh_from_db()
    assert (store.name, profile.bio, address.city, address.is_default) == ('Updated', 'After', 'Tampa', True)
    assert response.data['store']['id'] == store.pk
    assert response.data['address']['store'] == store.pk
    assert StoreAddress.objects.filter(store=store, is_default=True).count() == 1
    other.refresh_from_db()
    assert other.name == 'Other'
    assert not StoreInfo.objects.filter(store=other).exists()


@pytest.mark.parametrize('stage', [StoreSerializer, StoreInfoSerializer, StoreAddressSerializer])
def test_settings_roll_back_failure_at_each_persistence_stage(settings_case, stage):
    client, store, _, _, payload = settings_case
    before = persisted(store)
    # Execute real serializer persistence before failing, including the last save.
    original = stage.save
    def fail_after_save(serializer, **kwargs):
        original(serializer, **kwargs)
        raise RuntimeError('controlled persistence failure')
    with patch.object(stage, 'save', fail_after_save):
        response = client.patch(f'/api/v1/stores/{store.pk}/settings/', payload, format='json')
    assert response.status_code == 500
    assert 'controlled persistence failure' not in str(response.data)
    assert persisted(store) == before


@pytest.mark.parametrize('section,field,value', [('address', 'zip', ''), ('profile', 'email', 'invalid')])
def test_settings_validate_all_sections_before_changes(settings_case, section, field, value):
    client, store, _, _, payload = settings_case
    before = persisted(store)
    payload[section][field] = value
    response = client.patch(f'/api/v1/stores/{store.pk}/settings/', payload, format='json')
    assert response.status_code == 400
    assert persisted(store) == before


def test_settings_reject_foreign_pinned_product_and_cross_store_access(settings_case):
    client, store, _, _, payload = settings_case
    outsider = User.objects.create_user(email='settings-outsider@example.test', password='fixture')
    other = Store.objects.create(user=outsider, name='Other', username='other-settings')
    product = Product.objects.create(store=other, title='Foreign', description='Fixture', price=1, discount=0, available=1)
    before = persisted(store)
    payload['profile']['pinned_products'] = [product.pk]
    assert client.patch(f'/api/v1/stores/{store.pk}/settings/', payload, format='json').status_code == 400
    assert persisted(store) == before
    client.force_authenticate(outsider)
    assert client.patch(f'/api/v1/stores/{store.pk}/settings/', payload, format='json').status_code == 404
    assert persisted(store) == before


@pytest.mark.parametrize('role,allowed', [(StoreTeamMember.ROLE_STORE_ADMIN, True), (StoreTeamMember.ROLE_CATALOG_MANAGER, False), (StoreTeamMember.ROLE_FULFILLMENT_SPECIALIST, False)])
def test_settings_team_permissions(settings_case, role, allowed):
    client, store, _, _, payload = settings_case
    member = User.objects.create_user(email=f'{role}@example.test', password='fixture')
    StoreTeamMember.objects.create(store=store, user=member, role=role)
    client.force_authenticate(member)
    before = persisted(store)
    response = client.patch(f'/api/v1/stores/{store.pk}/settings/', payload, format='json')
    assert response.status_code == (200 if allowed else 403), response.data
    if not allowed:
        assert persisted(store) == before


def test_settings_create_missing_related_records_atomically(settings_case):
    client, store, profile, address, payload = settings_case
    profile.delete()
    address.delete()
    response = client.patch(f'/api/v1/stores/{store.pk}/settings/', payload, format='json')
    assert response.status_code == 200, response.data
    assert StoreInfo.objects.get(store=store).bio == 'After'
    assert StoreAddress.objects.get(store=store, is_default=True).city == 'Tampa'


@pytest.mark.parametrize('admin_flag', ['is_superuser', 'is_business_admin'])
def test_settings_official_store_admin_access(settings_case, admin_flag):
    client, store, _, _, payload = settings_case
    store.is_official = True
    store.save()
    admin = User.objects.create_user(email=f'{admin_flag}@example.test', password='fixture', **{admin_flag: True})
    client.force_authenticate(admin)
    response = client.patch(f'/api/v1/stores/{store.pk}/settings/', payload, format='json')
    assert response.status_code == 200, response.data
    assert response.data['store']['user'] == store.user_id


def test_settings_reject_anonymous_without_mutation(settings_case):
    client, store, _, _, payload = settings_case
    before = persisted(store)
    client.force_authenticate(None)
    assert client.patch(f'/api/v1/stores/{store.pk}/settings/', payload, format='json').status_code == 401
    assert persisted(store) == before
