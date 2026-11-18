import json

import pytest
from django.http import QueryDict
from django.test import override_settings
from store.serializers import MerchantProductUploadSerializer


def serializer_for(value):
    data = QueryDict(mutable=True)
    data["product"] = value
    return MerchantProductUploadSerializer(data=data)


@pytest.mark.parametrize("value", ["bad\x00text", "\ud800", "\udfff"])
def test_decoded_invalid_text_is_rejected(value):
    serializer = serializer_for(json.dumps({"metadata": {"value": value}}))
    assert not serializer.is_valid()
    assert "product" in serializer.errors


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_embedded_json_is_rejected(value):
    serializer = serializer_for('{"metadata":' + value + '}')
    assert not serializer.is_valid()
    assert "product" in serializer.errors


@override_settings(API_INPUT_MAX_DEPTH=4)
def test_embedded_json_depth_is_checked():
    serializer = serializer_for('{"nested":[[[[[0]]]]]}')
    assert not serializer.is_valid()
    assert "product" in serializer.errors


def test_excessive_nesting_does_not_crash():
    serializer = serializer_for('{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}')
    assert not serializer.is_valid()
    assert "product" in serializer.errors


def test_legitimate_text_is_preserved():
    product = {"title": "O'Connor <shop> 東京 😀", "description": "  deliberate spacing  "}
    serializer = serializer_for(json.dumps(product))
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["product"] == product


@pytest.mark.django_db
def test_multipart_invalid_metadata_has_no_product_or_file_side_effects():
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from rest_framework.test import APIClient
    from core.models import User
    from product.models import Product
    from store.models import Store
    from store.test_required_product_image import image_upload, payload

    owner = User.objects.create_user(email="multipart-owner@example.test", password="test-password")
    store = Store.objects.create(user=owner, name="Metadata test", status=Store.STATUS_ACTIVE)
    client = APIClient()
    client.force_authenticate(owner)
    with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
        response = client.post(f"/api/v1/stores/{store.pk}/products/", {
            "product": json.dumps(payload(metadata={"text": "bad\x00text"})),
            "cover_image": image_upload(),
        }, format="multipart")
        assert response.status_code == 400
        assert not Product.objects.filter(store=store).exists()
        assert not any(path.is_file() for path in Path(media).rglob("*"))
