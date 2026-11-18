#!/usr/bin/env python3
"""Offline release-policy checks; never executes dependencies or loads credentials."""

import json
import hashlib
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def workflow_errors(text: str, allowed: set[str] | None = None) -> list[str]:
    errors = []
    for match in re.finditer(r"uses:\s*([^\s#]+)", text):
        action = match.group(1)
        if action.startswith("./"):
            continue
        if allowed is not None and action.split("@", 1)[0] not in allowed:
            errors.append("Unreviewed Action identity: " + action.split("@", 1)[0])
        if not re.fullmatch(r"[\w./-]+@[a-f0-9]{40}", action):
            errors.append("Actions must use full commit SHAs: " + action)
        if action.startswith("actions/checkout@"):
            block = text[match.end() :].split("\n      - ", 1)[0]
            if not re.search(r"persist-credentials:\s*false", block):
                errors.append("Checkout must not persist credentials")
    if "pull_request_target:" in text or "workflow_run:" in text:
        errors.append("Privileged workflow triggers require a separate security design")
    if "@latest" in text or "permissions: write-all" in text:
        errors.append("Mutable tools or unrestricted token permissions")
    if re.search(r"(?m)^permissions:\n(?!  contents: read\n)", text):
        errors.append("Workflow token defaults must be contents: read only")
    if re.search(
        r"(?m)^permissions:\n(?:  [^\n]+\n)*  (?:id-token|security-events|actions):",
        text,
    ):
        errors.append("Elevated token permissions must be scoped to a job")
    if "  deploy:\n" in text:
        deploy = text.split("  deploy:\n", 1)[1]
        required = ["attest-release", "release_security.py preflight", "release_security.py verify-artifact"]
        if any(item not in deploy for item in required):
            errors.append("Production deployment requires live controls and signed provenance")
        if "continue-on-error" in deploy:
            errors.append("Production security checks must fail closed")
        verification = deploy.find("release_security.py verify-artifact")
        for boundary in ("scripts/unpack_deployment.py", "scripts/release_artifact.py verify", "scripts/vault_ci.py"):
            if boundary in deploy and (verification < 0 or verification > deploy.index(boundary)):
                errors.append("Signature verification must precede unpacking and credential retrieval")
    return errors


def lock_errors(package: dict, lock: dict) -> list[str]:
    errors = []
    for name, entry in lock.get("packages", {}).items():
        if not name:
            continue
        if entry.get("link"):
            errors.append("Unreviewed local dependency link: " + name)
            continue
        if not str(entry.get("resolved", "")).startswith("https://registry.npmjs.org/"):
            errors.append("Unreviewed dependency source: " + name)
        if not re.fullmatch(
            r"sha(?:256|384|512)-[A-Za-z0-9+/=]+", entry.get("integrity", "")
        ):
            errors.append("Dependency lacks a supported integrity hash: " + name)
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        if package.get(field, {}) != lock.get("packages", {}).get("", {}).get(
            field, {}
        ):
            errors.append("Manifest/lockfile disagreement: " + field)
    return errors


def main() -> None:
    errors = []
    allowed = set(json.loads((ROOT / ".github/actions-allowlist.json").read_text()))
    for path in (ROOT / ".github/workflows").glob("*.y*ml"):
        errors.extend(workflow_errors(path.read_text(), allowed))
    for manifest in [ROOT / "package.json", ROOT / "tools/deploy/package.json"]:
        if not manifest.exists():
            continue
        lock = manifest.with_name("package-lock.json")
        if not lock.exists():
            errors.append("Missing committed lockfile: " + str(lock.relative_to(ROOT)))
            continue
        errors.extend(
            lock_errors(json.loads(manifest.read_text()), json.loads(lock.read_text()))
        )
        npmrc = manifest.with_name(".npmrc")
        if (
            not npmrc.exists()
            or "ignore-scripts=true" not in npmrc.read_text().splitlines()
        ):
            errors.append("Dependency lifecycle scripts must be disabled by default")
    for directory, folders, names in os.walk(ROOT):
        folders[:] = [
            name
            for name in folders
            if name
            not in {
                "node_modules",
                ".venv",
                "venv",
                ".git",
                ".next",
                "dist",
                "qa-report",
                "qa-reports",
                "__pycache__",
            }
        ]
        for name in names:
            if name != "Dockerfile" and not (
                "compose" in name and name.endswith((".yaml", ".yml"))
            ):
                continue
            for image in re.findall(
                r"(?m)^\s*(?:FROM|image:)\s+([^\s]+)",
                (Path(directory) / name).read_text(),
            ):
                if image.startswith("${"):
                    continue
                if not re.search(r"@sha256:[a-f0-9]{64}$", image):
                    errors.append(
                        "Container reference requires a verified digest: " + image
                    )
    python_policy = ROOT / ".github/python-lock-inputs.json"
    if python_policy.exists():
        for relative, expected in json.loads(python_policy.read_text()).items():
            path = ROOT / relative
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                errors.append(
                    "Python input/lock changed without paired review: " + relative
                )
        dockerfile = (ROOT / "server/Dockerfile").read_text()
        if "--require-hashes --only-binary=:all:" not in dockerfile:
            errors.append(
                "Runtime installation must verify hashes and reject source builds"
            )
    runtime_compose = ROOT / "deploy/hostgator/runtime.compose.yaml"
    if runtime_compose.exists():
        if re.search(r"(?m)^\s*build:", runtime_compose.read_text()):
            errors.append(
                "Production application images must be built before deployment credentials"
            )
        if "compose build" in (ROOT / "deploy/hostgator/deploy.sh").read_text():
            errors.append("Host deployment must not rebuild source")
    policy = ROOT / ".github/deployment-tool.json"
    if policy.exists():
        expected = json.loads(policy.read_text())
        lock = ROOT / "tools/deploy/package-lock.json"
        if lock.exists():
            actual = json.loads(lock.read_text())["packages"].get(
                "node_modules/" + expected["package"], {}
            )
            if any(
                actual.get(key) != expected[key] for key in ("version", "integrity")
            ):
                errors.append(
                    "Deployment tool differs from its reviewed version/integrity"
                )
    for path in (ROOT / "scripts").glob("deploy_*.py"):
        if "@latest" in path.read_text() or '"npx"' in path.read_text():
            errors.append("Deployment must use prepared, locked local tooling")
    if errors:
        raise SystemExit(
            "Supply-chain policy failed:\n" + "\n".join(sorted(set(errors)))
        )
    print("Supply-chain policy passed (not a malware or backdoor guarantee).")


if __name__ == "__main__":
    main()
