"""
Dependency Injection Gateway for Object Storage Signed URLs.

Decouples S3 / MinIO presigned URL generation and multi-part upload mechanics
from direct boto3 SDK calls.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Protocol, runtime_checkable

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)


@runtime_checkable
class StorageSigner(Protocol):
    """Protocol for generating signed / presigned object storage URLs."""

    def generate_part_url(self, upload: Any, part: int, expires_in: int = 900) -> str | None:
        """Generate a presigned upload URL for a specific chunk/part number."""
        ...


class S3StorageSigner:
    """Production S3 / MinIO signer using boto3."""

    def generate_part_url(self, upload: Any, part: int, expires_in: int = 900) -> str | None:
        from store.transfers import object_key, s3

        if not s3():
            return None

        import boto3

        storage = default_storage
        endpoint = getattr(settings, "MINIO_PUBLIC_URL", "")
        if not getattr(settings, "DEBUG", False) and not endpoint.startswith("https://"):
            raise ValidationError("Secure public object-storage transport must be configured.")

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=getattr(storage, "access_key", None),
            aws_secret_access_key=getattr(storage, "secret_key", None),
            region_name=getattr(storage, "region_name", None),
            config=getattr(storage, "client_config", None),
        )
        return client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": getattr(storage, "bucket_name", "proace-storage"),
                "Key": object_key(upload.object_name),
                "UploadId": upload.provider_id,
                "PartNumber": part,
            },
            ExpiresIn=expires_in,
        )


class MockStorageSigner:
    """Mock in-memory storage signer for testing without AWS or MinIO."""

    def __init__(self, base_url: str = "https://mock-storage.local/upload-part") -> None:
        self.base_url = base_url
        self.signed_requests: list[dict[str, Any]] = []

    def generate_part_url(self, upload: Any, part: int, expires_in: int = 900) -> str | None:
        object_name = getattr(upload, "object_name", str(upload))
        provider_id = getattr(upload, "provider_id", "mock-upload-id")
        url = f"{self.base_url}/{object_name}?part={part}&upload_id={provider_id}&expires={expires_in}"
        self.signed_requests.append({
            "upload": upload,
            "part": part,
            "expires_in": expires_in,
            "url": url,
        })
        return url


def get_storage_signer() -> StorageSigner:
    """Factory to retrieve the configured storage signer."""
    backend_path = getattr(
        settings,
        "STORAGE_SIGNER_BACKEND",
        "store.storage_signer.S3StorageSigner",
    )
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()
