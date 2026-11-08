#!/usr/bin/env bash
# One-time setup on Raspberry Pi OS / Debian. Run from the project root:
#   bash deploy/pi/install.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"

echo "==> Installing system packages (python3-venv)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip

PY_OK=$(python3 -c 'import sys; print(sys.version_info >= (3, 11))')
if [ "$PY_OK" != "True" ]; then
  echo "Python 3.11+ is required (Raspberry Pi OS Bookworm ships 3.11)." >&2
  exit 1
fi

echo "==> Creating virtual environment"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet .

mkdir -p data logs backups
chmod 700 data backups

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from template — edit it: nano .env"
fi
chmod 600 .env
[ -f token.json ] && chmod 600 token.json
[ -f credentials.json ] && chmod 600 credentials.json
chmod +x deploy/pi/*.sh

echo "==> Timezone: $(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
echo "    For the Tuesday 10:00 local schedule run: sudo timedatectl set-timezone Europe/Bratislava"
echo
echo "Next steps:"
echo "  1. Copy token.json from your Windows PC (after 'python -m yt_report auth'):"
echo "       scp token.json pi@<pi-host>:$PROJECT_DIR/"
echo "  2. Verify:  .venv/bin/python -m yt_report check"
echo "  3. Test:    .venv/bin/python -m yt_report run --dry-run"
echo "  4. Schedule: crontab -e   (paste lines from deploy/pi/crontab.example)"
