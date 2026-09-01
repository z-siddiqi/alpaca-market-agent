FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY deploy/risk_watcher/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/risk_watcher ./risk_watcher

CMD ["python", "-m", "risk_watcher.main"]
