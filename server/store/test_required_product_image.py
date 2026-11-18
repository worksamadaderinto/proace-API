import json
from io import BytesIO
from tempfile import TemporaryDirectory

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APIClient

from core.models import User
from product.models import Product, ProductImg
from store.models import Store, StoreAddress
from store.serializers import MerchantProductSerializer


@pytest.fixture
def merchant():
    owner = User.objects.create_user(email='image-owner@example.test', password='test-password')
    store = Store.objects.create(user=owner, name='Image test store', status=Store.STATUS_ACTIVE)
    client = APIClient()
    client.force_authenticate(owner)
    return client, store


def payload(**changes):
    data = {'title': 'Test product', 'price': '10.00', 'category': 'electronics', 'description': 'Product description', 'discount': 0, 'available': 1, 'visibility': False}
    data.update(changes)
    return data


def image_upload():
    image = BytesIO()
    Image.new('RGB', (16, 16)).save(image, format='PNG')
    return SimpleUploadedFile('cover.png', image.getvalue(), content_type='image/png')


@pytest.mark.django_db
@pytest.mark.parametrize('extra', [
    {}, {'image_url': ''}, {'image_url': '   '}, {'images': [{'image': 'https://example.test/ignored.jpg'}]},
    {'visibility': False}, {'is_digital': True, 'digital_file_url': 'https://example.test/art.png'},
    {'product_type': Product.PRODUCT_TYPE_AFFILIATE_CLICKBANK, 'affiliate_hoplink': 'https://example.test/offer'},
])
def test_creation_requires_explicit_image(merchant, extra):
    client, store = merchant
    response = client.post(f'/api/v1/stores/{store.pk}/products/', payload(**extra), format='json')
    assert response.status_code == 400
    assert 'image_url' in response.data['error']['details']
    assert not Product.objects.filter(store=store).exists()


@pytest.mark.django_db
def test_url_cover_creation(merchant):
    client, store = merchant
    response = client.post(f'/api/v1/stores/{store.pk}/products/', payload(image_url='https://example.test/cover.jpg'), format='json')
    assert response.status_code == 201
    assert Product.objects.get(pk=response.data['id']).image_url == 'https://example.test/cover.jpg'


@pytest.mark.django_db
def test_first_cover_is_uploaded_with_product(merchant):
    client, store = merchant
    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        response = client.post(f'/api/v1/stores/{store.pk}/products/', {
            'product': json.dumps(payload(visibility=False, tags=['test'])),
            'cover_image': image_upload(),
        }, format='multipart')
        assert response.status_code == 201, response.data
        product = Product.objects.get(pk=response.data['id'])
        assert product.images.count() == 1
        assert product.image_url
        assert list(product.tags.names()) == ['test']
        deleted = client.delete(f'/api/v1/stores/{store.pk}/products/{product.pk}/images/{product.images.get().pk}/')
        assert deleted.status_code == 400
        assert product.images.count() == 1


@pytest.mark.django_db
def test_invalid_upload_leaves_no_product(merchant):
    client, store = merchant
    response = client.post(f'/api/v1/stores/{store.pk}/products/', {
        'product': json.dumps(payload()),
        'cover_image': SimpleUploadedFile('fake.png', b'not an image', content_type='image/png'),
    }, format='multipart')
    assert response.status_code == 400
    assert not Product.objects.filter(store=store).exists()


@pytest.mark.django_db
def test_cannot_clear_only_cover(merchant):
    _, store = merchant
    product = Product.objects.create(store=store, **payload(image_url='https://example.test/cover.png'))
    serializer = MerchantProductSerializer(product, data={'image_url': ''}, partial=True)
    assert not serializer.is_valid()
    assert 'image_url' in serializer.errors
    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        ProductImg.objects.create(product=product, image=image_upload())
        serializer = MerchantProductSerializer(product, data={'image_url': ''}, partial=True)
        assert serializer.is_valid(), serializer.errors


@pytest.mark.django_db
def test_removing_cover_selects_remaining_image(merchant):
    client, store = merchant
    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        product = Product.objects.create(store=store, **payload())
        first = ProductImg.objects.create(product=product, image=image_upload())
        second = ProductImg.objects.create(product=product, image=image_upload())
        product.image_url = 'http://testserver' + first.image.url
        product.save(update_fields=['image_url'])
        response = client.delete(f'/api/v1/stores/{store.pk}/products/{product.pk}/images/{first.pk}/')
        assert response.status_code == 204
        product.refresh_from_db()
        assert product.image_url == second.image.url
        response = client.delete(f'/api/v1/stores/{store.pk}/products/{product.pk}/images/{second.pk}/')
        assert response.status_code == 400


@pytest.mark.django_db
def test_cover_upload_cannot_create_in_another_store(merchant):
    client, _ = merchant
    other = User.objects.create_user(email='other-image-owner@example.test', password='test-password')
    store = Store.objects.create(user=other, name='Other store', status=Store.STATUS_ACTIVE)
    response = client.post(f'/api/v1/stores/{store.pk}/products/', {
        'product': json.dumps(payload()), 'cover_image': image_upload(),
    }, format='multipart')
    assert response.status_code == 404
    assert not Product.objects.filter(store=store).exists()


@pytest.mark.django_db
def test_atomic_cover_replacement(merchant):
    client, store = merchant
    product = Product.objects.create(store=store, **payload(image_url='https://example.test/previous.jpg'))
    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        response = client.patch(f'/api/v1/stores/{store.pk}/products/{product.pk}/', {
            'product': json.dumps({'image_url': ''}), 'cover_image': image_upload(),
        }, format='multipart')
        assert response.status_code == 200, response.data
        product.refresh_from_db()
        assert product.image_url == product.images.get().image.url


@pytest.mark.django_db
@pytest.mark.parametrize('value', ['not-a-url', 'javascript:alert(1)', None])
def test_invalid_cover_url_cannot_bypass_requirement(merchant, value):
    client, store = merchant
    response = client.post(f'/api/v1/stores/{store.pk}/products/', payload(image_url=value), format='json')
    assert response.status_code == 400
    assert not Product.objects.filter(store=store).exists()


@pytest.mark.django_db
def test_informational_video_is_separate_public_media_and_can_be_removed(merchant):
    client, store = merchant
    from django.utils import timezone
    store.verified_at = timezone.now()
    store.save(update_fields=['verified_at'])
    StoreAddress.objects.create(store=store, is_default=True, address='10 Test Avenue',
                                city='Orlando', state='Florida', country='United States', zip='32801')

    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        response = client.post(f'/api/v1/stores/{store.pk}/products/', payload(
            image_url='https://example.test/cover.jpg', visibility=True,
        ), format='json')
        assert response.status_code == 201, response.data
        product = Product.objects.get(pk=response.data['id'])
        endpoint = f'/api/v1/stores/{store.pk}/products/{product.pk}/'
        invalid = client.patch(endpoint, {
            'product': '{}',
            'informational_video': SimpleUploadedFile('fake.mp4', b'<html>wrong format'),
        }, format='multipart')
        assert invalid.status_code == 400
        product.refresh_from_db()
        assert not product.informational_video

        # MP4 file-type box exercises the upload boundary; playback is a browser check.
        mp4_header = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom'
        saved = client.patch(endpoint, {
            'product': '{}',
            'informational_video': SimpleUploadedFile('intro.mp4', mp4_header, content_type='video/mp4'),
        }, format='multipart')
        assert saved.status_code == 200, saved.data
        product.refresh_from_db()
        assert product.informational_video.name.startswith('images/product-videos/')
        with product.informational_video.open('rb') as file:
            assert file.read() == mp4_header
        assert not product.digital_file_url

        public = APIClient().get(f'/api/v1/products/{product.pk}/')
        assert public.status_code == 200
        assert public.data['informational_video'].endswith(product.informational_video.name)
        removed = client.patch(endpoint, {'informational_video': None}, format='json')
        assert removed.status_code == 200
        product.refresh_from_db()
        assert not product.informational_video
