#!/usr/bin/env bash
# For the machine that already runs CCP (this laptop, or a senior who set it up before):
# keeps .env, the database, MinIO and every setting as they are, asks nothing, and brings everything
# up to date with the code in this folder: installs the packages, starts the containers (or uses the
# ones that already run), applies new migrations, restarts the CCP server, refreshes the dashboard and
# the capture settings, and finishes with a check of the latest sessions.
#
#   ./run-laptop.sh          update and start everything
#   ./run-laptop.sh check    only the check
set -euo pipefail
cd "$(dirname "$0")"
[[ -f .env ]] || { echo "No .env here: this is a new machine, run ./run.sh instead."; exit 1; }
if [[ "${1:-}" == "check" ]]; then exec ./run.sh check; fi
exec ./run.sh update
