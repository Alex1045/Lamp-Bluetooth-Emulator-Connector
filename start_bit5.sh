#!/bin/sh
set -eu
BIT5_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$BIT5_PROJECT_DIR/.venv/bin/python" ]; then
    printf 'Existing Python environment missing: %s/.venv\n' "$BIT5_PROJECT_DIR" >&2
    exit 1
fi
exec "$BIT5_PROJECT_DIR/.venv/bin/python" "$BIT5_PROJECT_DIR/start_bit5.py"
