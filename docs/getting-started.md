# API getting started

Run commands from this repository root. For frontend development use the web setup guide (optional sibling/parent workspace reference: `../../web/docs/README.md`); for native development use the mobile setup guide (optional sibling/parent workspace reference: `../../mobile/docs/README.md`).

## Python and environment

Use Python 3.11 for the Pipenv/CI development environment. Production uses its separately pinned Python 3.12 container and hash-locked runtime dependencies.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r tools/ci/requirements.lock
PIPENV_PIPFILE=server/Pipfile pipenv verify
PIPENV_PIPFILE=server/Pipfile pipenv sync --dev
```

Copy `server/.env.example` to `server/.env` if no local environment exists. Supply actual local settings and keep that file ignored. Never overwrite existing credentials during setup. Direct development supports SQLite; Compose uses PostgreSQL and persistent volumes. See the example file and [infrastructure](infrastructure.md) for service configuration.

During migration, `.venv` may still reference the retained rollback checkout. Do not remove that backup. Create a fresh environment only after preserving/replacing the existing environment link deliberately; see migration notes (optional sibling/parent workspace reference: `../../docs/migration.md`).

## Database and development server

```sh
.venv/bin/python server/manage.py migrate
.venv/bin/python server/manage.py runserver 127.0.0.1:8000
```

The API is available under `http://127.0.0.1:8000/api/v1/`. For explicit development fixtures only, run `.venv/bin/python server/manage.py populate_fake_data`. Seeding is never a deployment step and must preserve an existing technical administrator's password. Do not use removed seed command aliases. Backend behavior and seed guarantees are documented in [backend.md](backend.md).

## Verification

```sh
.venv/bin/python -m pytest server/
```

Web typecheck, unit and browser suites belong to the independent web repository. For cross-service verification, run web integration/E2E serially against the configured seeded API. Set E2E_DEMO_PASSWORD to the existing fixture password without printing or changing it. An isolated API checkout does not require the web source. Report skipped/incomplete suites accurately.

Performance/security runners are under `scripts/qa/` and run directly with `.venv/bin/python scripts/qa/<runner>.py`; optional parent workspace wrappers expose the `test:load`, `test:stress`, `test:volume`, `test:security` and `test:qa` npm commands. Generated API reports belong in ignored `qa-reports/`; retain latest-only completed performance/security output per category and retain bug audits.

## Containers and delivery

Use [API infrastructure](infrastructure.md) for local containers and [HostGator deployment](deployment.md) for production. Keep development email on the console backend unless deliberately testing real delivery with a recipient you control. Outbox/render tests do not establish live inbox delivery.

Dependency and deployment updates follow [supply-chain controls](supply-chain.md). Do not replace existing credentials or reseed to make a dependency/test check pass.


## Purchase/refund acceptance checks

Run the isolated signup, email-verification, authenticated purchase, merchant fulfillment and refund lifecycle with:

```sh
SQLITE_TEST_DATABASE_PATH=/tmp/proace-journey-test.sqlite3 .venv/bin/python -m pytest server/storefront/test_purchase_refund_journey.py -q
```

Fixtures and wallet credit exist only in the test database. Shipment states are simulated through the authorized merchant API; verification email is read from the test outbox rather than delivered to an inbox.

The separate provider-adapter test requires explicit `PROACE_RUN_STRIPE_SANDBOX=1`. It refuses live keys and verifies Stripe's test-mode response before creating a USD 1.00 test PaymentIntent. It exercises the application's real refund adapter, verifies refund idempotency and refunds a remaining test charge during cleanup. Keep keys in the existing ignored local runtime configuration; never put them in command arguments, traces or reports. This connector check does not complete hosted Checkout or establish webhook/email/push delivery.

Web browser checks run serially with `playwright test tests/checkout-audit.spec.ts tests/refund-audit.spec.ts --project=desktop --project=mobile --workers=1`. Their API responses are controlled test fixtures, so they establish UI behavior rather than live payment settlement.

The opt-in hosted journey uses an isolated API database, a temporary production web server and actual Stripe test Checkout. Set `PROACE_RUN_HOSTED_JOURNEY=1`, `PROACE_HOSTED_BROWSER_RUNNER` to an explicitly supplied web checkout's `scripts/hosted-checkout-journey.mjs`, and `PROACE_JOURNEY_NODE` to its compatible Node executable, then run `server/storefront/test_hosted_checkout_journey.py` with pytest. Build that web checkout first. Normal API verification does not require the web repository and skips this test.

The runner verifies provider test mode before entering a test card, then checks browser return, merchant delivery transitions, customer refund request and staff approval. It signs and replays the actual Stripe completion event twice to check callback idempotency; this does not establish public webhook transport delivery. Verification links come from the isolated outbox. Cleanup expires unpaid sessions or refunds remaining test charges. It records no screenshots, browser traces or private credentials.
