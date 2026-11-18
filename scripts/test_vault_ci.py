"""Offline checks: workload authentication, revocation and temporary-file security."""

import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "vault_ci", Path(__file__).with_name("vault_ci.py")
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VaultCredentialTests(unittest.TestCase):
    def test_https_required(self) -> None:
        for value in [
            "http://vault.test",
            "https://user:password@vault.test",
            "https://vault.test/#fragment",
        ]:
            with self.assertRaises(ValueError):
                MODULE.https_address(value)

    def test_no_redirect(self) -> None:
        self.assertIsNone(
            MODULE.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://evil.test"
            )
        )

    def test_scope_rejected_before_network(self) -> None:
        for path in [
            "kv/data/other",
            "kv/data/proace/production/../api",
            "kv/data/proace/production/api?version=1",
        ]:
            with self.assertRaises(ValueError):
                MODULE.fetch("https://vault.test", "role", path, Path("unused"))

    def test_workload_token_is_revoked_and_not_exported(self) -> None:
        responses = [
            {"value": "fake-oidc"},
            {"auth": {"client_token": "fake-vault-token"}},
            {"data": {"data": {"DEPLOY_TOKEN": "fake-deploy-token"}}},
            {},
        ]
        environment = {
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.test/token?existing=1",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fake-request-token",
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, environment),
            patch.object(MODULE, "request_json", side_effect=responses) as request,
        ):
            before = dict(os.environ)
            output = Path(directory) / "private" / "credentials.json"
            MODULE.fetch(
                "https://vault.test",
                "web",
                "kv/data/proace/production/web-deploy",
                output,
            )
            self.assertEqual(
                json.loads(output.read_text()), {"DEPLOY_TOKEN": "fake-deploy-token"}
            )
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
            self.assertEqual(dict(os.environ), before)
            self.assertIn("audience=proace-deploy", request.call_args_list[0].args[0])
            self.assertEqual(
                request.call_args_list[-1].args[0],
                "https://vault.test/v1/auth/token/revoke-self",
            )

    def test_revoke_even_when_read_fails(self) -> None:
        environment = {
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.test/token",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fake-request-token",
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, environment),
            patch.object(
                MODULE,
                "request_json",
                side_effect=[
                    {"value": "fake-oidc"},
                    {"auth": {"client_token": "fake-vault-token"}},
                    ValueError("unavailable"),
                    {},
                ],
            ) as request,
        ):
            output = Path(directory) / "credentials.json"
            with self.assertRaises(ValueError):
                MODULE.fetch(
                    "https://vault.test",
                    "web",
                    "kv/data/proace/production/web-deploy",
                    output,
                )
            self.assertFalse(output.exists())
            self.assertEqual(request.call_count, 4)


if __name__ == "__main__":
    unittest.main()
