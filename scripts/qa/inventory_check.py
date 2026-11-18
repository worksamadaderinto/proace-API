#!/usr/bin/env python3
"""Run checkout contention in a unique PostgreSQL test DB via the local API container."""

from __future__ import annotations

import datetime
import subprocess

from utils import REPO_ROOT, get_report_dir

CONTAINER_TEST = r"""
import os
import uuid
import xml.etree.ElementTree as ET
from types import TracebackType
from unittest import TestCase, TextTestRunner, TextTestResult
from unittest.mock import MagicMock, patch

os.environ["DJANGO_SETTINGS_MODULE"] = "codematics.test_settings"
from django.conf import settings
settings.DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.environ["POSTGRES_DB"],
    "USER": os.environ["POSTGRES_USER"],
    "PASSWORD": os.environ["POSTGRES_PASSWORD"],
    "HOST": os.environ["POSTGRES_HOST"],
    "PORT": os.environ["POSTGRES_PORT"],
    "TEST": {"NAME": "test_inventory_" + uuid.uuid4().hex[:16]},
}}
import django
django.setup()
from django.test.runner import DiscoverRunner

suite_xml = ET.Element("testsuite", name="isolated_inventory_checkout")
class Result(TextTestResult):
    def startTest(self, test: TestCase) -> None:
        super().startTest(test)
        self.case = ET.SubElement(suite_xml, "testcase", {
            "name": test._testMethodName,
            "classname": test.__class__.__module__ + "." + test.__class__.__name__,
        })
    def addFailure(self, test: TestCase, err: tuple[type[BaseException], BaseException, TracebackType]) -> None:
        super().addFailure(test, err)
        ET.SubElement(self.case, "failure").text = self._exc_info_to_string(err, test)
    def addError(self, test: TestCase, err: tuple[type[BaseException], BaseException, TracebackType]) -> None:
        super().addError(test, err)
        ET.SubElement(self.case, "error").text = self._exc_info_to_string(err, test)
    def addSkip(self, test: TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        ET.SubElement(self.case, "skipped", message=reason)

class Runner(TextTestRunner):
    resultclass = Result

runner = DiscoverRunner(interactive=False, verbosity=1)
runner.test_runner = Runner
# Redis Pub/Sub channels are shared across logical Redis databases. Test user
# IDs must never publish fixture events into the running application's inboxes.
realtime_broker = MagicMock()
with patch("notification.realtime.broker", return_value=realtime_broker):
    failed = runner.run_tests(["payment.test_inventory_concurrency"])
print("ISOLATED_REALTIME_PUBLISH_CALLS=" + str(realtime_broker.publish.call_count))
print("INVENTORY_XML=" + ET.tostring(suite_xml, encoding="unicode"))
raise SystemExit(bool(failed))
"""


def main() -> None:
    command = [
        "docker",
        "compose",
        "-f",
        str(REPO_ROOT / "server/compose.yaml"),
        "exec",
        "-T",
        "api",
        "python",
        "-",
    ]
    result = subprocess.run(
        command,
        input=CONTAINER_TEST,
        text=True,
        capture_output=True,
        timeout=180,
        cwd=REPO_ROOT,
    )
    marker = "INVENTORY_XML="
    if marker not in result.stdout:
        print("Isolated inventory test could not complete.")
        print(result.stderr[-4000:])
        raise SystemExit(1)
    xml = result.stdout.split(marker, 1)[1].strip()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    path = get_report_dir("stress") / f"inventory-check-{timestamp}.xml"
    path.write_text(xml, encoding="utf-8")
    print(result.stderr[-4000:])
    print(f"Inventory JUnit evidence: {path}")
    print(f"Use INVENTORY_TEST_JUNIT={path} npm run test:stress")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
