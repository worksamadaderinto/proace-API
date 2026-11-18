# API documentation

This directory documents Django and API-owned services. Commands below run from this repository root unless stated otherwise. `API/README.md` is the component entry point.

## Setup and architecture

- [Getting started](getting-started.md): Python environment, configuration, migrations, local API and verification.
- [Architecture](architecture.md): module ownership, API entry points and client boundaries.
- [Backend behavior](backend.md): detailed endpoint, validation, notification and financial contracts.

## Security and identity

- [Access control](access-control.md): roles, permissions, staff activation and tenant isolation.
- [Social account linking](social-account-linking.md): server-side identity verification and account linking.

## Commerce and optional services

- [Shipping integrations](shipping-integrations.md): carrier adapters, quotes and fulfillment.
- [Picture search](visual-search.md): API-owned optional recognition service and integration contract.

## Operations

- [Infrastructure](infrastructure.md): API containers, storage and service dependencies.
- [Deployment](deployment.md): HostGator pipeline, secrets and API hostname.

## Other components

UI styling and interaction conventions belong in web documentation (optional sibling/parent workspace reference: `../../web/docs/README.md`). Native application setup belongs in mobile documentation (optional sibling/parent workspace reference: `../../mobile/docs/README.md`). Dashboards, scraping and log infrastructure belong in monitoring documentation (optional sibling/parent workspace reference: `../../monitoring/docs/README.md`). Cross-component migration/rollback guidance belongs in monorepo documentation (optional sibling/parent workspace reference: `../../docs/README.md`).

- [Vault production secrets and activation](vault.md)
