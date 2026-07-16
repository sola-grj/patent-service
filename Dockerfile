ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    PATENT_SERVICE_WIPO_SELENIUM_HEADLESS=false \
    PATENT_SERVICE_WIPO_SELENIUM_CHROME_BINARY=/usr/bin/chromium \
    PATENT_SERVICE_WIPO_SELENIUM_DRIVER_PATH=/usr/bin/chromedriver

WORKDIR /app

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        fonts-noto-cjk \
        gnupg \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libexpat1 \
        libfontconfig1 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libu2f-udev \
        libvulkan1 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        wget \
        xauth \
        xdg-utils \
        xvfb \
    && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver

COPY docker/browser-wrapper.sh /usr/local/bin/patent-service-browser
COPY docker/start-with-xvfb.sh /usr/local/bin/start-with-xvfb

RUN chmod +x /usr/local/bin/patent-service-browser /usr/local/bin/start-with-xvfb \
    && fc-cache -f -v \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && python -m pip install . -i https://pypi.tuna.tsinghua.edu.cn/simple

RUN mkdir -p /tmp/patent-service/wipo

EXPOSE 9098

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD sh -c 'curl -fsS "http://127.0.0.1:9098/api/health" >/dev/null || exit 1'

CMD ["/usr/local/bin/start-with-xvfb"]
