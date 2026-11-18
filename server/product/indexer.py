"""
Dependency Injection Gateway for Catalog & Notification Search Indexing.

Decouples search indexing, bulk ingestion, and full-text querying from the concrete
Elasticsearch SDK and socket cluster, enabling fast and hermetic offline testing.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Protocol, runtime_checkable

from django.conf import settings

logger = logging.getLogger(__name__)


@runtime_checkable
class SearchIndexer(Protocol):
    """Protocol defining document indexing, deletion, and search queries."""

    def index(self, index: str, id: Any, document: dict[str, Any]) -> Any:
        """Index or update a document."""
        ...

    def delete(self, index: str, id: Any, ignore: list[int] | None = None) -> Any:
        """Delete a document by id."""
        ...

    def search(
        self,
        index: str,
        query: dict[str, Any],
        sort: list[Any] | None = None,
        from_: int = 0,
        size: int = 20,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a search query against an index."""
        ...


class ElasticsearchSearchIndexer:
    """Production indexer delegating to the Elasticsearch cluster client."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is not None:
            return self._client
        from product.search import client
        return client()

    def index(self, index: str, id: Any, document: dict[str, Any]) -> Any:
        return self.client.index(index=index, id=id, document=document)

    def delete(self, index: str, id: Any, ignore: list[int] | None = None) -> Any:
        kwargs: dict[str, Any] = {"index": index, "id": id}
        if ignore:
            kwargs["ignore"] = ignore
        return self.client.delete(**kwargs)

    def search(
        self,
        index: str,
        query: dict[str, Any],
        sort: list[Any] | None = None,
        from_: int = 0,
        size: int = 20,
        **kwargs: Any,
    ) -> dict[str, Any]:
        call_kwargs: dict[str, Any] = {
            "index": index,
            "query": query,
            "from_": from_,
            "size": size,
        }
        if sort is not None:
            call_kwargs["sort"] = sort
        call_kwargs.update(kwargs)
        return self.client.search(**call_kwargs)


class MockSearchIndexer:
    """In-memory deterministic search indexer for offline testing."""

    def __init__(self, predefined_search_results: list[dict[str, Any]] | None = None) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.predefined_search_results = predefined_search_results or []
        self.search_calls: list[dict[str, Any]] = []

    def index(self, index: str, id: Any, document: dict[str, Any]) -> Any:
        key = (str(index), str(id))
        self.documents[key] = dict(document)
        return {"result": "created", "_id": str(id), "_index": str(index)}

    def delete(self, index: str, id: Any, ignore: list[int] | None = None) -> Any:
        key = (str(index), str(id))
        self.documents.pop(key, None)
        self.deleted.append(key)
        return {"result": "deleted", "_id": str(id), "_index": str(index)}

    def search(
        self,
        index: str,
        query: dict[str, Any],
        sort: list[Any] | None = None,
        from_: int = 0,
        size: int = 20,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.search_calls.append({
            "index": index,
            "query": query,
            "sort": sort,
            "from_": from_,
            "size": size,
            "kwargs": kwargs,
        })
        if self.predefined_search_results:
            hits = self.predefined_search_results
        else:
            hits = [
                {"_id": doc_id, "_source": doc}
                for (idx, doc_id), doc in self.documents.items()
                if idx == str(index)
            ]
        sliced_hits = hits[from_ : from_ + size]
        return {
            "hits": {
                "total": {"value": len(hits), "relation": "eq"},
                "hits": sliced_hits,
            }
        }


def get_search_indexer() -> SearchIndexer:
    """Factory to retrieve the active search indexer configured in settings."""
    backend_path = getattr(
        settings,
        "SEARCH_INDEXER_BACKEND",
        "product.indexer.ElasticsearchSearchIndexer",
    )
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()
