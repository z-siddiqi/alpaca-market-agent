FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/market_agent ./src/market_agent

RUN pip install --no-cache-dir .

CMD ["sh", "-c", "uvicorn market_agent.service:app --host 0.0.0.0 --port ${PORT:-8080}"]
