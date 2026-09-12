# Pinned to a minor tag rather than latest: a silent jump to a new Python would
# be a strange way to find out the scraper broke.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: they change far less often than the code, so edits to the
# pipeline do not reinstall the world.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ core/
COPY local_connectors/ local_connectors/
COPY app/ app/
COPY config.toml ./

# Nothing here needs root, and the mounted resume is somebody's personal data.
RUN useradd --create-home --uid 10001 runner \
 && mkdir -p /app/state /app/reports \
 && chown -R runner:runner /app
USER runner

# No secret is baked in: GOOGLE_API_KEY arrives through the environment.
CMD ["python", "-m", "app.main"]
