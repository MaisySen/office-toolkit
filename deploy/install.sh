#!/usr/bin/env bash
# Idempotent host setup for doctool.multies.si (Debian/Ubuntu).
# Runs the host steps from docs/DEPLOYMENT.md: packages, users, code,
# venv, Keycloak, env files, systemd units, Caddy. Safe to re-run.
# Prereqs the script cannot create: the DNS A/AAAA record for
# doctool.multies.si pointing at this host, and filled
# /etc/office-toolkit/*.env files (see deploy/env.example).
set -euo pipefail

KC_VERSION="${KC_VERSION:-26.0.8}"
APP_DIR=/opt/office-toolkit
KC_DIR=/opt/keycloak
ENV_DIR=/etc/office-toolkit
DATA_DIR=/var/lib/office-toolkit

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq caddy openjdk-17-jre-headless python3 python3-venv \
    python3-pip postgresql curl

id -u keycloak >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin keycloak
id -u toolkit  >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin toolkit

# Toolkit code: install.sh lives in the repo, so the repo root is two
# levels up from this file.
SRC="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$APP_DIR" "$DATA_DIR"
rsync -a --delete --exclude .git --exclude .deploy --exclude node_modules \
    --exclude '*/node_modules' --exclude __pycache__ "$SRC/" "$APP_DIR/" \
    2>/dev/null || cp -a "$SRC/." "$APP_DIR/"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" gunicorn
chown -R toolkit:toolkit "$APP_DIR" "$DATA_DIR"

# Keycloak
if [ ! -x "$KC_DIR/bin/kc.sh" ]; then
    curl -fsSL "https://github.com/keycloak/keycloak/releases/download/${KC_VERSION}/keycloak-${KC_VERSION}.tar.gz" \
        -o /tmp/keycloak.tar.gz
    tar -xzf /tmp/keycloak.tar.gz -C /opt
    mv "/opt/keycloak-${KC_VERSION}" "$KC_DIR"
    rm /tmp/keycloak.tar.gz
fi
chown -R keycloak:keycloak "$KC_DIR"

# PostgreSQL database for Keycloak (idempotent)
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='keycloak'" \
    | grep -q 1 || sudo -u postgres createuser keycloak
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='keycloak'" \
    | grep -q 1 || sudo -u postgres createdb -O keycloak keycloak

# Env files must exist and be filled before services start.
for f in toolkit.env keycloak.env; do
    if [ ! -f "$ENV_DIR/$f" ]; then
        mkdir -p "$ENV_DIR"
        echo "MISSING: $ENV_DIR/$f - copy from deploy/env.example, fill, chmod 600" >&2
        exit 2
    fi
    if grep -q 'generate-a-long-random-string\|database-password\|bootstrap-admin-password\|web-client-secret-from-keycloak' "$ENV_DIR/$f"; then
        echo "UNFILLED placeholder in $ENV_DIR/$f" >&2
        exit 2
    fi
done
chmod 600 "$ENV_DIR"/*.env

# Keycloak optimized build needs its env (KC_DB_* etc.) present.
set -a; . "$ENV_DIR/keycloak.env"; set +a
sudo -u keycloak -E "$KC_DIR/bin/kc.sh" build

install -m 644 "$APP_DIR/deploy/keycloak.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/office-toolkit.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now keycloak office-toolkit caddy
systemctl reload caddy

echo "INSTALL OK - next: set env vars from keycloak.env + toolkit.env, then"
echo "run: $APP_DIR/venv/bin/python $APP_DIR/scripts/provision_keycloak.py"
