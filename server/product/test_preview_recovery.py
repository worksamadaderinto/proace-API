from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from core.models import User
from product.models import Product
from product.tasks import generate_digital_preview_task, recover_digital_previews, schedule_digital_preview
from store.models import Store


class PreviewRecoveryTests(TestCase):
    def setUp(self) -> None:
        user = User.objects.create_user(email="preview-jobs@example.test", password="test")
        store = Store.objects.create(user=user, name="Preview")
        self.product = Product.objects.create(
            store=store, title="Artwork", price=20, available=1, discount=0,
            is_digital=True, digital_file_url="digital_products/current.png", preview_status="pending",
        )

    def test_old_upload_is_not_processed(self) -> None:
        with patch("utils.digital_previews.create_digital_preview_from_storage") as generate:
            generate_digital_preview_task.run(self.product.pk, "digital_products/old.png")
        generate.assert_not_called()
        self.product.refresh_from_db()
        self.assertEqual(self.product.preview_status, "pending")

    def test_upload_replaced_during_processing_cannot_be_overwritten(self) -> None:
        def replace(*args: object) -> str:
            Product.objects.filter(pk=self.product.pk).update(digital_file_url="digital_products/new.png", preview_status="pending")
            return "images/digital-previews/old.jpg"
        with patch("utils.digital_previews.create_digital_preview_from_storage", side_effect=replace):
            generate_digital_preview_task.run(self.product.pk, self.product.digital_file_url)
        self.product.refresh_from_db()
        self.assertEqual(self.product.digital_file_url, "digital_products/new.png")
        self.assertEqual(self.product.preview_status, "pending")
        self.assertFalse(self.product.digital_preview_watermark)

    def test_lost_publish_remains_recoverable_and_rollbacks_do_not_publish(self) -> None:
        Product.objects.filter(pk=self.product.pk).update(updated=timezone.now() - timedelta(minutes=10))
        with patch.object(generate_digital_preview_task, "apply_async", side_effect=OSError("offline")):
            with self.captureOnCommitCallbacks(execute=True):
                schedule_digital_preview(self.product.pk, self.product.digital_file_url)
        self.product.refresh_from_db()
        self.assertEqual(self.product.preview_status, "pending")
        with patch.object(generate_digital_preview_task, "apply_async") as publish:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(recover_digital_previews(), 1)
            publish.assert_called_once_with(args=(self.product.pk, self.product.digital_file_url), retry=False)
        with patch.object(generate_digital_preview_task, "apply_async") as publish:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(recover_digital_previews(), 0)
            publish.assert_not_called()

    def test_worker_error_is_sanitized_and_can_be_recovered(self) -> None:
        with patch("utils.digital_previews.create_digital_preview_from_storage", side_effect=OSError("private/internal/secret")):
            result = generate_digital_preview_task.apply(args=(self.product.pk, self.product.digital_file_url), retries=3, throw=False)
        self.assertTrue(result.failed())
        self.product.refresh_from_db()
        self.assertEqual(self.product.preview_status, "pending")
        self.assertNotIn("secret", self.product.preview_error)
