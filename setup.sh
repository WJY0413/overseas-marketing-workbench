#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
umask 077
python_bin="${PYTHON_BIN:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required; Linux tested with 3.12"'
if [ ! -x .venv/bin/python ]; then
    "$python_bin" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
if [ ! -f .env ]; then
    # Shell noclobber prevents replacing a file created by another setup process.
    (set -C; cat .env.linux.example > .env)
fi
echo 'Linux setup ready. Review .env, then run: sh start.sh'
