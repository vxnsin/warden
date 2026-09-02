FROM python:3.13-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /app/.venv \
 && /app/.venv/bin/pip install --no-cache-dir .

FROM python:3.13-slim

COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 10001 warden \
 && mkdir /data \
 && chown warden:warden /data
USER warden

# Inside a container there is nothing else to reach it on, and the network is
# whatever the compose file allows in. Set WARDEN_TOKEN anyway if anything
# outside that network can route to the published port.
ENV WARDEN_HOST=0.0.0.0 \
    WARDEN_DATABASE=/data/warden.db

VOLUME ["/data"]
EXPOSE 7010

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import httpx, sys; sys.exit(0 if httpx.get('http://127.0.0.1:7010/health', timeout=3).status_code == 200 else 1)"]

ENTRYPOINT ["warden"]
CMD ["serve"]
