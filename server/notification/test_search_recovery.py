from unittest.mock import patch

from django.db import transaction
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from core.models import User
from notification.models import Notification
from notification.search import schedule_index_notification, schedule_delete_notification, search_notification_ids
from notification.tasks import index_notification_task, delete_notification_task
from product.models import SearchIndexJob
from product.tasks import recover_search_index_jobs


@override_settings(ELASTICSEARCH_ENABLED=True)
class NotificationSearchRecoveryTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="search-recipient@example.test", password="test")
        self.other = User.objects.create_user(email="other-search@example.test", password="test")
        self.note = Notification.objects.create(recipient=self.user, verb="Order update", actor_content_type=ContentType.objects.get_for_model(User), actor_object_id=str(self.user.pk))

    def test_publish_failure_recovers_and_only_affected_recipient_falls_back(self) -> None:
        with patch.object(index_notification_task, "apply_async", side_effect=OSError("offline")):
            with self.captureOnCommitCallbacks(execute=True):
                schedule_index_notification(self.note.pk)
        job = SearchIndexJob.objects.get(target_id=self.note.pk)
        self.assertEqual(job.recipient_id, self.user.pk)
        with patch("notification.search.client") as client:
            client.return_value.search.return_value = {"hits": {"hits": [], "total": {"value": 0}}}
            self.assertIsNone(search_notification_ids("order", self.user.pk))
            client.assert_not_called()
            self.assertEqual(search_notification_ids("order", self.other.pk), {"ids": [], "count": 0})
            self.assertIn({"term": {"recipient_id": self.other.pk}}, client.return_value.search.call_args.kwargs["query"]["bool"]["filter"])
        with patch.object(index_notification_task, "apply_async") as publish:
            self.assertEqual(recover_search_index_jobs(), 1)
        publish.assert_called_once_with(args=(self.note.pk,))

    def test_rollback_removes_intent_and_never_publishes(self) -> None:
        with patch.object(index_notification_task, "apply_async") as publish:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(ValueError):
                    with transaction.atomic():
                        schedule_index_notification(self.note.pk)
                        raise ValueError("rollback")
            publish.assert_not_called()
        self.assertFalse(SearchIndexJob.objects.exists())

    def test_hard_delete_intent_survives_record_and_reconciles_to_delete(self) -> None:
        pk = self.note.pk
        self.note.delete()
        with patch.object(delete_notification_task, "apply_async"):
            schedule_delete_notification(pk, recipient_id=self.user.pk)
        with patch("notification.search.client") as client:
            delete_notification_task.run(pk)
        client.return_value.delete.assert_called_once()
        self.assertEqual(client.return_value.delete.call_args.kwargs["id"], pk)
        self.assertFalse(SearchIndexJob.objects.exists())

    def test_provider_failure_retains_job_until_success(self) -> None:
        with patch.object(index_notification_task, "apply_async"):
            schedule_index_notification(self.note.pk)
        with patch("notification.tasks.index_notification", side_effect=OSError("offline")):
            result = index_notification_task.apply(args=(self.note.pk,), retries=5, throw=False)
        self.assertTrue(result.failed())
        self.assertTrue(SearchIndexJob.objects.filter(target_id=self.note.pk).exists())
        with patch("notification.tasks.index_notification") as index:
            index_notification_task.run(self.note.pk)
        index.assert_called_once_with(self.note.pk, strict=True)
        self.assertFalse(SearchIndexJob.objects.exists())
