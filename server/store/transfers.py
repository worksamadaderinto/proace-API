"""Bounded authenticated chunks; MinIO multipart avoids assembling multi-GB bodies in Django."""
from __future__ import annotations

import hashlib
import math
import tempfile
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError

from product.models import Product
from store.models import DigitalUpload
from utils.digital_previews import allowed_variant_asset_types, digital_asset_kind

CHUNK_BYTES = 8 * 1024 * 1024
MAX_SESSIONS = 4
UPLOAD_LIFETIME = timedelta(hours=24)


def maximum_bytes() -> int:
    return int(getattr(settings, 'RESUMABLE_DIGITAL_MAX_BYTES', 5 * 1024 ** 3))


class UploadRequest(serializers.Serializer):
    operation = serializers.ChoiceField(choices=['start', 'sign', 'part', 'complete', 'abort'])
    upload = serializers.UUIDField(required=False)
    filename = serializers.CharField(max_length=160, required=False)
    size = serializers.IntegerField(min_value=1, required=False)
    fingerprint = serializers.RegexField(r'^[a-f0-9]{64}$', required=False)
    part = serializers.IntegerField(min_value=1, max_value=10000, required=False)
    file = serializers.FileField(required=False, allow_empty_file=False)


class UploadLimits(serializers.Serializer):
    max_size = serializers.IntegerField()
    chunk_size = serializers.IntegerField()


class UploadState(serializers.Serializer):
    id = serializers.UUIDField()
    size = serializers.IntegerField()
    chunk_size = serializers.IntegerField()
    parts = serializers.ListField(child=serializers.IntegerField())
    status = serializers.CharField()
    expires_at = serializers.DateTimeField()
    max_size = serializers.IntegerField()
    part_url = serializers.URLField(required=False)


def state(upload: DigitalUpload) -> dict[str, Any]:
    return {'id': upload.pk, 'size': upload.size, 'chunk_size': upload.chunk_size,
            'parts': sorted(int(key) for key in upload.parts), 'status': upload.status,
            'expires_at': upload.expires_at, 'max_size': maximum_bytes()}


def s3() -> tuple[Any, str] | None:
    storage = default_storage
    if hasattr(storage, 'bucket'):
        return storage.connection.meta.client, storage.bucket_name
    return None


def object_key(name: str) -> str:
    return default_storage._normalize_name(name)


def discard(upload: DigitalUpload) -> None:
    backend = s3()
    if backend and upload.provider_id:
        backend[0].abort_multipart_upload(Bucket=backend[1], Key=object_key(upload.object_name), UploadId=upload.provider_id)
    else:
        for part in upload.parts.values():
            default_storage.delete(part['name'])
    upload.status = 'aborted'
    upload.save(update_fields=['status', 'updated'])


def compatible(product: Product, filename: str) -> None:
    if not product.is_digital or product.product_type != Product.PRODUCT_TYPE_STANDARD:
        raise ValidationError('Choose a standard digital product.')
    if digital_asset_kind(filename) == 'image':
        raise ValidationError('Use the image uploader for protected artwork and its preview.')
    allowed = allowed_variant_asset_types(filename)
    if any((row.get('digital_file_type') and row['digital_file_type'] not in allowed)
           or (row.get('digital_file_url') and digital_asset_kind(row['digital_file_url']) not in allowed)
           for row in product.variants if isinstance(row, dict)):
        raise ValidationError('Remove incompatible variant content before changing the main file type.')


@transaction.atomic
def transfer(product: Product, owner: Any, data: dict[str, Any]) -> DigitalUpload:
    # Serialize starts/finalization per product, and recheck type after waiting.
    product = Product.objects.select_for_update().get(pk=product.pk)
    type(owner).objects.select_for_update().get(pk=owner.pk)
    operation = data['operation']
    if operation == 'start':
        if not all(key in data for key in ('filename', 'size', 'fingerprint')):
            raise ValidationError('Filename, size and file fingerprint are required.')
        filename = data['filename']
        if filename != PurePosixPath(filename).name or '\\' in filename or filename.startswith('.'):
            raise ValidationError('Choose a plain filename.')
        compatible(product, filename)
        if data['size'] > maximum_bytes():
            raise ValidationError('This file exceeds the configured resumable upload limit.')
        completed = DigitalUpload.objects.filter(owner=owner, product=product, fingerprint=data['fingerprint'], size=data['size'], filename=filename, status='complete', object_name=product.digital_file_url, expires_at__gt=timezone.now()).first()
        if completed:
            return completed
        candidates = DigitalUpload.objects.select_for_update().filter(owner=owner, product=product, fingerprint=data['fingerprint'], size=data['size'], filename=filename, status='uploading')
        existing = candidates.filter(expires_at__gt=timezone.now()).first()
        if existing:
            return existing
        for expired in candidates:
            discard(expired)
        if DigitalUpload.objects.filter(owner=owner, status='uploading', expires_at__gt=timezone.now()).count() >= MAX_SESSIONS:
            raise ValidationError('Finish or cancel an existing upload before starting another.')
        identity = uuid4()
        name = f'digital_products/{product.store_id}/{product.pk}/{identity.hex}_{filename}'
        backend = s3()
        provider = backend[0].create_multipart_upload(Bucket=backend[1], Key=object_key(name), ContentType='application/octet-stream', Metadata={'upload-session': identity.hex})['UploadId'] if backend else ''
        return DigitalUpload.objects.create(id=identity, owner=owner, product=product, filename=filename, fingerprint=data['fingerprint'], size=data['size'], chunk_size=CHUNK_BYTES, object_name=name, provider_id=provider, expires_at=timezone.now()+UPLOAD_LIFETIME)
    if 'upload' not in data:
        raise ValidationError('Select an upload session.')
    upload = DigitalUpload.objects.select_for_update().filter(pk=data['upload'], owner=owner, product=product).first()
    if not upload:
        raise NotFound('Upload session unavailable.')
    if operation == 'complete' and upload.status == 'complete':
        return upload
    if upload.status != 'uploading' or upload.expires_at <= timezone.now():
        raise ValidationError('This upload expired or was cancelled. Start again.')
    if operation == 'abort':
        discard(upload)
        return upload
    backend = s3()
    count = math.ceil(upload.size/upload.chunk_size)
    if operation == 'sign':
        index = data.get('part')
        if not index or index > count:
            raise ValidationError('Choose a valid upload part.')
        return upload
    if operation == 'part':
        index, file = data.get('part'), data.get('file')
        if not index or index > count:
            raise ValidationError('Choose a valid upload part.')
        expected = min(upload.chunk_size, upload.size-(index-1)*upload.chunk_size)
        if backend and not file:
            parts = backend[0].list_parts(Bucket=backend[1], Key=object_key(upload.object_name), UploadId=upload.provider_id, PartNumberMarker=index-1, MaxParts=1).get('Parts', [])
            if not parts or parts[0]['PartNumber'] != index or parts[0]['Size'] != expected:
                raise ValidationError('The storage provider has not received this complete part.')
            upload.parts = {**upload.parts, str(index): {'etag': parts[0]['ETag'], 'sha256': ''}}
            upload.save(update_fields=['parts', 'updated'])
            return upload
        if not file:
            raise ValidationError('Select an upload part file.')
        if file.size != expected:
            raise ValidationError('The upload part has an unexpected size.')
        content = file.read(expected+1)
        if len(content) != expected:
            raise ValidationError('Incomplete upload part.')
        checksum = hashlib.sha256(content).hexdigest()
        old = upload.parts.get(str(index))
        if old:
            if old['sha256'] != checksum:
                raise ValidationError('This part differs from the saved file. Select the original file or start again.')
            return upload
        if backend:
            etag = backend[0].upload_part(Bucket=backend[1], Key=object_key(upload.object_name), UploadId=upload.provider_id, PartNumber=index, Body=content)['ETag']
            part = {'etag': etag, 'sha256': checksum}
        else:
            name = default_storage.save(f'digital_products/{product.store_id}/{product.pk}/parts/{upload.pk}/{index}-{checksum}', ContentFile(content))
            part = {'name': name, 'sha256': checksum}
        upload.parts = {**upload.parts, str(index): part}
        upload.save(update_fields=['parts', 'updated'])
        return upload
    if sorted(int(key) for key in upload.parts) != list(range(1, count+1)):
        raise ValidationError('Upload every part before completing the file.')
    compatible(product, upload.filename)
    if backend:
        try:
            backend[0].complete_multipart_upload(Bucket=backend[1], Key=object_key(upload.object_name), UploadId=upload.provider_id, MultipartUpload={'Parts': [{'PartNumber': i, 'ETag': upload.parts[str(i)]['etag']} for i in range(1,count+1)]})
        except Exception:
            # Completion can succeed before the API connection fails; recover only this session's immutable key.
            metadata = backend[0].head_object(Bucket=backend[1], Key=object_key(upload.object_name))
            if metadata.get('ContentLength') != upload.size or metadata.get('Metadata', {}).get('upload-session') != upload.pk.hex:
                raise
        metadata = backend[0].head_object(Bucket=backend[1], Key=object_key(upload.object_name))
        if metadata['ContentLength'] != upload.size:
            raise ValidationError('The stored file size does not match this upload.')
    else:
        # Storage and SQL cannot commit atomically. Reuse a fully stored object
        # after a database rollback, and retain the parts until SQL commits.
        ready = default_storage.exists(upload.object_name) and default_storage.size(upload.object_name) == upload.size
        if not ready:
            if default_storage.exists(upload.object_name):
                default_storage.delete(upload.object_name)
            with tempfile.TemporaryFile() as assembled:
                for index in range(1, count + 1):
                    with default_storage.open(upload.parts[str(index)]['name'], 'rb') as part_file:
                        while block := part_file.read(256 * 1024):
                            assembled.write(block)
                if assembled.tell() != upload.size:
                    raise ValidationError('The assembled file size could not be verified.')
                assembled.seek(0)
                upload.object_name = default_storage.save(upload.object_name, File(assembled))
        part_names = [part['name'] for part in upload.parts.values()]
        def cleanup_parts() -> None:
            for name in part_names:
                try:
                    default_storage.delete(name)
                except OSError:
                    # The hourly cleanup retries completed sessions as well.
                    import logging
                    logging.getLogger(__name__).warning('Digital staging cleanup deferred for upload %s', upload.pk)
        transaction.on_commit(cleanup_parts)
    product.digital_file_url = upload.object_name
    product.save(update_fields=['digital_file_url', 'updated'])
    upload.status = 'complete'
    upload.save(update_fields=['object_name', 'status', 'updated'])
    return upload


def signed_part_url(upload: DigitalUpload, part: int, signer: Any = None) -> str | None:
    from store.storage_signer import get_storage_signer

    active_signer = signer or get_storage_signer()
    return active_signer.generate_part_url(upload, part)
