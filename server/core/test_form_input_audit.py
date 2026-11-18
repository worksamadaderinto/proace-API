from io import BytesIO
import json

import pytest
from django.test import override_settings
from rest_framework.exceptions import ParseError
from rest_framework.test import APIClient

from core.models import User
from core.parsers import ValidatedJSONParser


@pytest.mark.django_db
@override_settings(ROOT_URLCONF='storefront.urls')
@pytest.mark.parametrize('body', [[], ['unexpected'], 'unexpected', True, 123, None])
def test_cart_rejects_non_object_payload_without_server_error(body: object) -> None:
    user = User.objects.create_user(email='form-shape@example.test', password='fixture-only')
    client = APIClient()
    client.force_authenticate(user)
    response = client.generic('POST', '/api/v1/cart/', json.dumps(body), content_type='application/json')
    assert response.status_code == 400


def test_excessively_nested_json_returns_parse_error() -> None:
    body = b'{"field":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    with pytest.raises(ParseError):
        ValidatedJSONParser().parse(BytesIO(body))


@pytest.mark.django_db
@override_settings(ROOT_URLCONF='storefront.urls')
def test_search_rejects_invalid_query_text() -> None:
    user = User.objects.create_superuser(email='query-input@example.test', password='FixturePassword2026!')
    client = APIClient()
    client.force_authenticate(user)
    response = client.get('/api/v1/admin/taxes/', {'search': 'fixture\x00text'})
    assert response.status_code == 400


@pytest.mark.parametrize('query', ['search=bad%00&search=good', 'bad%00key=value'])
def test_query_validation_checks_repeated_values_and_names(query: str) -> None:
    from django.http import HttpResponse
    from django.test import RequestFactory
    from core.input_middleware import InputQueryValidationMiddleware

    middleware = InputQueryValidationMiddleware(lambda request: HttpResponse('ok'))
    assert middleware(RequestFactory().get('/api/v1/products/?' + query)).status_code == 400


def test_query_validation_preserves_legitimate_text() -> None:
    from django.http import HttpResponse
    from django.test import RequestFactory
    from core.input_middleware import InputQueryValidationMiddleware

    value = "O'Connor <shop> 😀 東京"
    request = RequestFactory().get('/api/v1/products/', {'search': value})
    middleware = InputQueryValidationMiddleware(lambda request: HttpResponse('ok'))
    assert middleware(request).status_code == 200
    assert request.GET['search'] == value


@pytest.mark.parametrize('constant', ['NaN', 'Infinity', '-Infinity'])
def test_non_finite_json_numbers_are_rejected(constant: str) -> None:
    with pytest.raises(ParseError):
        ValidatedJSONParser().parse(BytesIO(('{"amount":' + constant + '}').encode()))


@override_settings(DATA_UPLOAD_MAX_NUMBER_FIELDS=1)
def test_excessive_query_fields_return_sanitized_json() -> None:
    from django.http import HttpResponse
    from django.test import RequestFactory
    from core.input_middleware import InputQueryValidationMiddleware

    middleware = InputQueryValidationMiddleware(lambda request: HttpResponse('ok'))
    response = middleware(RequestFactory().get('/api/v1/products/?a=1&b=2'))
    assert response.status_code == 400
    assert response['Content-Type'] == 'application/json'


@pytest.mark.parametrize('limit', ['RequestDataTooBig', 'TooManyFieldsSent', 'TooManyFilesSent'])
def test_form_limit_errors_are_actionable_without_internal_details(limit: str) -> None:
    from django.core import exceptions
    from utils.exceptions import custom_exception_handler

    response = custom_exception_handler(getattr(exceptions, limit)('private diagnostic fixture'), {})
    assert response.status_code == 400
    assert 'private diagnostic' not in str(response.data)
    assert 'reduce' in str(response.data)
