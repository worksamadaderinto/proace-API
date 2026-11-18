"""Validate query text without consuming signed bodies or uploaded files."""
from collections.abc import Callable

from django.core.exceptions import TooManyFieldsSent
from django.http import HttpRequest, HttpResponse, JsonResponse
from rest_framework.exceptions import ParseError

from core.parsers import validate_input_text


class InputQueryValidationMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            validate_input_text(request.GET)
        except TooManyFieldsSent:
            return JsonResponse({"error": {
                "code": "parse_error", "message": "Too many query fields. Please simplify your request.", "details": None,
            }}, status=400)
        except ParseError as error:
            return JsonResponse({"error": {
                "code": "parse_error", "message": str(error.detail), "details": None,
            }}, status=400)
        return self.get_response(request)
