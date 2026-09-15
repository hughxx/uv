#!/bin/sh
set -eu
exec "${COREGEEK_PYTHON:-python3}" -u -B "$(dirname "$0")/main3.py" "$@"
