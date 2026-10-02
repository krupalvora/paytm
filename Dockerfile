FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN useradd --create-home --uid 10001 appuser
USER appuser

ENV PORT=8000
EXPOSE 8000

# WEB_CONCURRENCY uvicorn workers (default 1); see scripts/entrypoint.sh.
CMD ["/usr/local/bin/entrypoint.sh"]
