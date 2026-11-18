# API infrastructure

Native `GET /api/v1/notifications/stream/` enters the authenticated edge, but Nginx sends this exact path to the restricted `notification-stream` process, with proxy buffering/cache disabled. The regular API worker pool handles JSON requests. Compose and HostGator mount `nginx/stream-upstream.docker.conf`; host installs use `stream-upstream.conf` (loopback 8001), while Kubernetes must provision the internal `commerce-notification-stream:8000` service referenced by its upstream file. Keep the stream service private; never send a bearer token to a client-selected stream origin.

Mobile dev URLs use port 8080, not direct Gunicorn port 8000. The edge preserves authentication, rate limits and IP policy. A successful SSE connection emits `ready`, heartbeats, and closes normally after about 25 seconds. Clients must not classify that lifetime as a JSON request timeout.

The stream endpoint explicitly negotiates `Accept: text/event-stream` (including browser EventSource). Successful responses stream SSE directly; authentication and broker failures retain JSON error payloads and their HTTP error status.

Docker upstreams use the embedded Docker DNS resolver with a shared Nginx upstream zone, so API/stream container IP changes are re-resolved rather than leaving the edge connected to a stale address after a restart. Nginx applies gzip compression for JSON and static text payloads >= 1KB with `gzip_vary on`, reducing wire transfer sizes by ~80%.

Backoffice/store dashboard `days` accepts `all` or an integer from 1–365. `all` removes the lower history bound, returns `period.from=null`, and preserves role/store isolation. Store activity is grouped by month (`period.granularity=month`) for all history, and by day for finite windows. Inventory/wallet configuration counts retain their existing scope. Overview counts are grouped aggregates; treasury iterates bounded ledger batches and prefetches historical cart data. Cached financial/top-store projections are invalidated immediately and again after commit when contributing records change. Queryset/bulk changes must explicitly invalidate projections or use the normal model/service paths.

Commands run from this repository root. This guide covers backend-owned containers and dependencies; observability dashboards and collectors belong to monitoring (optional sibling/parent workspace reference: `../../monitoring/docs/README.md`).

## Local topology

`server/compose.yaml` defines the API, notification worker, dedicated media worker, notification stream, Celery scheduler, Nginx gateway, PostgreSQL, Redis, Elasticsearch, MinIO and optional service profiles. Inspect that file for the exact enabled services, ports, health checks and volume mounts. Optional picture recognition is documented in [visual-search.md](visual-search.md).

Preserve the `commerce-server` Compose project, `commerce-network` and existing named volumes. The API stack's Nginx also provides the existing local web gateway on port 3000; this is local parity infrastructure, not a production web deployment.

For an initial local stack, follow the Compose service dependencies. For code updates, rebuild/recreate only affected consumers:

```sh
docker compose -f server/compose.yaml up -d --build --no-deps api
```

Include `notification-worker`, `media-worker`, `notification-stream` and `celery-beat` when shared code/dependencies affect them. Update Nginx when its configuration changes. Run required migrations through the updated API container, inspect status/health and check startup logs. A successful image build alone is insufficient. Never delete volumes, run down or reseed to apply code changes.

## Configuration and storage

Use the ignored `server/.env` for local configuration. Application media uses dedicated MinIO service credentials, not root credentials. Preserve PostgreSQL and object-storage volumes, recorded transaction snapshots and existing technical-admin credentials. The technical-admin synchronization flow retains its existing Grafana/MinIO behavior; monitoring is a separate component.

On HostGator, production secrets come from Vault Agent memory-backed files, with public JSON configuration; see [Vault operations](vault.md). The API edge serves `API_DOMAIN`; frontend origin and CSRF settings must match the independently deployed web origin. See [deployment.md](deployment.md) for the supported release path. HostGator tooling must not deploy web or monitoring.

## Observability and reports

Django owns health endpoints, request instrumentation, metrics and application trace/log integration. Monitoring owns Prometheus/Grafana/Loki/Tempo configuration and hosting. Never expose credentials, raw exceptions, internal paths or personal data in user-facing errors or telemetry intended for public clients.

Keep API reports under ignored `qa-report/`, preserving only the latest completed performance/security output per category. Migration-specific rollback and dependency notes belong in monorepo migration documentation (optional sibling/parent workspace reference: `../../docs/migration.md`), not the permanent service topology.


The media-worker consumes only the queue defined by CELERY_MEDIA_QUEUE. It uses
prefetch multiplier one, CELERY_MEDIA_WORKER_CONCURRENCY (default one), and
MEDIA_WORKER_CPUS / MEDIA_WORKER_MEMORY container limits. Notification delivery,
search reconciliation and financial recovery retain the default queue. Run both
workers whenever enabling the media routes; HostGator’s verified-image startup
includes both, without adding another image build or runtime credentials.


## Response compression

The API edge enables gzip for JSON and textual assets above its configured minimum size. Verify actual Content-Encoding, Vary and transferred bytes through the intended gateway; JSON body size alone is not wire size. Django query-plan measurements must use the SQL generated by the installed backend, not a different raw ILIKE predicate. See [trigram expression alignment](backend.md#trigram-expression-alignment).
