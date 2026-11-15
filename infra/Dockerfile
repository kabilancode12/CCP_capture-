# The always-on CCP server: ingestion API + indexer (+ optional plug-ins) in one container.
#
# Build context is the repository root (see infra/docker-compose.yml). Only the files the server
# needs are copied, so a local `.env` (secrets) never ends up in the image: the settings reach the
# container as environment variables at run time.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install the package with the server extra (PostgreSQL driver, MinIO client, web server).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[server]"

# The migration files: `ccp-server --migrate` (CCP_SERVER_MIGRATE=true) applies them at start-up.
COPY db/migrations ./db/migrations

# Run as an ordinary user.
RUN useradd --system --no-create-home ccp
USER ccp

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# `python -m ccp.server` is the same as the `ccp-server` command.
CMD ["python", "-m", "ccp.server"]
