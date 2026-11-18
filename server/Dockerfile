FROM cgr.dev/chainguard/minio-client@sha256:fc78c525b3227e4d256ed1fc285ee08976688c7ac870f3daed73469d5da750ba AS minio-client

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=codematics.storefront_settings

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY requirements.lock requirements-bootstrap.lock requirements-stream.lock ./
# Storage requirements are folded into the base lock; retain the Compose argument for compatibility.
ARG INSTALL_OBJECT_STORAGE=false
COPY --from=minio-client /usr/bin/mc /usr/local/bin/mc
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --require-hashes --only-binary=:all: -r requirements-bootstrap.lock \
    && python -m pip install --require-hashes --only-binary=:all: -r requirements.lock \
    && python -m pip check

ARG INSTALL_STREAM_POOL=false
RUN if [ "$INSTALL_STREAM_POOL" = "true" ]; then python -m pip install --require-hashes --only-binary=:all: -r requirements-stream.lock && python -m pip check; fi

COPY --chown=app:app . .
RUN chmod +x docker-entrypoint.sh \
    && mkdir -p /app/media /app/staticfiles /data /tmp/prometheus \
    && DATABASE_ENGINE=sqlite python manage.py collectstatic --noinput \
    && chown -R app:app /app /app/media /app/staticfiles /data /tmp/prometheus

USER app
EXPOSE 8000

ENTRYPOINT ["/bin/sh", "/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "codematics.wsgi:application"]
