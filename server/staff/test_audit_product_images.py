from django.test import TestCase
from core.models import User
from product.models import Product, ProductImg
from store.models import Store
from staff.serializers import StoreAuditProductSerializer


class AuditProductImageTests(TestCase):
    def test_missing_image_is_empty_and_gallery_is_authoritative(self):
        user = User.objects.create_user(email='image-owner@example.test', password='test')
        store = Store.objects.create(user=user, name='Image fixture')
        product = Product.objects.create(store=store, title='Product', price='10', discount=0, available=1)
        self.assertEqual(StoreAuditProductSerializer(product).data['image_url'], '')
        product.image_url = 'https://example.test/cover.png'
        product.save()
        self.assertEqual(StoreAuditProductSerializer(product).data['image_url'], product.image_url)
        ProductImg.objects.create(product=product, image='images/recorded-cover.png')
        value = StoreAuditProductSerializer(product).data['image_url']
        self.assertIn('images/recorded-cover.png', value)
        self.assertNotIn('digital_products/', value)

    def test_catalog_thumbnail_response_remains_store_scoped_and_staff_only(self):
        from django.test import override_settings
        from rest_framework.test import APIClient

        owner = User.objects.create_user(email='scoped-owner@example.test', password='test')
        admin = User.objects.create_superuser(email='scoped-admin@example.test', password='test')
        first = Store.objects.create(user=owner, name='First store')
        second = Store.objects.create(user=owner, name='Second store')
        visible = Product.objects.create(store=first, title='First product', price='10', discount=0, available=1, image_url='https://example.test/first.png')
        hidden = Product.objects.create(store=second, title='Second product', price='10', discount=0, available=1, image_url='https://example.test/second.png')
        client = APIClient()
        with override_settings(ROOT_URLCONF='storefront.urls'):
            client.force_authenticate(admin)
            response = client.get(f'/api/v1/admin/staff/stores/{first.pk}/products/')
            self.assertEqual(response.status_code, 200)
            rows = response.data['results']
            self.assertEqual([row['id'] for row in rows], [visible.pk])
            self.assertEqual(rows[0]['image_url'], visible.image_url)
            self.assertNotIn(hidden.image_url, str(response.data))
            client.force_authenticate(owner)
            self.assertEqual(client.get(f'/api/v1/admin/staff/stores/{first.pk}/products/').status_code, 403)
