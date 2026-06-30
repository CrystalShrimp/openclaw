#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "Error: .env file not found. Copy .env.example to .env and fill in your config."
    echo "  cp .env.example .env"
    exit 1
fi

exec uv run python -m app.main
