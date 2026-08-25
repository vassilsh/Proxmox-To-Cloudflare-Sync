FROM python:3.14-alpine AS builder

# Build tools are only needed to compile aiohttp's C extensions; they don't ship in the final image
RUN apk add --no-cache gcc musl-dev libc-dev

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.14-alpine

RUN apk add --no-cache gettext \
    && adduser -D -H appuser

COPY --from=builder /install /usr/local

WORKDIR /app
COPY app/ /app/
RUN chown -R appuser:appuser /app

USER appuser

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=1 \
    CMD ["/bin/ash", "/app/healthcheck.sh"]

ENTRYPOINT ["/bin/ash", "/app/entrypoint.sh"]
