#!/usr/bin/env bash
# One-shot setup of the production stack on a fresh Ubuntu VM
# (DigitalOcean droplet or EC2). Run from the repo checkout:
#
#   git clone <repo> seats && cd seats
#   sudo ./deploy/bootstrap.sh                    # HTTPS at https://<ip-dashed>.sslip.io
#   sudo ./deploy/bootstrap.sh seats.example.com  # HTTPS on your own domain (A record -> this VM)
#   sudo ./deploy/bootstrap.sh :80                # plain HTTP on the IP, no TLS
#
# Re-running is safe: existing .env and secrets are kept.
# Cloud firewall / security group must allow inbound 22, 80, 443.
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

log() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------- docker
if ! command -v docker >/dev/null 2>&1; then
  log "installing docker"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null

# ---------------------------------------------------------------- kernel limits for a stampede
log "tuning kernel network limits"
cat > /etc/sysctl.d/99-seats.conf <<'EOF'
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.ip_local_port_range = 1024 65000
net.ipv4.tcp_fin_timeout = 15
net.core.netdev_max_backlog = 16384
fs.file-max = 2097152
EOF
sysctl --system >/dev/null

# ---------------------------------------------------------------- site address
PUBLIC_IP="$(curl -fsS --max-time 5 https://checkip.amazonaws.com || curl -fsS --max-time 5 https://ifconfig.me || true)"
PUBLIC_IP="$(echo "$PUBLIC_IP" | tr -d '[:space:]')"
SITE_ADDRESS="${1:-}"
if [ -z "$SITE_ADDRESS" ]; then
  [ -n "$PUBLIC_IP" ] || { echo "could not detect public IP; pass a domain or :80" >&2; exit 1; }
  SITE_ADDRESS="${PUBLIC_IP//./-}.sslip.io"   # wildcard DNS -> real Let's Encrypt cert, no domain needed
fi

# ---------------------------------------------------------------- secrets (.env, kept across runs)
rand() { openssl rand -hex "${1:-24}"; }
NPROC="$(nproc)"
if [ ! -f .env ]; then
  log "generating .env with fresh secrets"
  WORKERS="$NPROC"; [ "$WORKERS" -gt 4 ] && WORKERS=4
  POOL=$(( 160 / WORKERS )); [ "$POOL" -gt 20 ] && POOL=20
  cat > .env <<EOF
SITE_ADDRESS=$SITE_ADDRESS
POSTGRES_PASSWORD=$(rand 24)
ADMIN_TOKEN=$(rand 24)
JWT_SECRET=$(rand 32)
GRAFANA_ADMIN_PASSWORD=$(rand 12)
LOGS_USER=viewer
LOGS_PASSWORD=$(rand 10)
WEB_CONCURRENCY=$WORKERS
DB_POOL_MAX=$POOL
UPSTREAM_MAX_CONNS=256
LOG_LEVEL=INFO
EOF
  chmod 600 .env
else
  log "keeping existing .env (SITE_ADDRESS updated to $SITE_ADDRESS)"
  sed -i "s|^SITE_ADDRESS=.*|SITE_ADDRESS=$SITE_ADDRESS|" .env
fi
set -a; . ./.env; set +a

# ---------------------------------------------------------------- basic auth for /logs
if [ ! -f deploy/caddy/auth.caddy ]; then
  log "creating basic-auth for /logs"
  HASH="$(docker run --rm caddy:2.8-alpine caddy hash-password --plaintext "$LOGS_PASSWORD")"
  printf 'basic_auth {\n\t%s %s\n}\n' "$LOGS_USER" "$HASH" > deploy/caddy/auth.caddy
fi

# ---------------------------------------------------------------- optional host firewall
if [ "${UFW:-0}" = "1" ] && command -v ufw >/dev/null 2>&1; then
  log "configuring ufw (22, 80, 443)"
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443 >/dev/null
  ufw --force enable >/dev/null
fi

# ---------------------------------------------------------------- start
log "building and starting the stack"
docker compose -f docker-compose.prod.yml up -d --build --remove-orphans

case "$SITE_ADDRESS" in
  :80) BASE="http://${PUBLIC_IP:-localhost}";;
  :*)  BASE="http://${PUBLIC_IP:-localhost}$SITE_ADDRESS";;
  *)   BASE="https://$SITE_ADDRESS";;
esac

log "waiting for $BASE/readyz (first HTTPS cert can take ~30s)"
for _ in $(seq 1 90); do
  if curl -fsS --max-time 5 "$BASE/readyz" >/dev/null 2>&1; then OK=1; break; fi
  sleep 2
done
[ "${OK:-0}" = 1 ] || { echo "not ready yet; check: docker compose -f docker-compose.prod.yml logs" >&2; exit 1; }

cat <<EOF

Service is up.
  API        $BASE
  Health     $BASE/healthz  |  $BASE/readyz
  Metrics    $BASE/metrics
  Dashboard  $BASE/grafana/        (anonymous viewer; admin / $GRAFANA_ADMIN_PASSWORD)
  Alerts     $BASE/prometheus/alerts
  Logs       $BASE/logs/           ($LOGS_USER / $LOGS_PASSWORD)
  Admin token for POST /shows:     $ADMIN_TOKEN

Burst test from your laptop:
  ./burst.sh $BASE --admin-token $ADMIN_TOKEN
EOF
