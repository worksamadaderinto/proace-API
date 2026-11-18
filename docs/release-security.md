# Release security

Each repository is independent. Its workflow defaults to a read-only token,
checks pinned Action identities against `.github/actions-allowlist.json`, and
keeps production credentials out of dependency installation and build jobs.
`.github/security-tools.lock.json` pins the GitHub CLI binary version and SHA-256.
The updater must verify the official release asset metadata before changing it.

## Runner observation

Harden-Runner v2.22.3 is pinned to its verified upstream release commit and is
configured as the first step in each job, in **audit** mode. It runs only when
repository variable `STEPSECURITY_ENABLED` is `true`. Leave the variable unset
until supported coverage is actually provisioned and insights are confirmed.
A private-repository subscription is required. Skipping the action is not
monitoring; production preflight rejects missing activation. Audit mode is not
an outbound allowlist. Collect representative successful workflows, review
required endpoints per job, then separately review and test any block policy.
Do not invent a global allowlist or block provider/CDN traffic blindly.

## Sole-maintainer GitHub rules

The owner currently operates alone. `.github/ruleset.production.json` is a
**disabled template**, not a remotely installed ruleset. It requires signed
commits, pull requests, strict required checks and prevents deletion/force pushes.
It does not require another approving reviewer or claim independent review.
Before enabling it, populate each check's `integration_id` from the actual check
run's GitHub App; null/unbound checks fail release preflight. Protect the actual
production environment with deployment limited to protected branches. Verify
account/repository plan support, permissions and a working signed test commit
before activation. Never rewrite history or fabricate a reviewer.

Use a GitHub-enrolled SSH/GPG signing key for the current gate. It requires
GitHub's commit verification to be `valid`; a local signature alone is
insufficient. Gitsign is not installed or accepted as an automatic substitute:
its maintainers document that GitHub does not recognize its signatures as
verified. Adopting it would require a separate issuer/identity/Rekor verification
policy and a compatible branch-rule design. See the
[upstream Gitsign FAQ](https://github.com/sigstore/gitsign#why-doesnt-github-show-commits-as-verified).
A signed commit is not evidence of code safety.

## Signed handoff

For API, web and mobile, an isolated `attest-release` job uses GitHub's pinned
`actions/attest` to sign provenance for the exact prepared archive. It has OIDC
and attestation permissions but no production environment or Vault credentials.
Deployment waits for signing. Before unpacking the archive or fetching Vault
credentials, it downloads the integrity-locked GitHub CLI and verifies the
artifact against the current repository, workflow, source commit, branch,
GitHub OIDC issuer and SLSA provenance predicate. Self-hosted signer runners are
rejected. Missing, tampered or wrong-identity attestations fail closed; archive
hash/manifest checks remain additional controls. No hash-only fallback exists.

Private GitHub artifact attestations require a supported Enterprise Cloud plan.
If unavailable, signing/release remains blocked. Do not mark it supported or
remove signature verification to make a deployment green. Prepared-runtime
attestations for web/mobile cover the handoff archive; they do not attest the
final remote Vercel or EAS build. Those provider build outputs and signed mobile
binaries require their own provider provenance/acceptance checks.

## Vault identity activation

Role templates retain explicit placeholders until authoritative repository IDs,
environments, branches and workflows are available. An operator must inspect
the **live** JWT auth backend (`bound_issuer` must be GitHub Actions' issuer),
role, least-privilege KV policy and environment restriction. Export a role's
non-secret configuration and validate it with:

```sh
python3 scripts/release_security.py validate-vault-role \
  --role-file /private/tmp/exported-role.json \
  --repository "$REPOSITORY" --repository-id "$REPOSITORY_ID" \
  --environment "$ENVIRONMENT" --ref "$RELEASE_REF" --policy "$DEPLOY_POLICY"
```

This checks exact repository ID/name, environment subject, workflow/ref,
audience, TTLs and policy, and rejects glob bindings. It does not install the
role or prove the backend/policy is live. Complete an authorized positive and
negative identity test before setting `VAULT_IDENTITY_REVIEWED=true` in the
repository variables. That variable is an operator acknowledgment, not remote
Vault introspection or a substitute for binding the actual server-side role.
No Vault administration token is introduced into CI.

## Verification

Run `python3 scripts/check_supply_chain.py` and
`python3 -m unittest discover -s scripts -p 'test_release_security.py'`, plus the
repository's normal suite. Offline regressions cover unsupported activation,
wrong repository, missing signed-commit/strict-check rules, unverified commits,
unprotected environments, tampered verifier downloads, signature failures and
Vault role mismatches. Mocked success is not hosted CI, live Vault or production
release acceptance. The workflow must be pushed and its remote controls
activated and tested by an authorized administrator before release readiness.
