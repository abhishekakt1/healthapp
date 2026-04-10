# Developer Deployment Guide

This guide covers: setting up the GitHub repo, VPS deploy key,
publishing to GHCR, and keeping your VPS in sync.

---

## 1. Repo Structure

```
healthapp/                        ← git root
├── .github/
│   └── workflows/
│       └── docker-publish.yml    ← auto-build on git tag
├── healthapp/
│   └── app/                      ← application code
│       ├── main.py
│       ├── gemini_utils.py
│       ├── requirements.txt
│       └── templates/
│           └── dashboard.html
├── docker-compose.yml            ← what end-users download
├── docker-compose.override.yml   ← your dev override (gitignored)
├── Dockerfile
├── setup.sh                      ← one-command installer
├── update.sh
├── backup.sh
├── README.md
├── DEPLOY.md                     ← this file
└── .gitignore
```

---

## 2. Initial GitHub Setup

```bash
# On your local machine / VPS where code lives:
cd ~/healthapp

git init
git remote add origin git@github.com:abhishekakt1/healthapp.git

# First push
git add .
git commit -m "Initial release v1.0"
git push -u origin main
```

**On GitHub → repo Settings → Actions → General:**
- Workflow permissions → "Read and write permissions" ✓

---

## 3. VPS Deploy Key (pull-only, no password)

```bash
# On your VPS:
ssh-keygen -t ed25519 -f ~/.ssh/healthapp_deploy -N "" -C "healthapp-vps-deploy"

# Show the PUBLIC key — copy this
cat ~/.ssh/healthapp_deploy.pub
```

**On GitHub → repo Settings → Deploy Keys:**
- Title: `VPS Deploy Key`
- Key: paste the public key
- Allow write access: ❌ (read-only is safer)
- Click "Add deploy key"

```bash
# On your VPS — add SSH config entry:
cat >> ~/.ssh/config << 'EOF'
Host github-healthapp
    HostName github.com
    User git
    IdentityFile ~/.ssh/healthapp_deploy
    IdentitiesOnly yes
EOF

# Test it:
ssh -T github-healthapp
# Expected: "Hi abhishekakt1! You've successfully authenticated..."

# Clone using the alias:
cd ~
git clone github-healthapp:abhishekakt1/healthapp.git
```

---

## 4. Publishing to GHCR (GitHub Container Registry)

Images are built automatically by GitHub Actions when you push a version tag.

```bash
# Tag a release:
git tag v1.0.0
git push origin v1.0.0
```

This triggers `.github/workflows/docker-publish.yml` which:
1. Builds `linux/amd64` + `linux/arm64` in parallel
2. Pushes to `ghcr.io/abhishekakt1/healthapp:v1.0.0`
3. Also tags as `ghcr.io/abhishekakt1/healthapp:latest`

**Make the package public** (so users don't need auth to pull):
GitHub → your profile → Packages → healthapp → Package Settings → Change visibility → Public

---

## 5. Update docker-compose.yml with your image name

Edit `docker-compose.yml` and replace:
```yaml
image: ghcr.io/abhishekakt1/healthapp:latest
```
with your actual GitHub username (lowercase):
```yaml
image: ghcr.io/abhishekakt1/healthapp:latest
```

---

## 6. Your VPS Workflow (day-to-day)

```bash
# Deploy app code changes:
cd ~/healthapp
cp /path/to/updated/main.py healthapp/app/main.py
cp /path/to/updated/dashboard.html healthapp/app/templates/
docker compose restart web

# OR: pull from git and rebuild image:
git pull
git tag v1.0.1
git push origin v1.0.1
# Wait for GitHub Actions to build (~3 min)
# Then on VPS:
docker compose pull && docker compose up -d
```

---

## 7. For Users (friends & family)

Share this one-liner:
```bash
curl -sSL https://raw.githubusercontent.com/abhishekakt1/healthapp/main/setup.sh | bash
```

Or manual:
```bash
mkdir familyhealth && cd familyhealth
curl -O https://raw.githubusercontent.com/abhishekakt1/healthapp/main/docker-compose.yml
docker compose up -d
docker compose logs | grep -A 12 "FIRST BOOT"
```

---

## 8. SSL / HTTPS (optional but recommended)

For exposing over the internet, put nginx + certbot in front:

```yaml
# Add to docker-compose.yml services:
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./certbot/conf:/etc/letsencrypt:ro
      - ./certbot/www:/var/www/certbot:ro
    depends_on:
      - web
```

For local network only (most home users), HTTP on port 8888 is fine.