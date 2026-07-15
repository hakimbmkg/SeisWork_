#!/usr/bin/env bash
# ==============================================================================
# setup_gunicorn_nginx.sh — production hardening for the SeisWork Web GUI:
# replace the Werkzeug dev server with gunicorn behind an nginx reverse proxy
# (TLS termination + IP allowlist + security headers).
#
# Context: the dev server (`seiswork gui`) prints "WARNING: this is a
# development server" for a reason — no TLS, not built for the open internet.
# This script puts gunicorn (single worker, multiple threads — see note
# below) behind nginx, which does TLS + restricts who can reach the GUI at
# all before a single request hits the app.
#
# EDIT THE CONFIG BLOCK BELOW FIRST, then run on the target machine as a user
# with sudo:
#   bash tools/setup_gunicorn_nginx.sh
#
# Idempotent: safe to re-run after editing the config block.
# ==============================================================================
set -e

# ══════════════════════════════ CONFIG — EDIT ME ═════════════════════════════
# Comma-separated CIDR ranges allowed to reach the GUI (BMKG office / VPN
# ranges). Everything else is denied at the nginx layer, before auth even
# matters. THIS IS THE MAIN ACCESS CONTROL — get it right.
ALLOW_CIDRS="203.0.113.0/24,198.51.100.0/24"   # TODO: ganti ke rentang BMKG asli

# Domain name for the TLS certificate (leave empty to skip certbot and use a
# self-signed cert instead — fine for an IP-only/internal deployment, but
# browsers will warn; get a real cert once DNS is set up).
DOMAIN=""                                       # TODO: isi kalau ada domain, mis. seiswork.bmkg.go.id

# Port the SeisWork GUI should listen on internally (gunicorn binds only to
# 127.0.0.1 here — nginx is the only thing exposed externally).
APP_PORT=5000

# Conda env with `seiswork` + gunicorn installed.
ENV_BIN="/opt/miniconda3/envs/seiswork/bin"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# ═══════════════════════════════════════════════════════════════════════════

command -v nginx >/dev/null 2>&1 || {
  echo "► Installing nginx..."
  sudo apt-get update -qq
  sudo apt-get install -y nginx
}

"$ENV_BIN/pip" show gunicorn >/dev/null 2>&1 || {
  echo "► Installing gunicorn into $ENV_BIN's env..."
  "$ENV_BIN/pip" install gunicorn
}

# ── 1. Gunicorn systemd (--user) unit — replaces the Werkzeug dev server ────
# IMPORTANT: --workers MUST stay 1. This app keeps job/session state in plain
# in-process memory (no Redis/shared store) — multiple worker PROCESSES would
# each get their own copy, so requests handled by different workers wouldn't
# see each other's jobs. Use --threads for concurrency instead (threads share
# one process's memory, same as the dev server's own threaded=True).
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/seiswork-gunicorn.service" <<EOF
[Unit]
Description=SeisWork — Web GUI (gunicorn, production)
Documentation=https://github.com/HakimBMKG/seiswork
After=network.target

[Service]
Type=simple
WorkingDirectory=$BASE_DIR
ExecStart=$ENV_BIN/gunicorn --workers 1 --threads 8 --worker-class gthread \\
          --timeout 120 --bind 127.0.0.1:$APP_PORT seiswork.web.wsgi:app
Environment=PYTHONNOUSERSITE=1
Environment=SEISWORK_TRUST_PROXY=1
Environment=PATH=$ENV_BIN:/opt/miniconda3/condabin:$BASE_DIR/core/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Restart=on-failure
RestartSec=5
# See seiswork.service's own KillMode=process note: slarchive is spawned with
# start_new_session=True so it survives an app-level restart — the default
# KillMode=control-group would still kill it via the cgroup on a unit
# restart/stop, so this must stay "process" (signals only the tracked PID).
KillMode=process
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

echo "► If the Werkzeug dev-server unit ('seiswork.service') is running, stop it"
echo "  first so both don't fight over port $APP_PORT:"
echo "    systemctl --user stop seiswork.service"
echo "    systemctl --user disable seiswork.service"

systemctl --user daemon-reload
systemctl --user enable --now seiswork-gunicorn.service
sleep 2
systemctl --user is-active seiswork-gunicorn.service --quiet \
  && echo "✓ seiswork-gunicorn.service is active" \
  || { echo "✗ failed to start — check: journalctl --user -u seiswork-gunicorn -n 50"; exit 1; }

# ── 2. nginx: TLS + IP allowlist + reverse proxy to gunicorn ────────────────
IFS=',' read -ra CIDRS <<< "$ALLOW_CIDRS"
ALLOW_BLOCK="$(for c in "${CIDRS[@]}"; do printf '    allow %s;\n' "$c"; done)"

sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled

if [ -n "$DOMAIN" ]; then
  CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
  if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
    echo "► No cert found for $DOMAIN yet. Run this separately once DNS points here:"
    echo "    sudo apt-get install -y certbot python3-certbot-nginx"
    echo "    sudo certbot --nginx -d $DOMAIN"
    echo "  Re-run this script after that succeeds."
  fi
else
  CERT_DIR="/etc/ssl/seiswork-selfsigned"
  if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
    echo "► No domain configured — generating a self-signed cert (browsers will"
    echo "  warn; fine for an internal-IP deployment, replace with a real cert +"
    echo "  DOMAIN once available)."
    sudo mkdir -p "$CERT_DIR"
    sudo openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
      -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/fullchain.pem" \
      -subj "/CN=seiswork" >/dev/null 2>&1
  fi
fi

sudo tee /etc/nginx/sites-available/seiswork-secure > /dev/null <<NGINX
map \$http_upgrade \$connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name _;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name _;

    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;

    client_max_body_size 256m;
    proxy_read_timeout   120s;

    # ── Access control: only these ranges may reach the GUI at all ─────────
$ALLOW_BLOCK
    deny  all;

    # Optional extra credential layer, independent of SeisWork's own
    # (currently unused) token feature. To enable:
    #   sudo apt-get install -y apache2-utils
    #   sudo htpasswd -c /etc/nginx/.htpasswd operator
    # then uncomment:
    # auth_basic           "SeisWork";
    # auth_basic_user_file /etc/nginx/.htpasswd;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location / {
        proxy_pass         http://127.0.0.1:$APP_PORT;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_set_header   X-Forwarded-Host \$host;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection \$connection_upgrade;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/seiswork-secure /etc/nginx/sites-enabled/seiswork-secure
sudo rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/seiswork
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx

if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 443/tcp
  sudo ufw allow 80/tcp   # needed for the 80→443 redirect + certbot's HTTP-01 challenge
  echo "✓ ufw: 80/443 opened (nginx itself enforces the IP allowlist above)"
fi

echo ""
echo "✓ Done. Verify:"
echo "    curl -k https://127.0.0.1/api/health          # from an ALLOWED IP → 200"
echo "    curl -k https://127.0.0.1/api/health           # from elsewhere    → connection refused/403"
echo "    journalctl --user -u seiswork-gunicorn -n 30"
