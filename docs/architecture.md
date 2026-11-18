# API architecture

ProAce's API is a Django REST Framework modular monolith in `server/`. It is authoritative for ownership, permissions, prices, stock, destination tax, payments, refunds, merchant balances and persisted transaction snapshots. Web and native clients submit inputs for server validation.

## Modules

Merchant refund title search uses the same store-scoped item projection as its
response: selected refund items, immutable order items, then legacy cart fallback.
Catalog edits do not rewrite historical titles or ownership. Refund metadata
search remains available within the authorized store's records.

| Path under server/ | Responsibility |
| --- | --- |
| codematics/ | Settings and WSGI/ASGI entry points |
| storefront/ | Integrated API routes, request validation, serializers and orchestration |
| core/ | Users, identity, addresses and shared customer records |
| product/ | Catalog, eligibility, product policies and search |
| store/ | Merchant ownership, teams, moderation and store finances |
| cart/ | Active carts, lines and guest-merge receipts |
| payment/ | Checkout, orders, tax, shipping, refunds and provider integration |
| notification/ | Recipient-scoped inbox, transactional delivery outbox and dispatch |
| affiliates/ | Referrals and external partner handoff |
| staff/ | Operational API functionality |
| observability/ | Health, metrics, request instrumentation and trace integration |
| vision/ | Optional picture-recognition service |

## Client and authentication boundaries

Public API contracts are rooted at `/api/v1/`; inspect `server/storefront/urls.py` for the current route map. Browser traffic reaches the same-origin Next.js proxy in `web/`, which manages HTTP-only authentication cookies and forwards credentials to Django. Native mobile uses its SecureStore credential adapter and calls the API directly. Django enforces authorization in both cases; client visibility controls are not security boundaries.

Validate ownership from the authenticated user. Reject client-asserted prices, inventory, discounts and financial outcomes. ClickBank listings use the external vendor checkout and cannot be purchased through native cart/checkout. Preserve immutable order/address/tax/payment snapshots and transactional inventory/wallet behavior.

## Background work and integrations

The API, notification worker, notification stream and Celery scheduler share backend code. Persist delivery outbox records with their business events, dispatch after commit, and preserve idempotency, retries and recipient isolation. External provider failures require safe user guidance and trusted diagnostic logging without credential disclosure.

Native app-store billing is intentionally excluded; the disabled compatibility contract rejects purchase claims without granting access. See [backend contracts](backend.md).

## Operations and related guides

HostGator deploys API services and the API edge through `.github/workflows/ci-cd.yml`. Web deploys independently to Vercel, mobile builds/submits through EAS, and monitoring hosting is pending. See [infrastructure](infrastructure.md), [deployment](deployment.md), [access control](access-control.md), [shipping](shipping-integrations.md), and [detailed backend behavior](backend.md).

Store settings can be saved through `PATCH /api/v1/stores/{id}/settings/` with explicit `name`, `profile` and `address` sections. The endpoint enforces existing owner/store-administrator permissions, locks the scoped store, validates every section and commits all three records in one database transaction. Failures roll back the full update; catalog invalidation runs after commit. Existing single-resource endpoints remain available to clients that edit only one section.
