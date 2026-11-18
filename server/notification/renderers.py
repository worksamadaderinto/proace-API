"""Negotiate SSE while keeping authentication/broker errors as ordinary JSON."""
from typing import Any

from rest_framework.renderers import JSONRenderer


class EventStreamRenderer(JSONRenderer):
    media_type = 'text/event-stream'
    format = 'event-stream'

    def render(
        self,
        data: Any,
        accepted_media_type: str | None = None,
        renderer_context: dict[str, Any] | None = None,
    ) -> bytes:
        # Successful streams use StreamingHttpResponse and bypass render().
        # DRF still negotiates a renderer before authentication and the view.
        if renderer_context and renderer_context.get('response') is not None:
            renderer_context['response']['Content-Type'] = 'application/json'
        return super().render(data, accepted_media_type, renderer_context)
