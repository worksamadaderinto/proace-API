# ProAce Django API

Eligible unshipped physical orders can be cancelled within 24 hours of placement.

Partner merchants activate by email invitation. New stores inherit Partner status and are exempt from ProAce commission. See [commerce and onboarding policy](docs/backend.md).


Merchant refund search and serialization share the same store-scoped historical
item projection. See [architecture](docs/architecture.md).

Dashboard periods accept `days=all` as well as 1–365 days; financial projections are invalidated after authoritative changes. Native notification SSE must enter through Nginx (local port 8080), which routes it to the restricted stream service with buffering disabled. See [infrastructure](docs/infrastructure.md).

This is an independent Git repository in the optional ProAce International workspace.

- [Search execution and timing](docs/backend.md#search-cache-and-media)
- [Request and worker performance](docs/backend.md#request-and-worker-performance)
- [Durable provider operations and transport reuse](docs/backend.md#durable-provider-operations-and-transport-reuse)
- [Sustained profiling and treasury reads](docs/backend.md#sustained-profiling-and-treasury-read-scope)
- [Shared treasury dashboard reports](docs/backend.md#shared-treasury-dashboard-reports)
- [Catalog hydration and exact search counts](docs/backend.md#catalog-hydration-and-exact-tag-search-counts)
- [Setup and architecture](docs/README.md)
- [Input validation and API boundaries](docs/backend.md#api-and-authentication)
- [Deployment and Vault](docs/deployment.md)
- [Agent rules](AGENTS.md)

Verification: `.venv/bin/python -m pytest server/` from this repository root.

HostGator uses Vault-backed runtime files and GitHub OIDC for deployment credentials through the protected production environment. It deploys only API and its edge.

Commands in older detailed documentation are relative to the API repository root unless marked otherwise.

The [documentation index](docs/README.md) groups API setup, architecture, security, commerce integrations and operations. Web UI conventions are maintained in the web component.

## Repository ownership

This repository owns `.github/workflows/ci-cd.yml`. Configure its own GitHub remote, protected deployment environment and Vault workload role. Parent workspace files do not execute its pipeline. Existing Git history is preserved; no commit or remote push was performed for the split.


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
`PASSWORD_PEPPER_ID` in public runtime configuration; see [Vault operations](docs/vault.md).

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


API CI verifies `server/Pipfile.lock` against `server/Pipfile` with `pipenv verify`, then installs pinned packages with `pipenv sync --dev` from `server/`. The virtualenv cache follows the lockfile hash. After changing Pipfile, regenerate the lock with modern Pipenv using Python 3.11 and commit both files; never resolve a lockfile conflict by retaining conflict markers. Docker uses server/requirements.txt separately; keep shared dependency constraints aligned when changing them.

The dependency-lock regression checks every runtime requirement against the pinned lock, so CI catches supported-version drift as well as malformed lockfiles.

Production secrets use [Vault workload identity and runtime files](docs/vault.md); local `.env` remains for development. Provision Vault before enabling the changed release pipeline.

The backoffice store response includes optional store branding; see [backend contracts](docs/backend.md).


Product onboarding requires at least one image, including drafts, digital and partner listings; see [backend contracts](docs/backend.md).

Customer reviews require a funded, non-disputed purchase whose existing escrow deadline has passed. Physical purchases require recorded delivery followed by the configured refund window; digital purchases follow the existing sale-based escrow clock. The API enforces this for creation and editing; review deletion remains available under existing ownership and moderation permissions.

Product onboarding supports atomic cover/content/variant uploads, protected artwork previews and server-enforced variant compatibility; [backend contracts](docs/backend.md) describe limits and platform-controlled labels.

Review deletion policy: [Review moderation](docs/review-moderation.md).

Active bug-audit findings use only reusable `Critical.md`, `High.md`, `Medium.md` and `Low.md` files in each repository’s ignored `qa-report/bug-audit/` directory. Keep discovery timestamps on issue headings; remove verified fixed sections and retain no separate bug-audit artifacts.

Backoffice catalog thumbnails use the existing store-scoped audit response; see [product image handling](docs/backend.md#backoffice-catalog-thumbnails).

[Resumable digital transfers](docs/digital-transfers.md) describes the transfer limits, recovery behavior and verification boundaries.

Order status action contracts: [status editing](docs/backend.md).

Shipment status updates preserve server-derived transitions, including pending tracking corrections. Mobile requires tracking and explicit verified replacement of existing numbers.

Supply-chain review, pinned tooling, verification and GitHub activation steps: [supply-chain controls](docs/supply-chain.md).

Form input validation and the ten-minute password-reset policy are documented in [docs/access-control.md](docs/access-control.md).

Bank verification configuration and service import compatibility are documented in [docs/backend.md](docs/backend.md#bank-verification-service-imports-and-configuration).

Staff and pending store-team invitations expire after seven days; see [invitation expiry](docs/access-control.md#invitation-expiry).

Structured form transport and input validation contracts are described in [docs/access-control.md](docs/access-control.md).

Repeatable isolated purchase/refund and opt-in Stripe sandbox checks are documented in [getting started](docs/getting-started.md#purchaserefund-acceptance-checks).

[Release identity, signing and runner controls](docs/release-security.md) documents enforcement and hosted activation requirements.
Order details expose file links only once the order is eligible for download. Purchased-file inline previews and their format/access limits are described in [digital transfers](docs/digital-transfers.md#purchased-file-previews).

- [Compact catalog cards](docs/backend.md#compact-catalog-cards)

- [Notification counter queries](docs/backend.md#notification-counter-queries)

Store settings use an atomic, permission-scoped API update; see [architecture](docs/architecture.md).

[Backoffice selection, tax pause and account closure](docs/bulk-actions.md) explains selected-record actions, role safeguards and closure behavior.

Apple/Google in-app billing is intentionally excluded; its compatibility endpoints remain disabled and cannot grant orders or digital access. See [backend contracts](docs/backend.md).

HostGator deployments retain healthy application releases and support guarded
manual rollback plus automatic recovery when migration/configuration fingerprints
match. See [deployment and rollback](docs/deployment.md#hostgator-application-rollback).
Database rollback is never automatic; a staging recovery drill is still required.

Store previews use shared backoffice roles, and purchase attribution prefers
immutable order snapshots. Hosted checkout retries resume the original open session. Wallet top-up cancellation requires its own session or funding reference. Checkout confirmation validates its session reference
before provider I/O. See [backend contracts](docs/backend.md#store-previews-and-purchase-attribution).

Item-return refunds exclude original delivery and shipping tax; legacy itemless returns use the same exclusion, while eligible pre-shipment cancellations retain a full refund. See [backend refund contracts](docs/backend.md).

Host releases quiesce application processes before schema changes; plan the documented [maintenance window](docs/deployment.md#schema-changes-and-the-maintenance-window) and staging recovery drill.

Refund eligibility uses each purchased store’s recorded delivery for both numeric
and string snapshot store IDs. See [refund return windows](docs/backend.md#refund-return-windows).

Return-window validation uses each purchased store’s fulfillment delivery even when historical snapshots encode its identifier as text; see [backend policy behavior](docs/backend.md).

See [backend behavior](docs/backend.md) for merchant order thumbnails and their store-scoped fallback.

The [backend guide](docs/backend.md) describes the treasury report’s escrow checks.

See the [deployment guide](docs/deployment.md) for the scheduled database backup prerequisites.
