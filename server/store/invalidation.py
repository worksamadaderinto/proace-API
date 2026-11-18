from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from django.conf import settings
from django.utils.module_loading import import_string

if TYPE_CHECKING:
    from product.models import Product

logger = logging.getLogger(__name__)


@runtime_checkable
class CatalogInvalidator(Protocol):
    """Abstract interface for catalog and cart cache invalidation."""

    def invalidate(self, product: Any) -> None:
        """Invalidate cached views, queries, and related carts for a given product."""
        ...

    def invalidate_many(self, products: tuple[Product, ...]) -> None:
        """Invalidate a group of products and their affected carts."""
        ...


class DatabaseCatalogInvalidator:
    """Production cache invalidator purging Redis and cart cache records."""

    def invalidate(self, product: Any) -> None:
        self.invalidate_many((product,))

    def invalidate_many(self, products: tuple[Product, ...]) -> None:
        from cart.cache import invalidate_cart_caches
        from cart.models import Cart
        from product.cache import invalidate_product_payload_caches
        from storefront.catalog_cache import invalidate_catalog_snapshot

        if not products:
            return
        invalidate_catalog_snapshot()
        invalidate_product_payload_caches(tuple((product.pk, product.store_id) for product in products))
        affected = Cart.objects.filter(
            cart_items__product_id__in=[product.pk for product in products], ordered=False,
        ).values_list("pk", "user_id").distinct()
        invalidate_cart_caches(tuple(affected))


class MockCatalogInvalidator:
    """In-memory cache invalidator recording invalidated products for unit tests."""

    def __init__(self) -> None:
        self.invalidated_products: list[Any] = []

    def invalidate(self, product: Any) -> None:
        self.invalidate_many((product,))

    def invalidate_many(self, products: tuple[Product, ...]) -> None:
        self.invalidated_products.extend(products)


def get_catalog_invalidator() -> CatalogInvalidator:
    """Resolve and instantiate the configured catalog invalidator from settings."""
    backend_path = getattr(
        settings,
        "CATALOG_INVALIDATOR_BACKEND",
        "store.invalidation.DatabaseCatalogInvalidator",
    )
    if isinstance(backend_path, str):
        try:
            backend_cls = import_string(backend_path)
            return backend_cls()
        except Exception as exc:
            logger.warning(
                "Could not load CATALOG_INVALIDATOR_BACKEND '%s': %s. Falling back to DatabaseCatalogInvalidator.",
                backend_path,
                exc,
            )
            return DatabaseCatalogInvalidator()
    return DatabaseCatalogInvalidator()
