"""Isolated release-policy regressions: no real credentials, signatures or deployments."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import release_security as release

ENV = {"GITHUB_REPOSITORY": "fixture/repository", "GITHUB_REF": "refs/heads/main",
       "GITHUB_SHA": "a" * 40, "GITHUB_EVENT_NAME": "push", "GITHUB_REPOSITORY_ID": "123",
       "RUNNER_HARDENING_ENABLED": "true", "VAULT_IDENTITY_REVIEWED": "true"}


def rules():
    return [{"type": value} for value in ("deletion", "non_fast_forward", "required_signatures", "pull_request")] + [{
        "type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": "verify", "integration_id": 1}]}}]


class PolicyTests(unittest.TestCase):
    def test_untrusted_event_ref_and_repository_are_rejected(self):
        for key, value in [("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_REF", "refs/heads/feature"),
                           ("GITHUB_REPOSITORY", "fixture/repository; command"), ("GITHUB_SHA", "latest")]:
            with patch.dict(os.environ, {**ENV, key: value}, clear=True), self.assertRaises(ValueError):
                release.context()

    def test_missing_coverage_blocks_before_network(self):
        for key in ["RUNNER_HARDENING_ENABLED", "VAULT_IDENTITY_REVIEWED"]:
            with patch.dict(os.environ, {**ENV, key: ""}, clear=True), patch.object(release, "github") as api:
                with self.assertRaises(ValueError): release.preflight()
                api.assert_not_called()

    def test_signed_commits_and_strict_app_bound_checks_are_required(self):
        release.verify_rules(rules(), ["verify"])
        for mutate in (lambda rows: rows.pop(2),
                       lambda rows: rows[-1]["parameters"].update(strict_required_status_checks_policy=False),
                       lambda rows: rows[-1]["parameters"]["required_status_checks"][0].update(integration_id=None)):
            rows = rules(); mutate(rows)
            with self.assertRaises(ValueError): release.verify_rules(rows, ["verify"])
        with self.assertRaises(ValueError): release.verify_rules(rules(), ["missing-check"])

    def test_live_policy_rejects_repository_swap_unsigned_commit_or_unprotected_environment(self):
        policy = {"required_checks": ["verify"], "environment": "production"}
        good = [{"id": 123}, rules(), {"commit": {"verification": {"verified": True, "reason": "valid"}}},
                {"deployment_branch_policy": {"protected_branches": True}}]
        with patch.dict(os.environ, ENV, clear=True), patch.object(Path, "read_text", return_value=json.dumps(policy)):
            with patch.object(release, "github", side_effect=good): release.preflight()
            for index, invalid in [(0, {"id": 999}), (2, {"commit": {"verification": {"verified": False}}}),
                                   (3, {"deployment_branch_policy": None})]:
                responses = good.copy(); responses[index] = invalid
                with patch.object(release, "github", side_effect=responses), self.assertRaises(ValueError):
                    release.preflight()

    def test_attestation_verification_binds_identity_commit_branch_and_issuer(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(release.subprocess, "run") as run:
            release.verify_artifact(Path("artifact.tgz"), Path("/locked/gh"))
            args = run.call_args.args[0]
            for flag, expected in [("--repo", "fixture/repository"), ("--source-ref", ENV["GITHUB_REF"]),
                                   ("--source-digest", ENV["GITHUB_SHA"]), ("--signer-digest", ENV["GITHUB_SHA"]),
                                   ("--signer-workflow", "fixture/repository/.github/workflows/ci-cd.yml"),
                                   ("--cert-oidc-issuer", "https://token.actions.githubusercontent.com")]:
                self.assertEqual(args[args.index(flag) + 1], expected)
            self.assertIn("--deny-self-hosted-runners", args)
            run.assert_called_once()

    def test_bad_signature_fails_instead_of_falling_back_to_hash_only(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(release.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "gh")):
            with self.assertRaises(subprocess.CalledProcessError):
                release.verify_artifact(Path("artifact.tgz"), Path("/locked/gh"))

    def test_tampered_cli_is_rejected_before_execution_or_writing(self):
        metadata = {"github_cli": {"url": "https://github.com/cli/cli/releases/download/v1.0.0/gh_1.0.0_linux_amd64.tar.gz", "version": "1.0.0", "sha256": "0" * 64}}
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "read_text", return_value=json.dumps(metadata)), patch.object(release, "urlopen", return_value=io.BytesIO(b"tampered")), patch.object(release.subprocess, "run") as run:
            destination = Path(directory) / "output"
            with self.assertRaises(ValueError): release.install_cli(destination)
            self.assertFalse(destination.exists()); run.assert_not_called()

    def test_authenticated_github_requests_cannot_follow_redirects(self):
        self.assertIsNone(release.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://untrusted.invalid"))

    def test_valid_verifier_archive_extracts_only_the_expected_executable(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as package:
            member = tarfile.TarInfo("gh_1.0.0_linux_amd64/bin/gh")
            member.size = len(b"fixture"); package.addfile(member, io.BytesIO(b"fixture"))
        data = archive.getvalue()
        metadata = {"github_cli": {"url": "https://github.com/cli/cli/releases/download/v1.0.0/gh_1.0.0_linux_amd64.tar.gz", "version": "1.0.0", "sha256": hashlib.sha256(data).hexdigest()}}
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "read_text", return_value=json.dumps(metadata)), patch.object(release, "urlopen", return_value=io.BytesIO(data)), patch.object(release.subprocess, "run", return_value=SimpleNamespace(stdout="gh version 1.0.0 (fixture)")):
            path = release.install_cli(Path(directory))
            self.assertEqual(path.read_bytes(), b"fixture")
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_vault_bindings_reject_wrong_identity_wildcards_policies_and_long_ttl(self):
        role = {"role_type": "jwt", "user_claim": "sub", "bound_audiences": ["proace-deploy"],
                "bound_claims": {"repository": "fixture/repository", "repository_id": "123",
                    "sub": "repo:fixture/repository:environment:production", "ref": "refs/heads/main",
                    "workflow_ref": "fixture/repository/.github/workflows/ci-cd.yml@refs/heads/main"},
                "token_policies": ["api-deploy"], "token_no_default_policy": True,
                "token_ttl": "5m", "token_max_ttl": "10m"}
        args = ("fixture/repository", "123", "production", "refs/heads/main", "api-deploy")
        release.validate_vault_role(role, *args)
        for key, value in [("bound_claims_type", "glob"), ("token_max_ttl", "1h"), ("token_policies", ["root"]), ("bound_audiences", ["other"])]:
            with self.assertRaises(ValueError): release.validate_vault_role({**role, key: value}, *args)
        changed = json.loads(json.dumps(role)); changed["bound_claims"]["repository_id"] = "999"
        with self.assertRaises(ValueError): release.validate_vault_role(changed, *args)

    def test_release_workflow_preserves_verification_before_credentials(self):
        text = (release.ROOT / ".github/workflows/ci-cd.yml").read_text()
        if "  deploy:\n" not in text: return
        deploy = text.split("  deploy:\n", 1)[1]
        self.assertIn("attest-release", deploy.split("steps:", 1)[0])
        self.assertLess(deploy.index("release_security.py preflight"), deploy.index("release_security.py verify-artifact"))
        self.assertLess(deploy.index("release_security.py verify-artifact"), deploy.index("scripts/vault_ci.py"))
        for unpack in ["scripts/unpack_deployment.py", "scripts/release_artifact.py verify"]:
            if unpack in deploy: self.assertLess(deploy.index("release_security.py verify-artifact"), deploy.index(unpack))
        self.assertNotIn("continue-on-error", deploy)


if __name__ == "__main__": unittest.main()
