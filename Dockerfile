FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DELIVERY_DB_PATH=/data/deliveries.db

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown app:app /data

COPY --from=builder /opt/venv /opt/venv

USER app
WORKDIR /app

# Mount a volume here so delivery idempotency survives container restarts.
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "devin_remediation_automation.main:app", "--host", "0.0.0.0", "--port", "8000"]
