#!/usr/bin/env bash
# One-command on-sale stampede. Usage:
#   ./burst.sh <BASE_URL> [--admin-token TOKEN] [--requests 20000] [--quick] ...
# Uses uv if present (deps resolved from the script header), else docker, else python3+httpx.
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -lt 1 ]; then
  echo "usage: $0 <BASE_URL> [burst.py options]   (see: $0 http://x --help)" >&2
  exit 2
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run --quiet burst/burst.py "$@"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  # host.docker.internal lets a containerised client reach a service on the host's localhost.
  args=("$@"); args[0]="${args[0]/localhost/host.docker.internal}"; args[0]="${args[0]/127.0.0.1/host.docker.internal}"
  exec docker run --rm -t -e ADMIN_TOKEN="${ADMIN_TOKEN:-}" -v "$PWD:/w" -w /w python:3.12-slim \
    sh -c 'pip install -q "httpx>=0.27" && python burst/burst.py "$@"' _ "${args[@]}"
elif python3 -c 'import httpx' >/dev/null 2>&1; then
  exec python3 burst/burst.py "$@"
else
  echo "need one of: uv, docker, or python3 with httpx installed (pip install httpx)" >&2
  exit 1
fi
