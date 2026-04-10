# 🏥 Family Health

A self-hosted family health record system. Track lab reports, prescriptions,
and investigations for your whole family. AI-powered extraction via Gemini.

## Quick Start (2 minutes)

**Requirements:** Docker + Docker Compose — nothing else.

```bash
# 1. Create a folder and download the compose file
mkdir familyhealth && cd familyhealth
curl -O https://raw.githubusercontent.com/abhishekakt1/healthapp/main/docker-compose.yml

# 2. Start
docker compose up -d

# 3. Get your auto-generated admin password
docker compose logs | grep -A 12 "FIRST BOOT"

# 4. Open the app
open http://localhost:8888
```

**First login:** Use the email and password from the logs.
You'll be prompted to change your password immediately.

**Add Gemini API keys:** Log in → Admin tab → ⚙️ API Keys → paste your key.
Get a free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
The app works without AI keys — you can still manage records manually.

---

## Configuration

All configuration is optional. The app runs with zero configuration.

Edit `docker-compose.yml` to override defaults:

```yaml
environment:
  ADMIN_EMAIL: admin@yourfamily.com    # default: admin@family.health
  ADMIN_PASSWORD: your-password        # default: auto-generated
  GEMINI_API_KEY: AIzaSy...            # or add via Admin UI
  JWT_SECRET: change-this-in-prod      # random session secret
```

**Port:** Change the left side of `8888:8080` if port 8888 is taken.

---

## Updating

```bash
docker compose pull          # download latest image
docker compose up -d         # restart with new image
```

Your data in `./data/` is never touched during updates.

---

## Backup & Restore

```bash
# Backup (while running)
cp -r ./data ./data-backup-$(date +%Y%m%d)

# Restore
docker compose down
cp -r ./data-backup-20240101 ./data
docker compose up -d
```

---

## Data Location

All data lives in `./data/` next to your `docker-compose.yml`:

```
./data/
├── health_records.db    ← SQLite database (all records)
└── uploads/             ← uploaded PDFs and images
    └── family_member/
        ├── lab/
        └── prescription/
```

---

## Architecture

- **Backend:** FastAPI (Python)
- **Database:** SQLite (zero-config, single file)
- **AI:** Google Gemini (via API key, free tier sufficient for family use)
- **Image:** Multi-arch (`linux/amd64` + `linux/arm64`) on GHCR

---

## Platforms

Tested on: Ubuntu, Debian, macOS (Apple Silicon + Intel), Raspberry Pi 4/5.
Requires: Docker 24+ and Docker Compose v2+.