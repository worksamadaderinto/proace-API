from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from store.models import DigitalUpload
from store.transfers import discard

class Command(BaseCommand):
    help = "Abort expired private multipart uploads and remove staging parts."

    def handle(self, *args, **options):
        removed = 0
        for identity in DigitalUpload.objects.filter(status='uploading', expires_at__lte=timezone.now()).values_list('pk', flat=True).iterator():
            with transaction.atomic():
                upload = DigitalUpload.objects.select_for_update().get(pk=identity)
                if upload.status == 'uploading' and upload.expires_at <= timezone.now():
                    discard(upload)
                    removed += 1
        from django.core.files.storage import default_storage
        for upload in DigitalUpload.objects.filter(status='complete').iterator():
            for part in upload.parts.values():
                if 'name' in part:
                    default_storage.delete(part['name'])
        self.stdout.write(f"Expired uploads cleaned: {removed}")
