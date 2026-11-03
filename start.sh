#!/usr/bin/env bash
# WolfPack one-click start — run: ./start.sh
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.11+ was not found. Install it from https://www.python.org/downloads/"
    exit 1
fi
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"; then
    echo "WolfPack needs Python 3.11 or newer:"; python3 --version
    exit 1
fi

echo "Starting WolfPack on http://localhost:7100/ ..."
( sleep 2; (command -v open >/dev/null && open http://localhost:7100/) \
    || (command -v xdg-open >/dev/null && xdg-open http://localhost:7100/) ) &
exec python3 server.py --port 7100
