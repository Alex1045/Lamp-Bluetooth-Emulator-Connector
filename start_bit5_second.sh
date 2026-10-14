#!/bin/sh
# Start the live bridge using the second Bit5's captured profile.
set -eu
BIT5_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$BIT5_PROJECT_DIR"
exec "$BIT5_PROJECT_DIR/.venv/bin/python" "$BIT5_PROJECT_DIR/bit5_live.py" \
    --profile "$BIT5_PROJECT_DIR/profiles/bit5-second.json"
