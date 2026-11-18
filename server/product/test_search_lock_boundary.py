from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless
from uuid import uuid4

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from product.models import SearchIndexJob
from product.tasks import _execute_intent


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(ELASTICSEARCH_ENABLED=True)
class SearchLockBoundaryTests(TransactionTestCase):
    def test_request_intent_refresh_does_not_wait_for_worker_network_call(self) -> None:
        job = SearchIndexJob.objects.create(action=SearchIndexJob.INDEX_PRODUCT, target_id=17)
        revision = uuid4()
        entered, release = Event(), Event()

        def slow_provider(target_id: int, *, strict: bool) -> None:
            self.assertEqual(target_id, 17)
            self.assertTrue(strict)
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test synchronization failed")

        def work() -> None:
            close_old_connections()
            try:
                _execute_intent(SearchIndexJob.INDEX_PRODUCT, 17, slow_provider)
            finally:
                connections.close_all()

        def refresh() -> int:
            close_old_connections()
            try:
                return SearchIndexJob.objects.filter(pk=job.pk).update(revision=revision)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as workers:
            running = workers.submit(work)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(workers.submit(refresh).result(timeout=3), 1)
                self.assertFalse(release.is_set())
            finally:
                release.set()
            running.result(timeout=5)
        self.assertEqual(SearchIndexJob.objects.get(pk=job.pk).revision, revision)
