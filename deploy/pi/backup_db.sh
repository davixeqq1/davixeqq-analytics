#!/usr/bin/env bash
# Consistent online SQLite backup (safe while the DB is in use). Keeps the last 12.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR"
DB="${DATABASE_PATH:-data/yt_report.db}"
[ -f "$DB" ] || { echo "No database at $DB"; exit 0; }

mkdir -p backups
TARGET="backups/yt_report_$(date +%Y%m%d).db"
.venv/bin/python - "$DB" "$TARGET" <<'EOF'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close(); dst.close()
EOF
chmod 600 "$TARGET"
ls -1t backups/yt_report_*.db | tail -n +13 | xargs -r rm --
echo "Backup written: $TARGET"
