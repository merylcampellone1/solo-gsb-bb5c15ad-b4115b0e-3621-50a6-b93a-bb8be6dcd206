FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 默认监听地址与端口（可通过环境变量覆盖）。
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DEFAULT_MAX_EVALUATIONS=1000 \
    HARD_MAX_EVALUATIONS=100000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8000\")}/healthz', timeout=2)" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port ${PORT}"]
