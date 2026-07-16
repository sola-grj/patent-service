#!/bin/sh
set -eu

if command -v google-chrome >/dev/null 2>&1; then
  exec google-chrome "$@"
fi

if command -v chromium >/dev/null 2>&1; then
  exec chromium "$@"
fi

if command -v chromium-browser >/dev/null 2>&1; then
  exec chromium-browser "$@"
fi

echo "No supported browser binary found for patent-service" >&2
exit 1
