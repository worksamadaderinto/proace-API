# Django API deployment

HostGator uses Vault-backed runtime files and GitHub OIDC for deployment credentials through the protected production environment. It deploys only API and its edge.

The executable workflow is `.github/workflows/ci-cd.yml` at this repository root. Pull requests run checks without production credentials. Pushes to main/master can deploy only after verification succeeds, through protected GitHub environments. Configure environment approvals and secrets in the destination GitHub repository; local files do not provision remote secrets. No production deployment was performed during migration.

For local development, rebuild `docker compose -f server/compose.yaml up -d --build --no-deps api` from the root; add notification-worker, notification-stream and celery-beat when shared code affects them. Apply migrations and verify health.

Do not print secret values or upload environment/signing files as artifacts. Keep production backend origins, CORS/CSRF settings, OAuth callbacks, Stripe webhooks and mobile verified links consistent with the actual deployment URLs.

## HostGator configuration

Follow [Vault setup and migration](vault.md) before enabling production deployments. Set only the nonsecret GitHub environment variables `VAULT_ADDR` and `VAULT_ROLE`. The deployment job exchanges GitHub OIDC for the exact `api-deploy` KV document; application service secrets never enter Actions. Local development continues to use `server/.env` and `server/compose.yaml`.

Production uses `deploy/hostgator/runtime.compose.yaml` with `--env-file /dev/null`. Public configuration lives at `/etc/proace/config.json`; secrets come from Vault Agent on `/run/proace-vault/api`. API DNS points to HostGator; web DNS stays on Vercel. Set Vercel's public/nonsecret server routing `API_URL` and stream origin to the actual API host. No seed, volume deletion, or secret generation occurs during releases.

## Independent repository checkout

GitHub runs `.github/workflows/ci-cd.yml` from this repository root. No parent workflow or sibling source checkout is required. Configure secrets/environment protection in this repository on GitHub. No production deployment or remote push was performed during the split.


## Password hashing and pepper operations

New passwords use `core.hashers.PepperedArgon2PasswordHasher`: HMAC-SHA256 of the
UTF-8 password with a separate 256-bit secret, then Argon2id (64 MiB, 3 passes,
parallelism 1) with a fresh random salt. This is server-only; web/mobile payloads
are unchanged. `argon2p` identifies the versioned format, which fits the existing
128-character password column. PBKDF2 SHA256/SHA1 and unpeppered Argon2 remain
verification-only fallbacks. Django upgrades a legacy/older-key hash on successful
password authentication; wrong passwords do not change it. Existing password
choices, technical-admin credentials and social-only unusable passwords are
preserved. No bulk reset, destructive seed or data migration is performed. Offline
hashes cannot be converted to this direct-password format without the password.
Inactive users retain old hashes until login or a normal verified password reset.
Representation changes can invalidate other password-derived Django sessions.

Required server secrets: `PASSWORD_PEPPER_ID=v1` and `PASSWORD_PEPPER_KEYS`, a JSON
object mapping IDs (1–8 letters/digits/underscore/hyphen) to 64 lowercase hex
characters each. Generate each key using `secrets.token_hex(32)` and inject it via
a secret manager; never use Django SECRET_KEY, client public env, source control,
logs, or database rows. `.env.example` contains only an intentionally invalid
placeholder. Local server/.env has one generated key; preserve and securely back
it up separately from database backups. All API/worker/stream/scheduler processes
must receive the same keyring. Missing or invalid configuration fails startup.
Do not generate keys during builds/startup or overwrite them during redeploys.
Production keeps `PASSWORD_PEPPER_KEYS` in Vault and the selected
`PASSWORD_PEPPER_ID` in public runtime configuration; see [Vault operations](vault.md).

For planned rotation, add a new ID/key, retain all old keys, then select the new
ID and recreate all consumers together. Successful login upgrades old-key hashes.
Do not remove an old key while any stored hash still references it; those users
would need a verified password reset. A compromised key requires an incident
response and forced password reset for affected users rather than relying on
lazy rotation. Never reuse an ID for a different secret. Rollbacks must retain
the peppered hasher, dependency and keyring; older code cannot verify new hashes.
Benchmark authentication concurrency on production hardware before rollout:
64 MiB is required per concurrent hash, with existing login throttles retained.

References: [Django password upgrades](https://docs.djangoproject.com/en/5.2/topics/auth/passwords/)
and [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).

Deployment trust boundaries and required repository/provider activation are documented in [supply-chain controls](supply-chain.md). Never fetch mutable CLI tools during a credentialed release.

HostGator now consumes the verified CI API/stream image handoff described in [supply-chain controls](supply-chain.md). Do not rebuild application source on the credentialed host; production Compose requires the verified active image IDs. Existing Vault runtime files, service identities and data volumes remain authoritative.

## HostGator application rollback

`scripts/host_release.py` coordinates deployment and recovery on the HostGator
host. CI uploads source to staging; under a host file lock the coordinator copies
it into place, activates the verified image handoff, runs the deployment script
and records a healthy release only after readiness and running-image checks pass.
Direct invocation of `deploy/hostgator/deploy.sh` is a low-level operation and does
not create rollback evidence; use the coordinator for releases.

The ignored `.release/healthy.json` records the last successful application image
IDs, migration-state fingerprint and public runtime configuration fingerprint.
`.release/rollback.json` retains the preceding successful release. A failed
release targets the last healthy release; after a successful release an explicit
rollback targets the preceding healthy release. `.release/previous.json` alone
is not proof that a version was healthy. Retain both successful releases' Docker
images and release records; do not run image pruning that removes them. No image
is downloaded or rebuilt during rollback.

From the deployment checkout **on the HostGator host**:

```sh
# Read-only compatibility/image preflight (no service replacement).
python3 scripts/host_release.py rollback

# Restore the retained application release and verify its health.
python3 scripts/host_release.py rollback --apply
```

Rollback restores API, notification/media workers, notification stream and beat
using immutable image IDs, refreshes static assets, waits for the application
services/Nginx and checks the public HTTPS readiness endpoint. It does not restore
old source/configuration files, provider credentials, databases or media volumes,
run migrations backward, rebuild images or delete orders. A failed rollback does
not mark the target healthy; service replacement may already have started, so
inspect actual containers before attempting another operation.

A deployment failure automatically attempts this same recovery only when the
recorded migration state and runtime configuration match. A schema/configuration
change, missing retained image or absent healthy baseline blocks automatic
recovery. The deployment job remains failed even if recovery succeeds. This is
health-based recovery, not detection of every functional regression.

If migrations/configuration changed, first review old-image compatibility with
the **current** database, secrets (including the full password pepper keyring),
queue payloads, search index and public configuration. After that review, an
operator can explicitly assert compatibility:

```sh
python3 scripts/host_release.py rollback --reviewed-compatibility
python3 scripts/host_release.py rollback --apply --reviewed-compatibility
```

The override never rewinds data and is never used by automatic recovery. Prefer
expand/contract migrations and forward fixes for incompatible changes. Keep
separately verified backups and a tested restore procedure; application rollback
is not database recovery and does not recover deleted data.

Existing installations need one verified healthy release before automatic
recovery is available. If the currently running image identities match its active
manifest, bootstrap the baseline with `python3 scripts/host_release.py mark-healthy`.
The command verifies images, running containers and public readiness before
recording the baseline. A first-ever deployment has no previous release to restore.

Offline regression tests exercise release selection, immutable-ID checks, locks,
compatibility refusal, health failure and deployment recovery. No live HostGator
rollback drill is implied; rehearse a deliberately failing deployment in staging
and verify orders/uploads remain intact before production use.

### Schema changes and the maintenance window

Host releases stop the existing API, stream, scheduler, notification worker, media worker and Nginx before applying migrations. Docker allows 60 seconds for graceful shutdown before stopping remaining processes. Database, cache, object storage and persistent volumes stay online. This deliberately creates a maintenance window and prevents previous-release code from handling requests or queued work against a changing schema. New application services start only after migration and static collection succeed. A failed migration leaves them stopped unless the release coordinator can establish that retained-image recovery is compatible; review migration state before overriding recovery safeguards. Rehearse this behavior with staging data and backups before production.

Scheduled database backups resolve the active verified API and stream image IDs from host release metadata so Compose can load the runtime manifest. Run the backup from a deployed checkout with its release metadata available.
