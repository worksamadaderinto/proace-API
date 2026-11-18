"""Run media CPU jobs separately from notification delivery and recovery."""
from django.conf import settings
from django.core.management.base import BaseCommand
from codematics.celery import app


class Command(BaseCommand):
    help = "Run the dedicated, bounded media task worker."

    def handle(self, *args: object, **options: object) -> None:
        app.worker_main([
            "worker", "--loglevel=INFO", "--queues", settings.CELERY_MEDIA_QUEUE,
            "--concurrency", str(settings.CELERY_MEDIA_WORKER_CONCURRENCY),
            "--prefetch-multiplier=1",
        ])
