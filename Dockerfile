# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    A2A_HOST=0.0.0.0 \
    A2A_PORT=8080 \
    A2A_AGENTS_FILE=/app/config/agents.yaml \
    A2A_DB_PATH=/app/data/a2a_hub.db

WORKDIR /app

# 依赖单独一层，改代码不会导致重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY a2a_hub/ ./a2a_hub/
COPY web/ ./web/
COPY config/ ./config/
COPY run.py pyproject.toml README.md ./

RUN mkdir -p /app/data && \
    useradd -m -u 10001 hub && chown -R hub:hub /app
USER hub

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "a2a_hub.cli", "serve", "--host", "0.0.0.0", "--port", "8080"]
