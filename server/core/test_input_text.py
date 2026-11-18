from io import BytesIO

import pytest
from django.conf import settings
from rest_framework.exceptions import ParseError

from core.parsers import ValidatedFormParser, ValidatedJSONParser, validate_input_text


@pytest.mark.parametrize("value", [{"name": "O'Connor <shop> 😀", "password": " secret "}, ["é", "東京"]])
def test_legitimate_text_is_preserved(value):
    validate_input_text(value)


@pytest.mark.parametrize("body", [b'{"name":"bad\\u0000text"}', b'{"nested":["\\ud800"]}', b'{"\\u0000":"value"}'])
def test_json_parser_rejects_invalid_text(body):
    with pytest.raises(ParseError):
        ValidatedJSONParser().parse(BytesIO(body))


def test_form_parser_checks_repeated_values():
    with pytest.raises(ParseError):
        ValidatedFormParser().parse(BytesIO(b'name=bad%00&name=good'))


def test_password_reset_policy_is_ten_minutes():
    assert settings.PASSWORD_RESET_TIMEOUT == 600
