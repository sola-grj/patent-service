ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATENT_SERVICE_RAPIDOCR_MODEL_CACHE_DIR=/opt/patent-service/models/rapidocr \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libreoffice-writer \
        tesseract-ocr \
        tesseract-ocr-ara \
        tesseract-ocr-chi-sim \
        tesseract-ocr-deu \
        tesseract-ocr-eng \
        tesseract-ocr-fra \
        tesseract-ocr-jpn \
        tesseract-ocr-kor \
        tesseract-ocr-por \
        tesseract-ocr-rus \
        tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && python -m pip install . -i https://pypi.tuna.tsinghua.edu.cn/simple

RUN mkdir -p /tmp/patent-service/wipo /tmp/patent-service/analysis \
        /opt/patent-service/models/rapidocr \
    && python -m app.analysis.preload_ocr_models --languages en,de,fr,ru,ko,ar

EXPOSE 9098

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD sh -c 'curl -fsS "http://127.0.0.1:9098/api/health" >/dev/null || exit 1'

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9098"]
