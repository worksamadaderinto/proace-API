"""Serializer fields for structured values decoded after request parsing."""
from rest_framework import serializers
from rest_framework.exceptions import ParseError

from core.parsers import validate_input_text


class ValidatedJSONField(serializers.JSONField):
    def to_internal_value(self, data: object) -> object:
        try:
            value = super().to_internal_value(data)
            validate_input_text(value)
        except RecursionError:
            raise serializers.ValidationError(
                "Request data is nested too deeply. Please simplify your entry."
            ) from None
        except ParseError as exc:
            raise serializers.ValidationError(exc.detail) from None
        return value
