#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x .venv/bin/python ]; then
    printf '%s\n' 'Lancez d’abord : sh installer.sh' >&2
    exit 1
fi
exec .venv/bin/python -m aihub.linux launch
