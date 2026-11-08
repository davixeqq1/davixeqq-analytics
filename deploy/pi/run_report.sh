#!/usr/bin/env bash
# Cron wrapper: same command as on Windows / GitHub Actions (`python -m yt_report run`).
# flock prevents overlapping runs; output is appended to logs/cron.log.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR" || exit 1
mkdir -p logs

exec 9>"$PROJECT_DIR/logs/.run.lock"
if ! flock -n 9; then
  echo "$(date -Is) another run is in progress, skipping" >> logs/cron.log
  exit 0
fi

echo "$(date -Is) starting weekly report" >> logs/cron.log
"$PROJECT_DIR/.venv/bin/python" -m yt_report run "$@" >> logs/cron.log 2>&1
status=$?
echo "$(date -Is) finished with exit code $status" >> logs/cron.log
exit $status
