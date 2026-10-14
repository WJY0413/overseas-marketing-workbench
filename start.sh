#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
umask 077
if [ ! -f .env ]; then
    echo 'Missing .env. Run sh setup.sh first.' >&2
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    echo 'Missing Linux virtual environment. Run sh setup.sh first.' >&2
    exit 1
fi
exec .venv/bin/python -m app.linux "$@"
