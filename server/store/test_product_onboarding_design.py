import json
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from product.models import Product
from store.serializers import MerchantProductSerializer
from store.test_required_product_image import merchant, payload, image_upload


def document(name='guide.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 test content', content_type='application/pdf')


@pytest.mark.django_db
def test_digital_creation_uploads_private_content_and_cover_atomically(merchant):
    client, store = merchant
    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        response = client.post(f'/api/v1/stores/{store.pk}/products/', {
            'product': json.dumps(payload(is_digital=True)), 'cover_image': image_upload(), 'digital_file': document(),
        }, format='multipart')
        assert response.status_code == 201, response.data
        product = Product.objects.get(pk=response.data['id'])
        assert product.digital_file_url.startswith(f'digital_products/{store.pk}/{product.pk}/')
        assert product.image_url and product.images.count() == 1
        assert product.weight == 0 and product.available == 0
        assert response.data['allowed_variant_asset_types'] == ['document']


@pytest.mark.django_db
def test_artwork_upload_generates_only_a_protected_public_cover(merchant):
    client, store = merchant
    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        response = client.post(f'/api/v1/stores/{store.pk}/products/', {
            'product': json.dumps(payload(is_digital=True)), 'digital_file': image_upload(),
        }, format='multipart')
        assert response.status_code == 201, response.data
        product = Product.objects.get(pk=response.data['id'])
        assert product.digital_file_url.startswith('digital_products/')
        assert 'digital-previews/' in product.image_url
        assert 'digital-previews/' in product.images.get().image.name
        assert product.digital_preview_watermark


@pytest.mark.django_db
def test_variant_files_are_attached_to_the_correct_option(merchant):
    client, store = merchant
    options = [{'name': 'PDF edition', 'price_delta': '0.00'}, {'name': 'Bonus guide', 'price_delta': '1.00'}]
    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        response = client.post(f'/api/v1/stores/{store.pk}/products/', {
            'product': json.dumps(payload(is_digital=True, variants=options)),
            'cover_image': image_upload(), 'digital_file': document(),
            'variant_files': [document('bonus.pdf')], 'variant_indexes': json.dumps([1]),
        }, format='multipart')
        assert response.status_code == 201, response.data
        product = Product.objects.get(pk=response.data['id'])
        assert not product.variants[0].get('digital_file_url')
        assert '/variants/' in product.variants[1]['digital_file_url']
        assert product.variants[1]['digital_file_type'] == 'document'


@pytest.mark.django_db
@pytest.mark.parametrize('index,name', [(0, 'track.mp3'), (4, 'bonus.pdf')])
def test_invalid_variant_upload_rolls_back_creation(merchant, index, name):
    client, store = merchant
    response = client.post(f'/api/v1/stores/{store.pk}/products/', {
        'product': json.dumps(payload(is_digital=True, variants=[{'name': 'Edition', 'price_delta': '0.00'}])),
        'cover_image': image_upload(), 'digital_file': document(),
        'variant_files': [document(name)], 'variant_indexes': json.dumps([index]),
    }, format='multipart')
    assert response.status_code == 400
    assert not Product.objects.filter(store=store).exists()


@pytest.mark.django_db
def test_storage_failure_does_not_create_partial_product(merchant):
    client, store = merchant
    with patch('django.core.files.storage.default_storage.save', side_effect=OSError('test storage failure')):
        response = client.post(f'/api/v1/stores/{store.pk}/products/', {
            'product': json.dumps(payload(is_digital=True)), 'cover_image': image_upload(), 'digital_file': document(),
        }, format='multipart')
    assert response.status_code == 400
    assert not Product.objects.filter(store=store).exists()
    assert 'test storage failure' not in str(response.data)


@pytest.mark.django_db
def test_seller_cannot_assign_platform_merchandising_label(merchant):
    client, store = merchant
    response = client.post(f'/api/v1/stores/{store.pk}/products/', payload(image_url='https://example.test/cover.jpg', label='bestseller'), format='json')
    assert response.status_code == 201, response.data
    assert Product.objects.get(pk=response.data['id']).label != 'bestseller'


@pytest.mark.django_db
def test_existing_product_cannot_save_incompatible_variant_asset(merchant):
    _, store = merchant
    product = Product.objects.create(store=store, **payload(is_digital=True, digital_file_url='private/art.png', image_url='https://example.test/cover.jpg'))
    serializer = MerchantProductSerializer(product, data={'variants': [{'name': 'Audio', 'price_delta': '0.00', 'digital_file_type': 'audio'}]}, partial=True)
    assert not serializer.is_valid()
    assert 'variants' in serializer.errors


@pytest.mark.django_db
def test_store_role_is_never_fabricated_without_authenticated_membership(merchant):
    from types import SimpleNamespace
    from django.contrib.auth.models import AnonymousUser
    from store.serializers import StoreSerializer
    client, store = merchant
    assert StoreSerializer(store).data['user_role'] == ''
    assert StoreSerializer(store, context={'request': SimpleNamespace(user=AnonymousUser())}).data['user_role'] == ''
    outsider = type(store.user).objects.create_user(email='outsider-role@example.test', password='test-password')
    assert StoreSerializer(store, context={'request': SimpleNamespace(user=outsider)}).data['user_role'] == ''
    assert StoreSerializer(store, context={'request': SimpleNamespace(user=store.user)}).data['user_role'] == 'owner'


@pytest.mark.django_db
def test_direct_variant_endpoint_cannot_bypass_main_file_compatibility(merchant):
    client, store = merchant
    product = Product.objects.create(store=store, **payload(is_digital=True, digital_file_url='private/art.png'))
    response = client.post(f'/api/v1/stores/{store.pk}/products/{product.pk}/variant-file/', {'file': document('song.mp3'), 'asset_type': 'audio'}, format='multipart')
    assert response.status_code == 400


@pytest.mark.django_db
def test_combined_upload_limit_is_advertised_and_enforced(merchant):
    client, store = merchant
    response = client.get(f'/api/v1/stores/{store.pk}/')
    limit = response.data['product_upload_limit_bytes']
    upload = document()
    upload.size = limit + 1
    serializer = MerchantProductSerializer(data=payload(is_digital=True, image_url='https://example.test/cover.jpg', digital_file=upload))
    assert not serializer.is_valid()
    assert 'digital_file' in serializer.errors


@pytest.mark.django_db
def test_variant_type_cannot_mislabel_existing_content(merchant):
    client, store = merchant
    product = Product.objects.create(store=store, **payload(is_digital=True, digital_file_url='private/song.mp3', image_url='https://example.test/cover.jpg'))
    url = f'digital_products/{store.pk}/{product.pk}/variants/song.mp3'
    response = client.patch(f'/api/v1/stores/{store.pk}/products/{product.pk}/', {'variants': [{'name': 'Video edition', 'price_delta': '0.00', 'digital_file_type': 'video', 'digital_file_url': url}]}, format='json')
    assert response.status_code == 400


@pytest.mark.django_db
def test_replacement_upload_can_change_a_variant_between_compatible_types(merchant):
    client, store = merchant
    product = Product.objects.create(store=store, **payload(is_digital=True, digital_file_url='private/song.mp3', image_url='https://example.test/cover.jpg'))
    old_url = f'digital_products/{store.pk}/{product.pk}/variants/song.mp3'
    options = [{'name': 'Video edition', 'price_delta': '0.00', 'digital_file_type': 'video', 'digital_file_url': old_url}]
    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        response = client.patch(f'/api/v1/stores/{store.pk}/products/{product.pk}/', {
            'product': json.dumps({'variants': options}), 'variant_files': [document('video.mp4')], 'variant_indexes': json.dumps([0]),
        }, format='multipart')
        assert response.status_code == 200, response.data
        product.refresh_from_db()
        assert product.variants[0]['digital_file_type'] == 'video'
        assert product.variants[0]['digital_file_url'].endswith('video.mp4')
        assert product.variants[0]['digital_file_url'] != old_url
