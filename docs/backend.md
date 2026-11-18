# Backend

The backend is a Django REST Framework modular monolith. It owns authentication,
permissions, catalog eligibility, pricing, tax, stock, payments, and financial
records. Frontend values are inputs to validate, not authoritative business state.

Native store billing has a deliberately disabled contract in
`server/payment/mobile_billing.py`: customer configuration reports disabled,
and verification returns 503 without orders, entitlements, or token logging.
Apple/Google in-app billing is intentionally excluded by product decision.
These compatibility endpoints must never enable client-asserted purchases.

Native guest bags use customer-only `POST /api/v1/cart/merge/` with a UUID
`merge_key` and validated product/variant quantities. A per-user receipt and
transactional locks make retries idempotent; a rejected line rolls back the
entire import. Prices, ownership, stock and eligibility remain server-derived.
`GET /api/v1/wallet/topup-session/` returns only the authenticated customer's
pending funding sessions for recovery. Review listings filter by search, dates
and rating before counting/pagination while retaining the customer's own review.

Push-device registration accepts `platform` (`web`, `ios`, or `android`),
defaulting to `web` for existing browser clients. Delivery separates native
display notifications from browser data-only messages, preserving recipient
isolation, token retry state and delivery leases. Firebase/APNs configuration
and signed-device delivery testing remain deployment requirements.

## Entry points and module map

`server/manage.py` selects `codematics.storefront_settings`; the integrated API
routes are declared in `server/storefront/urls.py`. The base settings package
also supports specialized test and notification-stream configurations.

| Module | Responsibility |
| --- | --- |
| `core/` | Users, addresses, shopper wallets, reviews, saved items, and shared filters |
| `product/` | Product models, catalog search, indexing, and local picture search |
| `store/` | Merchant stores, team permissions, catalog management, earnings, and payouts |
| `cart/` | Customer carts and selected product variants |
| `payment/` | Checkout, orders, taxes, refunds, funding, accounting, and fulfillment |
| `notification/` | Recipient inboxes, delivery outbox, email, push, and SSE |
| `affiliates/` | Referral accounting and ClickBank outbound attribution |
| `staff/` | Platform operations and backoffice endpoints |
| `storefront/` | Integrated serializers, views, and API composition |
| `observability/` | Health probes, metrics, structured logs, and tracing |

Important payment modules are `services.py` for checkout/order operations,
`accounting.py` for tender and allocation calculations, `fulfillment.py` for
store-level shipment state, `funding.py` for wallet deposits,
`checkout_recovery.py` for abandoned/compensated checkout, and
`provider_events.py` for provider adjustments.

## API and authentication

The frontend proxy forwards browser calls from `/api/*` to `/api/v1/*`.
Direct API consumers can use supported bearer authentication. Browser tokens
must stay in HTTP-only cookies, not local/session storage.

Views derive the acting user from authentication. Store access is resolved
through ownership and active team membership. Role-sensitive operations require
their actual permission, not merely a client-supplied store or owner ID.
See [access control](access-control.md) for the role matrix.

Shared request parsers validate text, nesting and finite numeric values without
rewriting legitimate punctuation, Unicode or password whitespace. JSON requires
an object; multipart upload metadata uses `core.fields.ValidatedJSONField` to
repeat validation after embedded JSON is decoded. Serializer field and
cross-field rules remain necessary: parsing does not establish identity,
ownership, payment eligibility or safe output rendering.

Bank payouts resolve a verified saved bank account owned by the acting user;
responses expose masked account suffixes rather than full account identifiers.
Split-tender wallet amounts use the same decimal validation as wallet checkout.
Backoffice coupon writes share the storefront coupon constraints. Local tax
rules validate supported country/state and postal-prefix scope explicitly.

Serializers validate API inputs and normalize error messages. Financial/date
filters run before pagination so counts and results describe the same scope.
Public errors omit SQL, paths, raw provider exceptions, credentials, and traces.
Internal logs retain diagnostic detail.

OpenAPI endpoints are `/api/schema/`, `/api/docs/`, and `/api/redoc/`.
Use the route declarations and generated schema for exact methods and payloads.

## Catalog and merchant stores

Public eligibility requires a visible, unblocked product in an eligible store.
Scheduled drops use server time. Merchant draft previews require authorized
access; a preview parameter alone never makes drafts public.

Products can have variant-specific stock, price modifiers, SKUs, and images.
An empty cart variant identifies the main product; it is not an arbitrary
fallback for an unknown option. Physical products need shipping measurements;
digital products use protected purchased-file delivery.

Merchant CSV imports use RFC 4180 parsing, including quoted commas, escaped
quotes, and multiline values. Store administrators can manage branding and
settings; catalog and fulfillment roles remain scoped to their work.

Store team invitations support both linked users and pending email recipients.
Membership changes and recipient notices are transactional. Inviting, accepting,
resending, and revoking access remain separate operations. Active memberships
control store navigation, queries, and notification destinations.

### Seller tiers and commissions

| Tier | Gross-sales threshold |
| --- | --- |
| Starter | Below $5,000 |
| Booster | $5,000 |
| Accelerator | $25,000 |
| Power | $100,000 |
| Mega | $500,000 |
| Legendary | $1,000,000 |
| Official Partner | Platform-managed partner classification |

Standard stores pay a 15% platform fee on new native sales. Official and invited Partner stores pay 0%. Existing ledger amounts are retained. Eligible physical orders have a 14-day post-delivery refund window; digital refund exclusions and the separate cancellation policy still apply.

Backoffice invites a new Partner merchant by email. The recipient verifies the invited address and sets a password; only stores created by that account receive the server-assigned Partner flag. Partner stores follow normal verification and payout rules and can publish native and external ClickBank listings. External listings never enter the native cart.

Product onboarding accepts an optional MP4 of up to 4 MB under the public product media prefix, separately from paid digital assets.
Verification and moderation remain independent of a store's sales tier.
Products under moderation and stores with financial history cannot be removed
in ways that erase their retained records.

## Checkout, orders, and fulfillment

Checkout re-reads eligible products, variants, prices, coupons, tax, and stock
from backend records. Inventory changes and financial mutations use database
transactions and row locks. Native carts cannot contain external ClickBank offers.

Stripe supports cards and supported Apple Pay/Google Pay wallets. ProAce Wallet
can pay alone or alongside a Stripe charge. Checkout attempts durably record
wallet reservations and provider sessions so retries can recover without
duplicating a payment or releasing funds while a session can still complete.

Orders preserve item, address, tax, and tender snapshots. Historic order detail
does not substitute today's product price or customer's current address.
Each store has its own fulfillment membership and shipping history; one merchant
cannot expose or mutate another store's shipment.

Physical delivery uses USPS, UPS, FedEx, and DHL Express rates based on destination
and parcel measurements. Military APO/FPO/DPO destinations require USPS.
Digital goods carry no shipping charge and omit physical delivery promises.
See [shipping integrations](shipping-integrations.md).

### Cancellation, returns, and merchant balances

Unshipped orders can be cancelled within 24 hours (the centrally defined `CANCELLATION_WINDOW_HOURS` policy). Accepted cancellation restocks
eligible physical inventory atomically and immediately returns customer store
credit through a refund wallet receipt.

Physical return eligibility runs for seven days after delivery. Physical merchant
proceeds remain in review until seven days after recorded store delivery and no
earlier than the sale hold. Undelivered physical proceeds stay held; digital
proceeds use the seven-day sale hold.

Refunds retain cumulative settled amounts, tender allocation, operator identity,
and stock-return state. Partial refunds do not imply that the entire order is
refunded. Refund and cancellation retries cannot issue the same wallet credit
or restock the same accepted return twice.

Payouts draw only from cleared funds, recheck reservation coverage on approval,
and retain debt or dispute holds. Bank destination values are encrypted at rest,
owner-scoped, masked in responses, and resolved server-side from saved IDs.
Preserve the application secret key or migrate encrypted records before rotation.
A treasury destination records transfer intent; it does not itself execute an
external bank/Stripe transfer.

### Wallet integrity and compensation

Wallet credits and debits require positive, finite, exact-cent amounts within
the database range. Credits also check resulting balance capacity while holding
the wallet lock. Idempotency keys identify prior receipts and reject conflicting
amounts, owners, or transaction directions. Legitimate negative recovery balances
are retained rather than silently reset.

Checkout and funding compensation complete only after a provider refund reports
`succeeded`. Pending, action-required, failed, and unknown results stay recovery
work. Pending create responses are retrieved for current settlement state.
Provider idempotency keys and wallet receipt keys persist across retries.
Split wallet reservations are not released prematurely.

## Destination taxes

`payment.TaxJurisdiction` stores destination rules and ZIP overrides with
five-decimal rate precision. The engine supports Stripe Tax fallback and records
the calculation in immutable `Order.tax_snapshot`, including jurisdiction,
taxable merchandise/shipping, exemptions, and provider context.
Operators manage jurisdictions at `/admin/taxes` and `/api/v1/admin/taxes/`.

## Provider adjustment reconciliation

Apply all migrations before starting API, worker, stream, and scheduler consumers.
Subscribe Stripe webhooks to:

- `checkout.session.completed` and `checkout.session.expired`
- `charge.refunded`
- `refund.created`, `refund.updated`, and `refund.failed`
- `charge.dispute.created`, `charge.dispute.updated`, and `charge.dispute.closed`

Signature verification precedes processing. `ProviderAdjustment` stores unique
refund/dispute references and applied amounts. Order locks serialize callbacks;
current provider state resolves out-of-order updates. Failed reconciliation
returns a retryable response rather than acknowledging unapplied changes.

Refund approval requires settlement. External card refunds adjust tender and
merchant earnings without assuming physical goods returned. Merchant reversals
use merchandise allocation, not refunded tax/shipping. Multi-store allocations
use cent-safe largest-remainder distribution. Disputes hold funds until resolved;
lost disputes reverse earnings and won disputes release holds.

Referral rewards require a fully paid qualifying order and are reversed on
cancellation/refund. Already-spent promotional credit can produce wallet debt.
ClickBank products instead use external vendor checkout and its fulfillment and
60-day satisfaction policy; they never enter native payment/refund flows.

## Notifications and email

Business events persist recipient-scoped inbox and outbox rows transactionally.
Dispatch starts after commit. A Celery worker sends email/push; Beat recovers
pending or abandoned attempts. Store associations and destination links require
the recipient's active access.

Delivery attempts use an incrementing attempt number as a lease fence. Only the
current processing attempt may persist success, failure, or remaining push tokens.
This protects database state when an old worker finishes late. It cannot undo
an external request already in flight or guarantee exactly-once provider delivery.

Templates live in `server/templates/email-templates/`. Payload producers use
actual recipients and authoritative transaction snapshots, omit missing optional
rows, preserve zero amounts, escape user content, and distinguish digital from
physical orders. Credential notices use dedicated protected flows, not generic
status emails. Rendering or queuing a message does not establish inbox delivery.

See [getting started](getting-started.md) for console/Resend setup and
observability (optional sibling/parent workspace reference: `../../monitoring/docs/architecture.md`) for delivery diagnostics.

## Search, cache, and media

Elasticsearch returns candidate IDs/counts; PostgreSQL hydrates current records.
Database fallback keeps catalog reads available when search is disabled or
unusable. Durable indexing jobs support retry/recovery after search outages.
Rebuild indexes using `python server/manage.py rebuild_product_index`.
Schema creation and mapping updates run in indexing/rebuild paths, never in a
customer search request. A missing/unusable index falls back to PostgreSQL.
Request logs split `search_engine` network time from `search_fallback` database
loading/serialization time; correlate them with `X-Request-ID`. Timings contain
no search terms, SQL, credentials or response payloads.

Public catalog snapshots and shard search caching are performance mechanisms,
not business locks. Authenticated/private responses must not be shared across
users. See [performance](getting-started.md#verification) for cache scope and worker budgets.
[Picture search](visual-search.md) documents the separate local inference service.

MinIO provides local S3-compatible media storage with dedicated application
credentials, separate from root/console access. Enabled storage requires its
dependencies; it must not silently fall back to ephemeral application files.
Purchased digital assets remain authorization-checked streams.

## Development seeding

The canonical command is `core/management/commands/populate_fake_data.py`.
The storefront command forwards to it; there are no separate seed aliases.

| Preset | Users | Stores | Products | Orders | Reviews |
| --- | --- | --- | --- | --- | --- |
| small | 25 | 6 | 45 | 90 | 70 |
| medium | 65 | 12 | 150 | 350 | 280 |
| large | 200 | 25 | 500 | 1,500 | 1,100 |
| huge | 500 | 50 | 1,200 | 4,000 | 3,200 |

Use `--users`, `--stores`, `--products`, `--orders`, and `--reviews` to
override volumes. Counts describe generation inputs, not an exact final database
size: known accounts/scenarios may be reused and supporting rows are also added.
At least 11 users and positive overrides are required.

The command is development-only, transactional, and refuses `DEBUG=False`.
It preserves an existing technical-admin password; other known demo accounts
can be refreshed. Password precedence is CLI, configured admin password, then
the documented development fallback. Output never prints that password.

`--clear` deletes commerce data throughout the database, not only tagged fake
rows. It is unnecessary for adding volume. Search indexing runs after commit.
See the [root guide](../README.md#development-data) for the 10,000-record command.

## Verification and operation

```sh
.venv/bin/pytest server/
.venv/bin/python server/manage.py makemigrations --check --dry-run
```

Offline settings isolate cache, email, storage, and broker dependencies.
PostgreSQL locking tests use `codematics.concurrency_test_settings`, which creates
and destroys only `test_proace_audit_47_49`. It rejects that name as the application
database. Do not auto-approve deletion of an unexpectedly existing test database:

```sh
docker compose -f server/compose.yaml exec -T api python manage.py test \
  core.test_wallet_integrity_audit notification.test_delivery_lease_audit \
  payment.test_compensation_settlement_audit payment.test_inventory_concurrency \
  payment.test_audit_regressions.PostgresConcurrencyTests \
  --settings=codematics.concurrency_test_settings
```

For backend changes, apply migrations and rebuild the affected consumers as
described in [infrastructure](infrastructure.md). Verify running health, not only
image build success. Technical-admin password updates synchronize Grafana and
MinIO through `core/auth_sync.py`; external-service failure must not block the
Django password update.


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


API CI verifies `server/Pipfile.lock` against `server/Pipfile` with `pipenv verify`, then installs pinned packages with `pipenv sync --dev` from `server/`. The virtualenv cache follows the lockfile hash. After changing Pipfile, regenerate the lock with modern Pipenv using Python 3.11 and commit both files; never resolve a lockfile conflict by retaining conflict markers. Docker uses server/requirements.txt separately; keep shared dependency constraints aligned when changing them.

`core/test_dependency_lock.py` checks that all runtime requirements are represented in the lock, pinned with hashes, and within their supported ranges. Preserve compatible exact pins when aligning manifests; additional legacy/development packages remain declared in Pipfile.

## Backoffice store branding

`GET /api/v1/admin/staff/stores/` and its store detail route include read-only
`avatar_url`. It is the store profile avatar, not the owner's avatar. Missing
branding returns an empty string. Uploaded branding uses the existing versioned
`/api/stores/{id}/public/images/avatar` route, never a storage path. The first
profile by primary key is authoritative, matching storefront/profile behavior.
The list prefetches store profiles to avoid a query per row. Consumers retain
an icon fallback and existing authenticated preview access for non-public stores.
No database migration or permission expansion is needed for this additive field.


Product creation requires a valid cover reference or an uploaded image, including drafts, digital and ClickBank offers. Multipart writes accept `product` JSON plus optional `cover_image`, `digital_file`, repeated `variant_files` and JSON `variant_indexes`. Each variant file maps to its validated option index. All product/gallery/variant database writes commit together. An uploaded digital image may generate a watermarked public cover; its original stays private. A remote download URL alone does not satisfy the cover requirement. Product photos are at most 5 MB, digital files at most 50 MB and combined product uploads at most 50 MB; store responses expose `product_upload_limit_bytes`.

Merchant responses expose `allowed_variant_asset_types`, and store responses expose `digital_variant_policy`. Images accept image options, audio/video accept either family, and documents/software accept document options. Both initial uploads and later variant-file writes enforce compatibility. Merchant writes cannot set `label`. Store roles are resolved from authenticated ownership/membership and never default an unrelated or anonymous caller to owner.

Customer reviews require a funded, non-disputed purchase whose existing escrow deadline has passed. Physical purchases require recorded delivery followed by the configured refund window; digital purchases follow the existing sale-based escrow clock. The API enforces this for creation and editing; review deletion remains available under existing ownership and moderation permissions.

## Backoffice catalog thumbnails

The authorized store catalog audit response includes a read-only `image_url` from the recorded gallery cover or product image URL. Missing media stays empty; private downloadable content is never substituted. Gallery rows are prefetched for pagination.

## Server-derived order status choices

Backoffice order records expose `allowed_status_transitions` from the API transition rules, excluding unchanged targets, terminal orders and invalid digital/cancellation actions. Store order records expose their own fulfillment choices, including the current status for tracking-only corrections. Clients must not substitute a fixed list when metadata is absent; refresh the record instead. Financial returns still use the refund workflow. The API revalidates mutations under its order lock, so action hints are not authorization or a concurrency guarantee.


## Bank verification service imports and configuration

`core.services` is a Python package: its entry point preserves the wallet service imports, and `core.services.bank_validation` owns bank verification. Account number, routing number, holder or account-type edits require verification of the resulting complete account. Missing credentials or incomplete owner matches never simulate success. New and pre-existing unverified records default to pending verification.

Bank configuration uses the shared runtime configuration reader (ignored local environment files in development; Vault Agent files in production): `USBANK_CLIENT_ID`, `USBANK_CLIENT_SECRET`, `USBANK_API_BASE_URL`, and `USBANK_ENVIRONMENT`. Production requires `production` mode and HTTPS. Missing configuration returns sanitized unavailable guidance; it does not prevent API startup. Tests mock provider responses explicitly and do not establish live provider acceptance.


## Request and worker performance

Catalog fallback search unions narrow text/tag candidate IDs, avoiding wide
DISTINCT sorts and allowing separate substring indexes. PostgreSQL migrations
install pg_trgm and build uppercase GIN indexes concurrently for title, brand,
description, category and tag names. SQLite keeps the same matching semantics
without these PostgreSQL indexes. Index builds need the database owner’s trusted
extension/index privileges; never skip migration failures. Indexes add write and
storage overhead and are not a replacement for query-plan measurement. Search pages select IDs before hydrating store,
specification, image and tag data. Counts and current visibility/price checks
remain authoritative PostgreSQL reads.

Carrier requests share a per-process pool, configured by SHIPPING_QUOTE_WORKERS
and SHIPPING_QUOTE_DEADLINE_SECONDS. Running plus queued work is bounded across
requests; saturation/deadline failures return a retryable 503 without partial
shipping totals. Active synchronous HTTP calls cannot be interrupted safely and
retain capacity until provider timeouts finish. Limits multiply by API process
count and are not a fleet-wide provider quota.

Indexing intents survive broker failures. Workers lock separate execution rows,
so a request can refresh an intent while Elasticsearch is slow. Revision checks
retain newer work. Notification intents retain server-derived recipient IDs;
only affected recipients use SQL search while their intents are pending.

Digital preview source keys are revalidated before publication and failure
updates. Pending/stale processing states recover through the scheduler after a
five-minute grace period. Corrupt/unsupported inputs fail without publishing raw
sources; temporary provider errors retain a sanitized delayed state. Preview and
avatar tasks run on the dedicated media queue via run_media_worker, separately
from notification delivery. Image/inference work remains CPU-bound even when
queued; use worker process and container CPU/RAM limits.

PostgreSQL computes historical order/store counts from immutable snapshots,
with cart fallback for legacy records. SQLite keeps the portable reference path.
Treasury and merchant escrow computations batch legacy cart composition and
aggregate scoped refund adjustments rather than querying each order. Treasury
still evaluates financial clearance policy over sale rows; it is not an
incremental reporting table. Wallet funding recovery expires provider sessions
before acquiring its short revalidation lock. Funding creation and original-payment refund decisions now use durable
claim/provider/settlement phases described below. Other checkout-compensation
and pre-shipment cancellation paths still need separate lock-boundary review.


When upgrading notification workers from the earlier non-durable task format,
drain old tasks before switching versions or rebuild the derived notification
index from authoritative records (`rebuild_notification_index --clean`) during
a controlled maintenance window. New-format intents handle normal retry/recovery;
old queue messages did not contain a persisted intent. This does not remove
notification records from PostgreSQL.


## Durable provider operations and transport reuse

Wallet funding reserves deposit allowance in WalletFundingCreation before calling
Stripe. Its immutable payload, UUID idempotency key and provider-credential
fingerprint survive ambiguous network failures. Linking a returned session to its
receipt is a short user-locked transaction. Pending/review creations consume
allowance until reconciled. Confirmation credits atomically; invalid-payment
compensation calls the provider after persisting refund_pending. Cancellation
also performs provider verification before locking and revalidating its receipt.
The logical funding reservation remains thirty minutes; provider session expiry
has a one-minute clock/network grace to satisfy Stripe’s minimum thirty-minute
expiry, and scheduled recovery confirms expiry before releasing allowance.

Original-payment refunds persist an immutable amount/items/card allocation,
idempotency key and initiating operator in RefundProviderOperation. A five-minute
lease prevents duplicate workers. Stripe I/O holds no order/refund locks;
settlement atomically updates wallet, ledger, inventory and provider reconciliation
records. Competing refund edits, declines and cancellation are blocked by durable
state. Original invoice totals remain immutable. Customer item-based returns
refund selected paid item amounts and item tax, excluding delivery and shipping
tax. Itemless legacy return plans use the same delivery-excluding ceiling. Previously
submitted provider operations keep their immutable amount for reconciliation;
new policy does not rewrite an already-issued provider request. Eligible
pre-shipment cancellations retain their separate full-settlement refund path.
Webhooks can win the race and settlement recognizes their completed result.
Confirmed provider results survive local rollback and retry settlement without
issuing another provider refund. Provider-reference fields contain real provider
references only; editability derives from operation state.

Recovery tasks process bounded ready rows. Automatic create replay stops after
twenty-three hours, before Stripe may prune its twenty-four-hour idempotency key;
provider-credential changes also require review. Unknown outcomes keep allowance
or refund claims reserved. Review states require operator reconciliation with the
provider; they must not be cleared blindly or retried under a new key. Verified
terminal failed/canceled refunds can begin a new attempt. A known settled result
can finish local accounting without another external create call.

Carrier OAuth tokens are cached only in process memory, keyed by a salted
credential/account/environment fingerprint, with provider-supplied expiry and a
refresh margin. Missing/invalid expiry is never guessed. Concurrent refreshes
coalesce across threads/event loops; rejected bearer tokens refresh once. No token
or raw credential is logged, persisted or added to a public cache. Synchronous
HTTP uses a shared thread-safe client. Asynchronous carrier calls use a dedicated
process-local event loop/client, avoiding transport reuse across different loops
or forks. Connections are bounded by CARRIER_HTTP_CONNECTIONS; cookies are not
retained across accounts. Clients close during process shutdown.

Request diagnostics split catalog_count, catalog_page_ids, catalog_hydration,
search_engine and provider-reported search_server time. Parent timings include
children and must not be summed. Elasticsearch failures log a bounded error
category/status, not query text or provider payloads.

References: [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests),
[HTTPX client lifecycle](https://www.python-httpx.org/async/).


## Sustained profiling and treasury read scope

Run `.venv/bin/python scripts/qa/load_test.py --url http://127.0.0.1:8000 --concurrency 5 --duration 60`
against an authorized local/staging instance. The runner discovers actual visible
products and records listing/search/detail/category latency, errors, empty-result
coverage, cache status and available application/database Server-Timing metrics.
The verdict evaluates overall and per-scenario p95/p99, so a fast scenario mix cannot hide a slow route. Missing metrics are unavailable, not zero. Detailed provider/count/hydration stages
stay in trusted request-correlated logs. Nested timings overlap: never sum them.
This closed-loop workload includes think time; its concurrency is neither an arrival
rate nor proof of a particular user capacity. Run without competing builds/tests
and collect host/container resource metrics before drawing capacity conclusions.

Worker scaling benchmarks demonstrate that Gunicorn worker processes must balance CPU parallelism against host contention: on an 8-core host running co-located PostgreSQL, Redis, Elasticsearch and monitoring, 4 workers (`WEB_CONCURRENCY=4`) with 16 threads (`GUNICORN_THREADS=16`) achieved optimal throughput (140.23 RPS at concurrency 20, 0% errors) and kept maximum pool connections safely within PostgreSQL limits (4 * 16 = 64 < 100 max_connections). Increasing to 6 workers caused severe context switching and dropped throughput by 62% (53.43 RPS, p50 178 ms).

Latest completed runner reports live in `qa-report/performance/{load,stress,volume}/`.
Each runner publishes latest Markdown/JSON before pruning only its own dated output.
Unrelated reports and fixtures are preserved.

Treasury selects only the ledger, official-store and order clearance fields required
for authoritative totals; it omits buyer/contact/provider data and unrelated snapshots.
Refund offsets, disputes, delivery-based escrow and legacy cart handling remain live.
Authoritative calculation uses a consolidated single-pass PostgreSQL query
(`historical_treasury_report`) with materialized CTEs to compute cleared and pending
amounts in a single query, with a compatible ORM loop fallback for SQLite environments.
Financial withdrawal authorization (`withdraw_commission`) strictly executes this live
calculation and never uses cached totals or summaries.

`TreasuryReportSummary` provides a periodic reconciled baseline for settled historical
ledger entries older than the return hold window. Dirty-row signal listeners on `Order`,
`OrderFulfillment`, `StoreEarningsLedger`, and `Store` invalidate summaries whenever an
older transaction or refund is modified.


## Shared treasury dashboard reports

`treasury_report` computes cleared and pending-inclusive totals in one authoritative
ledger pass. Dashboard and commission-wallet display reads share the same pair of
totals for sixty seconds via revision-scoped caching (`read_report`). Store/ledger/order/fulfillment
writes rotate a cache revision before and after commit. A scan that races invalidation cannot
publish its result under the new revision; older revision keys expire naturally.
Transaction-local reads bypass caching, so uncommitted or rolled-back amounts are
never published. Bulk writes that bypass signals remain bounded by display-cache
expiry; time-based escrow is recalculated after expiry even without a model write.

`treasury_earnings` remains an uncached authoritative calculation for withdrawal
eligibility. Neither dashboard snapshots nor Redis availability are evidence that
funds are withdrawable. Clearance still uses actual disputes, delivery timestamps,
refund adjustments and immutable/legacy order composition. Fulfillment prefetches
load only the order/store/delivery fields needed for this calculation.
build. A future persisted projection still requires reconciliation and complete
bulk-write/time-based invalidation coverage before changing financial decisions.


## Catalog hydration and exact tag-search counts

Public catalog reads keep the existing response schema used by web and native
clients. ORM hydration defers internal digital source keys, preview diagnostics
and moderation/operator fields that the public serializer does not consume.
Image prefetches load only IDs, product links and image paths. Public descriptions,
variant data, gallery images, tags and specifications remain available; this change
reduces database-to-application loading, not the public JSON response size.

Fallback search unions text-matching product IDs with matching TaggedItem object
IDs directly. The tag branch filters by the authoritative product content type,
so it cannot match tags belonging to another model with the same numeric ID.
Avoiding another product-table join reduces candidate-query work. Exact counts,
visibility, store verification, price/category filters and pagination remain
current database reads; no approximate or cross-tenant count cache is introduced.
Verify actual PostgreSQL plans and response equivalence before claiming latency
improvements. Trigram indexes may be less useful for short/common terms.


## Compact catalog cards

The products listing accepts `view=card` for compact product cards. Default/full listings and individual product details retain their existing fields. Card pages identify themselves with `view: card`; they omit descriptions, galleries, tags, specifications, variants and package dimensions. Card hydration defers those columns, skips tags/specifications and prefetches one primary image per product. Exact counts, filters, pinned products, store visibility and permission checks remain authoritative. Anonymous card/full cache identities are distinct; cached card price and stock are revalidated. Variants are re-read on detail/cart paths instead of being sent with every card.


### Trigram expression alignment

The installed Django PostgreSQL backend compiles icontains to UPPER(column::text) LIKE UPPER(parameter). Migration product.0024 restores uppercase expression GIN indexes after the raw-column change in product.0023. A raw ILIKE benchmark does not establish an index improvement for this ORM path. The forward migration builds replacement indexes concurrently before removing the prior indexes and supports retry after an interrupted build. Applied migration history is preserved.


## Notification counter queries

Recipient-scoped counts use one conditional aggregate; filters use a category aggregate, an overall status aggregate and the recipient-visible store lookup. The budgets remain one and three queries respectively. Regression fixtures verify unread/read/archive totals, active-category counts and cross-recipient/cross-store exclusion as well as query counts. Mobile refresh keys include separate category/status metadata so reduced query work does not leave stale filter counts.

## Store previews and purchase attribution

Unpublished store profiles and branding are readable by their owner and the
shared backoffice roles (staff, business admin and technical admin). Previewing
does not publish or verify the store. Anonymous and unrelated customer requests
remain hidden with a not-found response.

Verified-buyer attribution uses immutable order item snapshots whenever present.
Only historical orders without snapshots may use their ordered cart as a fallback;
cart contents cannot add purchase attribution to a snapshotted order. Review
creation and editing independently enforce funded-purchase eligibility and the
configured post-delivery escrow period.

Checkout confirmation accepts the bounded `PaymentSessionRequestSerializer`
contract before requesting Stripe verification. Malformed shapes, missing/null
references and overlong session IDs are validation failures and do not contact
the payment provider. Provider-confirmed ownership and settlement checks remain
authoritative after input validation.

## Refund return windows

Customer refund creation and edits enforce the configured return window against
the purchased store's recorded fulfillment delivery. Historical JSON snapshots
may hold numeric or string store IDs; both resolve to the same fulfillment.
The aggregate order delivery remains a fallback when no store delivery is recorded.
A newer delivery from another store must not extend an expired line's window.
Rejected creation or edits preserve the pending request and immutable order
snapshots and enqueue no refund notification. The endpoint regression suite is
`server/payment/test_return_window_scope.py`.

Return eligibility resolves historical string and numeric store identifiers to the same per-store fulfillment delivery timestamp. An aggregate order delivery timestamp cannot extend a recorded store delivery window because its identifier was encoded as a string. The purchased item snapshot remains unchanged.

## Dependency injection and adapter architecture

All external services, SDKs, messaging protocols, storage engines, identity verifiers, and time/security primitives are decoupled from core domain services, tasks, and views using Pythonic Dependency Injection (Ports & Adapters):

- **Protocols (`typing.Protocol`)**: Declared with `@runtime_checkable` in respective domain modules across payment, store, core, product, notification, staff, and utils.
- **Decoupled subsystems**:
  1. `PaymentGateway` (`payment/gateways.py`): Checkout sessions, cancellations/expirations, refunds, dispute checks, and provider reconciliation.
  2. `CarrierGateway` (`payment/gateways.py`): Multi-carrier rate estimation across USPS, UPS, FedEx, and DHL.
  3. `WebhookEventVerifier` (`payment/webhook_verifier.py`): Payment webhook signature verification and payload deserialization.
  4. `PayoutGateway` (`store/payout_gateway.py`): Merchant Stripe Connect accounts, onboarding links, and payout transfers.
  5. `StorageSigner` (`store/storage_signer.py`): S3 / MinIO pre-signed multi-part upload URLs.
  6. `NotificationDispatcher` (`notification/dispatcher.py`): Realtime in-app notification creation and dispatch.
  7. `Mailer` (`utils/mailer.py`): Transactional order receipt and security email delivery.
  8. `CatalogInvalidator` (`store/invalidation.py`): Merchant catalog cache invalidation.
  9. `DigitalPreviewGenerator` (`utils/digital_preview.py`): Pillow digital image downsampling, watermark generation, and storage preview creation.
  10. `TokenProvider` (`utils/tokens.py`): JWT access and refresh token generation.
  11. `TaxProvider` (`payment/tax.py`): Local geographic tax jurisdiction calculations and Stripe Tax integration.
  12. `SocialAuthProvider` (`core/social_auth_provider.py`): Google tokeninfo and Apple JWKS asymmetric token verification.
  13. `VisionClient` (`product/vision_client.py`): Deep learning visual similarity and ranker model inference.
  14. `PushGateway` (`notification/push_gateway.py`): Firebase Cloud Messaging (FCM) multicast push delivery.
  15. `BankValidator` (`core/services/bank_validation.py`): ABA routing number verification and account format validation.
  16. `TaskDispatcher` (`utils/task_dispatcher.py`): Celery background queue dispatcher and sync execution.
  17. `SearchIndexer` (`product/indexer.py`): Elasticsearch catalog and notification full-text search indexing.
  18. `RealtimeBroker` (`notification/broker.py`): Redis realtime event publishing and pub/sub broker.
  19. `Clock` (`utils/clock.py`): System and mock time provider for deterministic dispute/return window validation.
  20. `DataEncryptor` (`core/crypto.py`): Fernet cryptographic encryption for sensitive financial and bank account fields.
  21. `DigitalAssetFetcher` (`staff/digital_asset_fetcher.py`): SSRF-safe streaming asset downloader.
  22. `AvatarFetcher` (`core/social_avatar.py`): Streaming profile image download with dimension sanitization.
  23. `CredentialSyncer` (`core/auth_sync.py`): Technical super-admin password synchronization with Grafana and MinIO.
- **Settings configuration**: Pluggable backend paths are registered in `server/codematics/settings.py` via `*_BACKEND` settings with live production defaults. Carrier tracking URLs (`CARRIER_TRACKING_URLS`), carrier base APIs (`CARRIER_API_ENDPOINTS`), and OAuth endpoints (`GOOGLE_TOKENINFO_URL`, `APPLE_JWKS_URL`) are centrally declared in `settings.py` rather than hardcoded.
- **Default-parameter injection**: Services, views, and tasks accept injected gateway/client parameters defaulting to `None`, falling back to `get_<gateway>()` settings factories (`def process_order(..., gateway: Any = None)`).
- **Deterministic mock adapters**: Every protocol is paired with an in-memory `Mock*` adapter that enables offline, hermetic, and fast unit and regression testing without monkey-patching or outbound network dependencies.

## Explicit state machines and transition guards

All domain entity lifecycles must operate as explicit finite state machines validated directly at the model `.save()` boundary:
- **No implicit state transitions**: Entity states must never transition as an uncontrolled side effect of saving arbitrary fields. Direct database saves and workers must adhere to `ALLOWED_TRANSITIONS` graphs defined directly on domain models.
- **Terminal state boundaries**: Terminal states are immutable and irreversible:
  - `Order`: `cancelled` (can only transition to `refunded` on payout return) and `refunded` are strictly terminal; cancelled/refunded orders cannot be reopened.
  - `OrderFulfillment`: `delivered` and `picked up` are strictly terminal.
  - `Refund`: `declined` is terminal.
  - `CheckoutAttempt`: `completed`, `released`, and `refunded` are terminal reservations.
  - `WalletFundingAttempt`: `credited`, `released`, and `refunded` are terminal reservations.
  - `WalletFundingCreation`: `linked` is strictly terminal.
  - `CouponRedemption`: Immutable once redeemed; order cancellations atomically delete the redemption and restore capacity via `coupon.released()`.
  - `StoreTeamMember`: `active` memberships cannot transition back to pending invitations.
  - `StoreEarningsLedger`: `completed` ledger settlements are irreversible.
  - `StorePayout`: `completed` and `rejected` payouts cannot transition to any other status.
  - `DigitalUpload`: `complete` and `aborted` uploads cannot be altered.
  - `BankAccount`: `verification_status` transitions only across explicit states (`pending`, `verified`, `failed`).
  - `Store`: `status` transitions strictly between `pending`, `active`, and `blocked`; official stores cannot be blocked.
  - `Product`: `preview_status` strictly governs the watermarking derivative pipeline (`none` -> `pending` -> `processing` -> `ready` / `failed`).
  - `NotificationDelivery`: `sent` and `failed` deliveries are terminal.
  - `Referral`: `reversed` referrals are terminal.
  - `PlatformCommissionWithdrawal`: `completed` and `failed` withdrawals are terminal.
- **Atomic capacity restoration**: When an order is cancelled or a reserved session expires, allocated resources (such as coupon redemption counts and reserved inventory) are atomically released.

## Asynchronous I/O and Celery workload segregation

Workloads are segregated by execution model:
- **Asynchronous non-blocking I/O (`asyncio`)**: Reserved for high-concurrency, network-bound I/O tasks where waiting on remote sockets would block worker threads, such as concurrent multi-carrier rate calculation (`async_carriers.py`), realtime server-sent events (`stream`), and parallel upstream metadata lookups.
- **Celery background workers**: Dedicated to CPU-intensive processing (such as image manipulation, digital preview watermarking, cryptographic digest validation, and heavy PDF/report compilation) and durable deferred task execution with retry policies.



Merchant order items include an image URL from the saved order snapshot. If the snapshot has no image, the API uses the current product image only when the product still belongs to the requested store. Missing images return an empty string.

The PostgreSQL treasury report uses the configured refund window and checks order creation time for all sales, alongside delivery time for physical sales.

Interactive catalog search sends one Elasticsearch attempt before using the SQL fallback. Indexing retains its configured transport retry behavior.
