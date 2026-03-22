#!/usr/bin/env bash
# =============================================================================
#  setup.sh — One-shot developer setup for crypto_payment
#
#  Usage:
#    chmod +x setup.sh
#    ./setup.sh            # full setup
#    ./setup.sh --no-docker  # skip Docker, set up Python env only
# =============================================================================

set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

USE_DOCKER=true
for arg in "$@"; do
  [[ "$arg" == "--no-docker" ]] && USE_DOCKER=false
done

# ─── Sanity checks ────────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || error "python3 not found"
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')
[[ "$PY_VER" -ge 311 ]] || error "Python 3.11+ required (found $(python3 --version))"

if $USE_DOCKER; then
  command -v docker >/dev/null 2>&1 || error "docker not found"
  command -v docker-compose >/dev/null 2>&1 || \
    docker compose version >/dev/null 2>&1   || error "docker-compose not found"
fi

# ─── Virtual environment ──────────────────────────────────────────────────────
info "Creating Python virtual environment …"
if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
  success "Virtual environment created at .venv/"
else
  warn ".venv already exists — skipping creation"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

info "Installing Python dependencies …"
pip install --upgrade pip -q
pip install -r requirements.txt -q
success "Dependencies installed"

# ─── .env file ───────────────────────────────────────────────────────────────
if [[ ! -f ".env" ]]; then
  info "Creating .env from .env.example …"
  cp .env.example .env
  warn "Please edit .env and set BTC_XPUB, BITCOIN_RPC_USER, BITCOIN_RPC_PASSWORD"
else
  warn ".env already exists — skipping"
fi

# ─── Docker services ─────────────────────────────────────────────────────────
if $USE_DOCKER; then
  info "Starting Docker services (db, redis, bitcoind) …"
  # Use either docker compose (v2) or docker-compose (v1)
  DC="docker compose"
  command -v docker-compose >/dev/null 2>&1 && DC="docker-compose"

  $DC up -d db redis bitcoind
  info "Waiting for PostgreSQL to be ready …"
  for i in {1..30}; do
    $DC exec -T db pg_isready -U postgres >/dev/null 2>&1 && break
    sleep 2
    echo -n "."
  done
  echo
  success "PostgreSQL is ready"

  info "Waiting for Redis …"
  for i in {1..15}; do
    $DC exec -T redis redis-cli ping >/dev/null 2>&1 && break
    sleep 1
  done
  success "Redis is ready"
fi

# ─── Database ─────────────────────────────────────────────────────────────────
info "Initialising database schema …"
PYTHONPATH=. python scripts/init_db.py
success "Database schema created"

info "Seeding initial data (coins + demo merchant) …"
PYTHONPATH=. python scripts/seed.py
success "Seed data inserted"

# ─── Done ────────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo
echo "  Start the API service:"
echo "    source .venv/bin/activate"
echo "    PYTHONPATH=. uvicorn services.api.app:app --reload"
echo
echo "  Start the ZMQ Watcher:"
echo "    source .venv/bin/activate"
echo "    PYTHONPATH=. python -m services.watcher.zmq_watcher"
echo
echo "  Or run everything with Docker:"
if $USE_DOCKER; then
  DC="docker compose"
  command -v docker-compose >/dev/null 2>&1 && DC="docker-compose"
  echo "    $DC up --build"
fi
echo
echo "  API docs → http://localhost:8000/docs"
echo
