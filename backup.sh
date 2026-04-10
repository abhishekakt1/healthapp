#!/usr/bin/env bash
# Family Health — backup.sh
# Usage: bash backup.sh  (run from your install directory)
set -e
GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
BACKUP_DIR="./backups/backup-$(date +%Y%m%d-%H%M%S)"
echo -e "${BOLD}Family Health — Backup${NC}"
mkdir -p "$BACKUP_DIR"
cp -r ./data "$BACKUP_DIR/"
echo -e "${GREEN}✓ Backup saved to: $BACKUP_DIR${NC}"
echo "  Contains: database + all uploaded files"
# Keep only last 10 backups
ls -dt ./backups/backup-* 2>/dev/null | tail -n +11 | xargs rm -rf 2>/dev/null || true
echo "  (Older backups auto-removed, keeping last 10)"