#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Family Health — setup.sh
#  One-command installer for Linux / macOS
#  Usage: curl -sSL https://raw.githubusercontent.com/abhishekakt1/healthapp/main/setup.sh | bash
# ─────────────────────────────────────────────────────────────────────────────
set -e

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║     Family Health — Installer        ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. Check Docker ───────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo -e "${RED}✗ Docker not found.${NC}"
    echo "  Install Docker first: https://docs.docker.com/get-docker/"
    exit 1
fi
echo -e "${GREEN}✓ Docker found:${NC} $(docker --version)"

if ! docker compose version &>/dev/null; then
    echo -e "${RED}✗ Docker Compose v2 not found.${NC}"
    echo "  Upgrade Docker or install the compose plugin."
    exit 1
fi
echo -e "${GREEN}✓ Docker Compose found:${NC} $(docker compose version)"

# ── 2. Create install directory ───────────────────────────────────────────────
INSTALL_DIR="${FAMILYHEALTH_DIR:-$HOME/familyhealth}"
echo ""
echo -e "${BOLD}Install directory:${NC} $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data"
cd "$INSTALL_DIR"

# ── 3. Download docker-compose.yml if not present ────────────────────────────
if [ ! -f docker-compose.yml ]; then
    echo "  Downloading docker-compose.yml…"
    curl -sSL \
      https://raw.githubusercontent.com/abhishekakt1/healthapp/main/docker-compose.yml \
      -o docker-compose.yml
    echo -e "${GREEN}✓ docker-compose.yml downloaded${NC}"
else
    echo -e "${YELLOW}⚠ docker-compose.yml already exists — not overwriting${NC}"
fi

# ── 4. Pull image ─────────────────────────────────────────────────────────────
echo ""
echo "  Pulling latest image (this may take a minute)…"
docker compose pull
echo -e "${GREEN}✓ Image pulled${NC}"

# ── 5. Start ──────────────────────────────────────────────────────────────────
echo ""
echo "  Starting Family Health…"
docker compose up -d

# ── 6. Wait for healthy ───────────────────────────────────────────────────────
echo "  Waiting for app to start…"
for i in $(seq 1 20); do
    if curl -sf http://localhost:8888/health &>/dev/null; then
        break
    fi
    sleep 1
    printf "."
done
echo ""

# ── 7. Show credentials ───────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓ Family Health is running!${NC}"
echo -e "${BOLD}══════════════════════════════════════════${NC}"
echo ""
echo "  Open: http://localhost:8888"
echo ""
echo "  Your admin credentials:"
docker compose logs | grep -A 5 "Admin account created" | grep -E "Email|Password" | \
    sed 's/^/  /' || echo "  (Check logs: docker compose logs | grep -A 12 'FIRST BOOT')"
echo ""
echo "  Next steps:"
echo "   1. Log in and change your password"
echo "   2. Go to Admin → ⚙️ API Keys → add your Gemini key"
echo "      (free key: https://aistudio.google.com/apikey)"
echo "   3. Create family member accounts in Admin → Users"
echo ""
echo "  Update later:   docker compose pull && docker compose up -d"
echo "  View logs:      docker compose logs -f"
echo "  Stop:           docker compose down"
echo ""