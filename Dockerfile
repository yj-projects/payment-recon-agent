FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY app/ ./app/
RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser

COPY static/ ./static/
COPY data/ledger.csv data/bank.csv ./data/

USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
