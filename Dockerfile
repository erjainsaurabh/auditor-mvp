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
COPY auditor/     ./auditor/
COPY run.py       ./
COPY api.py       ./
COPY config.yaml  ./

# Runtime config — credentials and API key are injected via --env or --env-file
# at container start, never baked into the image.
ENV AUDITOR_HEADLESS=true

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
