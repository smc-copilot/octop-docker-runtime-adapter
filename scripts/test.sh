#!/bin/sh
set -eu
ADAPTER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ADAPTER_PYTHON=${OCTOP_DRA_PYTHON:-/data/octop/.venv/bin/python}
cd "$ADAPTER_ROOT"
export OCTOP_DRA_ENABLED=0
export PYTHONPATH="$ADAPTER_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$ADAPTER_PYTHON" -m unittest discover -s tests -v
