#!/bin/sh
set -eu
ADAPTER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ADAPTER_PYTHON=${OCTOP_DRA_PYTHON:-/data/octop/.venv/bin/python}
export PYTHONPATH="$ADAPTER_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OCTOP_DRA_ENABLED=0
export OCTOP_DRA_CONFIG="${OCTOP_DRA_CONFIG:-$ADAPTER_ROOT/config/adapter.json}"
exec "$ADAPTER_PYTHON" -m octop_docker_runtime_adapter.smoke "$@"
