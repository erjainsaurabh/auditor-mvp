# Playwright's official image ships Python + Chromium + all system libs.
# Pin to the exact version installed locally to avoid selector drift.
FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

WORKDIR /app

# Install Python dependencies first (layer-cached until pyproject.toml changes)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]"

# Chromium is already in the base image; this pins it to the version
# that matches the installed playwright Python package.
RUN playwright install chromium

# Copy application code
COPY flowprobe/   ./flowprobe/
COPY run.py       ./
COPY api.py       ./
COPY config.yaml  ./

# Entrypoint creates required directories then starts uvicorn.
# Flow YAMLs and test_data are NOT baked into the image — they are delivered
# at runtime via the API (yaml_contents / data_content fields) or uploaded
# to the persistent volume.  Only fingerprints and strategy_stats live on
# the volume and persist across runs.
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Runtime config — credentials and API key are injected via fly secrets,
# never baked into the image.
ENV FLOWPROBE_HEADLESS=true

EXPOSE 8000

CMD ["./docker-entrypoint.sh"]
