#!/bin/sh
set -eu

DISPLAY="${DISPLAY:-:99}"
UVICORN_LOG_LEVEL="${UVICORN_LOG_LEVEL:-info}"
XVFB_SCREEN="${XVFB_SCREEN:-1600x1200x24}"

echo "[startup] patent-service container booting" >&2
echo "[startup] PORT=9098 DISPLAY=${DISPLAY} UVICORN_LOG_LEVEL=${UVICORN_LOG_LEVEL}" >&2

for cmd in Xvfb python /usr/local/bin/patent-service-browser; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "[startup] required command missing: ${cmd}" >&2
    exit 127
  fi
done

echo "[startup] launching Xvfb on ${DISPLAY}" >&2
Xvfb "${DISPLAY}" -screen 0 "${XVFB_SCREEN}" -nolisten tcp -ac &
xvfb_pid=$!
echo "[startup] Xvfb pid=${xvfb_pid}" >&2

display_num="${DISPLAY#:}"
x_socket="/tmp/.X11-unix/X${display_num}"
tries=0
while [ ! -S "${x_socket}" ]; do
  tries=$((tries + 1))
  if ! kill -0 "${xvfb_pid}" >/dev/null 2>&1; then
    echo "[startup] Xvfb exited before creating ${x_socket}" >&2
    wait "${xvfb_pid}" || true
    exit 1
  fi
  if [ "${tries}" -ge 50 ]; then
    echo "[startup] timed out waiting for ${x_socket}" >&2
    exit 1
  fi
  sleep 0.1
done

echo "[startup] launching uvicorn on port 9098" >&2

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 9098 --log-level "${UVICORN_LOG_LEVEL}"
