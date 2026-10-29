#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
if [ "$#" -eq 0 ]; then set -- dashboard; fi
exec python3 tools/bootstrap.py "$@"
