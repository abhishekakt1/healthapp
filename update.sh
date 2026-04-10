#!/usr/bin/env bash
# Family Health — update.sh
# Usage: bash update.sh  (run from your install directory)
set -e
GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
echo -e "${BOLD}Family Health — Update${NC}"
echo "  Pulling latest image…"
docker compose pull
echo "  Restarting with new image…"
docker compose up -d
echo -e "${GREEN}✓ Update complete.${NC} Data untouched."
echo "  Changelog: https://github.com/abhishekakt1/healthapp/releases"