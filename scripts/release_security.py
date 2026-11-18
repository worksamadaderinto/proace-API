#!/usr/bin/env python3
"""Fail-closed release checks; never fetch production credentials or deploy."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parents[1]


def context() -> tuple[str, str, str]:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF", "")
    sha = os.environ.get("GITHUB_SHA", "")
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or ref not in {"refs/heads/main", "refs/heads/master"}
            or not re.fullmatch(r"[a-f0-9]{40}", sha)
            or os.environ.get("GITHUB_EVENT_NAME") != "push"):
        raise ValueError("Release requires a production-branch push context.")
    return repository, ref, sha


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: object, fp: object, code: int, msg: str,
                         headers: object, newurl: str) -> None:
        return None


def github(path: str) -> object:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise ValueError("GitHub read access is required to verify release controls.")
    request = Request("https://api.github.com/" + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("GitHub verification response is too large.")
    return json.loads(raw)


def verify_rules(rules: object, required: list[str]) -> None:
    if not isinstance(rules, list):
        raise ValueError("Active branch rules are unavailable.")
    types = {row.get("type") for row in rules if isinstance(row, dict)}
    if not {"deletion", "non_fast_forward", "required_signatures", "pull_request"} <= types:
        raise ValueError("Branch rules must require signed commits, PRs, and prevent deletion/force pushes.")
    for row in rules:
        if not isinstance(row, dict):
            continue
        if row.get("type") != "required_status_checks":
            continue
        parameters = row.get("parameters", {})
        checks = parameters.get("required_status_checks", [])
        names = {item.get("context") for item in checks
                 if isinstance(item, dict) and isinstance(item.get("integration_id"), int)
                 and item["integration_id"] > 0}
        if set(required) <= names and parameters.get("strict_required_status_checks_policy") is True:
            return
    raise ValueError("Required strict checks must be bound to their expected GitHub App.")


def preflight() -> None:
    repository, ref, sha = context()
    policy = json.loads((ROOT / ".github/release-security.json").read_text())
    if os.environ.get("RUNNER_HARDENING_ENABLED") != "true":
        raise ValueError("Release blocked: supported StepSecurity coverage has not been activated.")
    if os.environ.get("VAULT_IDENTITY_REVIEWED") != "true":
        raise ValueError("Release blocked: an operator must verify the live Vault identity bindings first.")
    identity = github("repos/" + repository)
    if not isinstance(identity, dict) or str(identity.get("id")) != os.environ.get("GITHUB_REPOSITORY_ID"):
        raise ValueError("Repository identity verification failed.")
    rules = github("repos/" + repository + "/rules/branches/" + quote(ref.removeprefix("refs/heads/"), safe=""))
    verify_rules(rules, policy["required_checks"])
    commit = github("repos/" + repository + "/commits/" + sha)
    verification = commit.get("commit", {}).get("verification", {}) if isinstance(commit, dict) else {}
    if verification.get("verified") is not True or verification.get("reason") != "valid":
        raise ValueError("Release commit must have a valid GitHub-verified signature.")
    environment = github("repos/" + repository + "/environments/" + quote(policy["environment"], safe=""))
    branch_policy = environment.get("deployment_branch_policy") if isinstance(environment, dict) else None
    if not isinstance(branch_policy, dict) or branch_policy.get("protected_branches") is not True:
        raise ValueError("Production environment must restrict deployment to protected branches.")
    print("Live GitHub release checks passed; Vault activation acknowledgment is operator-supplied.")


def install_cli(destination: Path) -> Path:
    lock = json.loads((ROOT / ".github/security-tools.lock.json").read_text())["github_cli"]
    version = lock["version"]
    expected_url = f"https://github.com/cli/cli/releases/download/v{version}/gh_{version}_linux_amd64.tar.gz"
    if (not re.fullmatch(r"\d+\.\d+\.\d+", version)
            or lock["url"] != expected_url or not re.fullmatch(r"[a-f0-9]{64}", lock["sha256"])):
        raise ValueError("GitHub CLI lock must identify an exact official release asset.")
    with urlopen(lock["url"], timeout=60) as response:
        archive = response.read(128 * 1024 * 1024 + 1)
    if len(archive) > 128 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != lock["sha256"]:
        raise ValueError("GitHub CLI download failed integrity verification.")
    destination.mkdir(parents=True, exist_ok=True)
    binary = destination / "gh"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        member = package.getmember("gh_" + lock["version"] + "_linux_amd64/bin/gh")
        if not member.isfile() or member.size > 128 * 1024 * 1024:
            raise ValueError("Invalid GitHub CLI archive.")
        stream = package.extractfile(member)
        if stream is None:
            raise ValueError("GitHub CLI executable missing.")
        binary.write_bytes(stream.read())
    binary.chmod(0o700)
    version = subprocess.run([str(binary), "--version"], check=True, capture_output=True, text=True)
    if not version.stdout.startswith("gh version " + lock["version"] + " "):
        raise ValueError("GitHub CLI version mismatch.")
    return binary


def verify_artifact(artifact: Path, cli: Path) -> None:
    repository, ref, sha = context()
    command = [str(cli), "attestation", "verify", str(artifact), "--repo", repository,
               "--signer-workflow", repository + "/.github/workflows/ci-cd.yml",
               "--signer-digest", sha, "--source-digest", sha, "--source-ref", ref,
               "--cert-oidc-issuer", "https://token.actions.githubusercontent.com",
               "--predicate-type", "https://slsa.dev/provenance/v1", "--deny-self-hosted-runners"]
    subprocess.run(command, check=True, capture_output=True, text=True)
    print("Artifact signature, workflow, source commit and branch verified.")


def validate_vault_role(role: dict, repository: str, repository_id: str,
                        environment: str, ref: str, policy: str) -> None:
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[1-9][0-9]*", repository_id)
            or ref not in {"refs/heads/main", "refs/heads/master"}
            or not policy or policy in {"root", "default"}):
        raise ValueError("Use an authoritative repository ID and dedicated deployment policy.")
    expected = {"repository": repository, "repository_id": repository_id,
                "sub": "repo:" + repository + ":environment:" + environment,
                "ref": ref, "workflow_ref": repository + "/.github/workflows/ci-cd.yml@" + ref}
    if (role.get("role_type") != "jwt" or role.get("user_claim") != "sub"
            or role.get("bound_claims_type", "string") != "string"
            or role.get("bound_claims") != expected
            or role.get("bound_audiences") != ["proace-deploy"]
            or role.get("token_policies") != [policy]
            or role.get("token_no_default_policy") is not True
            or role.get("token_ttl") not in ("5m", 300) or role.get("token_max_ttl") not in ("10m", 600)
            or any("REPLACE" in value or "*" in value for value in expected.values())):
        raise ValueError("Vault role must bind the exact repository ID, environment, ref, workflow, audience and least-privilege policy.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "install-cli", "verify-artifact", "validate-vault-role"])
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--role-file", type=Path)
    parser.add_argument("--repository")
    parser.add_argument("--repository-id")
    parser.add_argument("--environment")
    parser.add_argument("--ref")
    parser.add_argument("--policy")
    options = parser.parse_args()
    try:
        if options.command == "preflight":
            preflight()
        elif options.command == "install-cli" and options.directory:
            install_cli(options.directory)
        elif options.command == "verify-artifact" and options.artifact and options.cli:
            verify_artifact(options.artifact, options.cli)
        elif options.command == "validate-vault-role" and all((options.role_file, options.repository, options.repository_id, options.environment, options.ref, options.policy)):
            role = json.loads(options.role_file.read_text())
            validate_vault_role(role.get("data", role), options.repository, options.repository_id,
                                options.environment, options.ref, options.policy)
            print("Exported Vault role matches exact identity constraints; live activation is not implied.")
        else:
            raise ValueError("Missing release verification arguments.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError, tarfile.TarError):
        raise SystemExit("Release security verification failed. Check required controls, trusted identities, signatures and supported service access; no deployment credentials were requested.") from None


if __name__ == "__main__":
    main()
