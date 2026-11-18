"""Reject invalid text at the API boundary without rewriting user content."""
from math import isfinite
from typing import BinaryIO

from django.conf import settings
from django.http import QueryDict
from rest_framework.exceptions import ParseError
from rest_framework.parsers import DataAndFiles, FormParser, JSONParser, MultiPartParser


def validate_input_text(value: object) -> None:
    """Preserve punctuation/Unicode; reject NUL and unpaired surrogate code points."""
    max_depth = getattr(settings, "API_INPUT_MAX_DEPTH", 32)
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > max_depth:
            raise ParseError("Request data is nested too deeply. Please simplify your entry.")
        if isinstance(item, str):
            if "\x00" in item or any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise ParseError("Text contains an unsupported character. Please review your entry.")
        elif isinstance(item, float) and not isfinite(item):
            raise ParseError("Enter a finite numeric value.")
        elif hasattr(item, "lists"):
            for key, values in item.lists():
                pending.extend((entry, depth + 1) for entry in [key, *values])
        elif isinstance(item, dict):
            pending.extend((entry, depth + 1) for entry in item.keys())
            pending.extend((entry, depth + 1) for entry in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((entry, depth + 1) for entry in item)


class ValidatedJSONParser(JSONParser):
    def parse(
        self, stream: BinaryIO, media_type: str | None = None,
        parser_context: dict[str, object] | None = None,
    ) -> object:
        try:
            data = super().parse(stream, media_type, parser_context)
        except RecursionError:
            raise ParseError("Request data is nested too deeply. Please simplify your entry.") from None
        if not isinstance(data, dict):
            raise ParseError("Submit a JSON object with the required fields.")
        validate_input_text(data)
        return data


class ValidatedFormParser(FormParser):
    def parse(
        self, stream: BinaryIO, media_type: str | None = None,
        parser_context: dict[str, object] | None = None,
    ) -> QueryDict:
        data = super().parse(stream, media_type, parser_context)
        validate_input_text(data)
        return data


class ValidatedMultiPartParser(MultiPartParser):
    def parse(
        self, stream: BinaryIO, media_type: str | None = None,
        parser_context: dict[str, object] | None = None,
    ) -> DataAndFiles:
        data = super().parse(stream, media_type, parser_context)
        validate_input_text(data.data)
        for key, files in data.files.lists():
            validate_input_text(key)
            for uploaded in files:
                validate_input_text(uploaded.name)
        return data
