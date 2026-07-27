# Serve-only image for the hosted IncidentLens app.
#
# Installs only [graph] and [sandbox] — a graph library and one model client.
# Deliberately does NOT install the studio/genblaze extras or ffmpeg. Rendering an
# incident replay takes minutes of CPU and is done offline; the finished media and
# its Genblaze provenance manifests live in Backblaze B2, and the browser streams
# them directly from the public bucket. The server only reconstructs incidents and
# serves the gallery, so it stays small, boots fast, and holds no credentials.
#
# To render locally instead: pip install -e ".[studio,genblaze]" and use the CLI.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so code edits do not bust the pip cache.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir ".[graph,sandbox]"

# Run unprivileged.
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# Health is checked by the platform against /api/v1/health (see render.yaml / fly.toml),
# so no Dockerfile HEALTHCHECK is needed.
#
# PORT is injected by most PaaS platforms; default to 8000 locally.
CMD ["sh", "-c", "incidentlens serve --host 0.0.0.0 --port ${PORT:-8000}"]
