"""Regression checks for release-policy trust boundaries."""

import unittest
from check_supply_chain import workflow_errors, lock_errors


class SupplyChainPolicyTests(unittest.TestCase):
    def test_unknown_action_identity_is_rejected_even_with_a_sha(self) -> None:
        self.assertTrue(
            workflow_errors(
                "- uses: attacker/fixture@" + "a" * 40, {"actions/checkout"}
            )
        )

    def test_mutable_actions_and_credentials_are_rejected(self) -> None:
        self.assertTrue(workflow_errors("- uses: actions/checkout@v4\n"))
        self.assertTrue(workflow_errors("- uses: actions/checkout@" + "a" * 40 + "\n"))
        self.assertFalse(
            workflow_errors(
                "- uses: actions/checkout@"
                + "a" * 40
                + "\n        with:\n          persist-credentials: false\n"
            )
        )

    def test_privileged_pr_triggers_are_rejected(self) -> None:
        self.assertTrue(workflow_errors("on:\n  pull_request_target:\n"))
        self.assertTrue(workflow_errors("permissions: write-all\n"))

    def test_dependency_confusion_and_missing_integrity_are_rejected(self) -> None:
        lock = {
            "packages": {
                "": {"dependencies": {"fixture": "1.0.0"}},
                "node_modules/fixture": {
                    "resolved": "https://attacker.invalid/fixture.tgz"
                },
            }
        }
        self.assertTrue(lock_errors({"dependencies": {"fixture": "1.0.0"}}, lock))
        lock["packages"]["node_modules/fixture"] = {
            "resolved": "https://registry.npmjs.org/fixture/-/fixture-1.0.0.tgz",
            "integrity": "sha512-YWJjZA==",
        }
        self.assertFalse(lock_errors({"dependencies": {"fixture": "1.0.0"}}, lock))
        self.assertTrue(lock_errors({"dependencies": {"fixture": "2.0.0"}}, lock))


if __name__ == "__main__":
    unittest.main()
