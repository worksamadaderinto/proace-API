# Supply-chain controls

## Review and trust boundaries

GitHub Actions use verified full commit SHAs and read-only default tokens. Checkout credentials are not persisted. Only designated CodeQL and production deployment jobs receive their necessary additional permissions. Workflow release jobs require production-branch pushes and successful checks; review enforcement additionally requires the GitHub settings below; untrusted PR code must never run in `pull_request_target` or a privileged `workflow_run`. Downloads and build execution belong in jobs without production credentials.

Dependency integrity verifies which bytes were selected; it does not establish that those bytes are benign. Review source, new transitive packages, maintainers, install hooks, registry URLs, lockfile changes, auth/payment behavior and unexpected network access. Keep independently reviewed, known-good releases available for rollback. Do not auto-merge updates, silence scanners or force incompatible downgrades to make a release pass.

## Repository settings that still require activation

The files alone do not activate GitHub protections. After publishing the workflow and creating/configuring this repository’s own remote:

1. Confirm the CODEOWNERS account has write access and add a trusted independent reviewer. The initial owner comes from the existing API origin; it is not a fabricated team. Authors cannot approve their own pull requests.
2. Import `.github/production-ruleset.json` for the default production branch. Confirm its required job contexts have run successfully. It requires an independent approving review, code-owner review, dismissal of stale approvals, resolved conversations, passing checks and protection against deletion/force pushes, with no bypass actors. Extend it explicitly if another branch can deploy.
3. Require full Action SHA pins and restrict allowed Actions in GitHub settings. Enable Dependabot/security alerts, secret scanning and push protection where the account plan supports them. `.github/dependabot.yml` opens reviewed Action/dependency updates; install/configure Renovate to maintain Docker/Compose digests from `renovate.json`. No auto-merge is enabled.
4. Protect production environments with independent approval, restrict eligible branches, and bind Vault OIDC roles to the exact repository, environment, workflow/ref and audience. Keep short TTLs and least-privilege policies. These remote settings were not changed locally.
5. Protect maintainer accounts with phishing-resistant MFA/passkeys and restricted recovery credentials. Review access periodically.

## Local verification

Run `python3 scripts/check_supply_chain.py` and `python3 -m unittest discover -s scripts -p 'test_supply_chain.py'`. The policy rejects mutable Action/container references, persisted checkout credentials, unsafe workflow triggers, unreviewed npm sources/links, missing integrity and mutable deployment tooling. Run the full repository verification suite as described in AGENTS.md. Passing local checks is not a hosted workflow or production-release verification.

## Containers and runtime permissions

Container sources are pinned to actual registry/cache/build-record digests. Tags remain readable labels; the digest selects the content. Review updates instead of permanently freezing security patches. OS packages installed from signed distribution repositories are not a fully reproducible OS snapshot. Retain vulnerability scanning and review base-image updates. Runtime images should keep existing non-root users and separate service identities.

Use server-derived authorization and ownership. Keep separate database/media/provider credentials for each purpose, least-privilege Vault policies and recipient-scoped records. Apply outbound allowlists at the hosting/network boundary only after inventorying required destinations (payments, mail, storage, Apple/Google, telemetry and DNS). Host firewall policies, new database roles, live provider permissions and alert routing were not invented or changed by this local patch.

## Python installation

CI bootstrapping/scanners use `tools/ci/requirements.in` and its full SHA-256 lock, including pip and setuptools. Install with `python -m pip install --require-hashes --only-binary=:all: -r tools/ci/requirements.lock`. Pipenv still verifies/synchronizes the existing application lock. The scanner targets the application virtual environment explicitly, rather than accidentally auditing its own tool environment. Executable virtual environments are not restored across workflow trust boundaries.

Production requirement inputs remain readable in `server/requirements*.txt`. Resolve with the pinned pip-tools version under Python 3.12/Linux, retaining the explicit storage pins and desired stream-pool constraint; then use `scripts/hash_python_lock.py` to attach verified PyPI artifact hashes to exact resolved requirements. Do not run resolution with production credentials or mount `.env`/Vault files into the resolver. Regenerate all related locks together, validate strict wheel-only installation and `pip check`, run tests and build only affected consumers. Review marker/platform changes on both supported build architectures.

The legacy `server/compose-dev.yaml` utility container uses a verified image digest and no longer receives the host Docker socket. Run Docker build/administration commands on the host; do not grant a generic development image control of the Docker daemon.

### Updating runtime locks

Resolve the combined `server/requirements.txt` and `server/requirements-storage.txt` inputs with the pinned pip-tools version under Python 3.12/Linux. Keep approved version constraints in the resolver’s temporary work directory; the storage inputs intentionally constrain boto3/botocore together. Resolve stream requirements against that base result. Do not copy local environment files into that directory.

For each exact resolved file, run `python3 scripts/hash_python_lock.py /path/to/resolved.txt server/requirements.lock` (or `requirements-stream.lock`). Bootstrap pip is recorded in `requirements-bootstrap.lock`. CI tooling regenerates with `pip-compile --allow-unsafe --generate-hashes --no-emit-index-url --no-emit-trusted-host --strip-extras -o tools/ci/requirements.lock tools/ci/requirements.in`. Recompute the corresponding `.github/python-lock-inputs.json` SHA-256 entries after reviewing changes. That manifest is a stale-lock check, not an independent signature.

The base production lock already includes the storage SDK at the explicit storage-input versions. `INSTALL_OBJECT_STORAGE` remains accepted for existing Compose configuration compatibility; it no longer installs a second, potentially inconsistent SDK set. `INSTALL_STREAM_POOL=true` adds the separately hashed stream pool and checks dependency consistency. A successful build/pip check is not an application startup or health check.

`.github/actions-allowlist.json` records the reviewed Action identities used by this repository. Adding a new publisher/repository/path requires an explicit policy review even when its reference is a full SHA.

## HostGator image handoff

CI builds separate API and stream images before obtaining production credentials. The image archive binds the commit, image config IDs and actual platform; the release job verifies its SHA-256 and commit before fetching SSH credentials. Host activation verifies platform and loaded IDs, then Compose uses immutable local image IDs with pulling/rebuilding disabled for application services. Production Compose contains no application `build` sections. Database, edge and infrastructure images retain their digest pins and existing Vault/runtime configuration.

Production jobs are serialized across release branches. The host must have a Docker platform matching the selected CI runner, Python 3.8+ and enough temporary storage for the downloaded image payload and existing rollback images. A platform mismatch stops before loading. No private registry URL or new provider credential was invented. Validate this handoff in the actual protected hosting environment before production acceptance.

The `.release/active.json`, `previous.json` and per-artifact manifest receipts record image selection. Existing images/volumes are never pruned. Only the current uploaded staging archive is removed after deployment and health checks succeed; failed handoffs remain available for operator inspection/retry. These receipts and the transferred digest are not independent signed attestations. Backward database migrations and rollback must be reviewed explicitly.

Run `python3 -m unittest discover -s scripts -p 'test_release_artifact.py'`. These tests use mocked SSH/Docker commands and synthetic image payloads; they do not deploy to HostGator.

See [release security](release-security.md) for pinned runner observation, signed archive verification, sole-maintainer ruleset templates and the required hosted activation checks.
