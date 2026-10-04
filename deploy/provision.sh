#!/usr/bin/env bash
# =============================================================================
# MIZAN - one-time provisioning of a fresh server into a live HTTPS demo.
#
# Tested target: Debian 12 (python3.11 from apt). Also handles Ubuntu 24.04
# (Python 3.11 via uv). x86_64 or arm64. Needs >= 8 GB RAM, >= 25 GB disk.
#
# Run as root ON THE SERVER, after deploy/push.sh has copied the project to
# /opt/mizan/app and after the domain's A record points at this server:
#
#   MIZAN_DOMAIN=mizan.example.com MIZAN_ACME_EMAIL=you@example.com \
#       bash /opt/mizan/app/deploy/provision.sh
#
# Safe to re-run: every step checks before it acts and nothing is deleted.
#   1. OS packages, 2 GB swap, firewall (22, 80, 443 only), security updates
#   2. user `mizan`; /opt/mizan/{venv,hf}; /var/lib/mizan; /var/lib/mizan-status
#   3. Python 3.11 venv: CPU-only torch + requirements-ml.txt       (~1 GB)
#   4. BAAI/bge-m3 at the revision pinned in src/mizan/semantic.py   (~2.2 GB)
#   5. data/corpus.sqlite via scripts/setup.sh (download + SHA-256 + fingerprint)
#   6. systemd: mizan.service (restart on crash + prewarm) + watchdog timer
#   7. Caddy with automatic HTTPS for $MIZAN_DOMAIN
# =============================================================================
set -euo pipefail

APP=/opt/mizan/app
VENV=/opt/mizan/venv
HFHOME=/opt/mizan/hf
DOMAIN="${MIZAN_DOMAIN:?set MIZAN_DOMAIN, e.g. MIZAN_DOMAIN=mizan.example.com}"
EMAIL="${MIZAN_ACME_EMAIL:?set MIZAN_ACME_EMAIL for Lets Encrypt expiry notices}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  !! %s\033[0m\n' "$*"; }
as_mizan() { runuser -u mizan -- env HF_HOME="$HFHOME" PYTHONPATH="$APP/src" "$@"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ -f "$APP/app.py" ] || { echo "$APP/app.py not found - run deploy/push.sh from your laptop first"; exit 1; }
cd "$APP"

# ----------------------------------------------------------------------------
step "1/7 OS packages, swap, firewall, security updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q curl ca-certificates gnupg debian-keyring debian-archive-keyring \
    apt-transport-https ufw unattended-upgrades sqlite3 rsync

if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/90-mizan.conf
sysctl -q -p /etc/sysctl.d/90-mizan.conf

mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/mizan.conf
systemctl restart systemd-journald

ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null
ufw status | sed 's/^/  /'

# Security updates daily, never an automatic reboot during judging.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
echo 'Unattended-Upgrade::Automatic-Reboot "false";' > /etc/apt/apt.conf.d/52mizan-no-reboot

# ----------------------------------------------------------------------------
step "2/7 user and directories"
install -d -m 0755 /opt/mizan /etc/mizan
id mizan >/dev/null 2>&1 || useradd --system --home-dir /opt/mizan --shell /usr/sbin/nologin mizan
install -d -o mizan -g mizan -m 0755 "$HFHOME" /var/lib/mizan-status
install -d -o mizan -g mizan -m 0750 /var/lib/mizan
chown -R mizan:mizan "$APP/data"

# ----------------------------------------------------------------------------
step "3/7 Python 3.11 venv with CPU-only PyTorch"
PY=""
if command -v python3.11 >/dev/null 2>&1; then
  apt-get install -y -q python3.11-venv
  PY="$(command -v python3.11)"
elif apt-cache show python3.11-venv >/dev/null 2>&1; then
  apt-get install -y -q python3.11 python3.11-venv
  PY="$(command -v python3.11)"
else
  # e.g. Ubuntu 24.04 (3.12 only): a standalone CPython 3.11 from uv, kept
  # under /opt/mizan/python so the mizan user can run it.
  export UV_INSTALL_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/mizan/python
  command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
  uv python install 3.11
  PY="$(uv python find 3.11)"
fi
echo "  interpreter: $PY ($("$PY" --version))"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
TORCH="$(sed -n 's/^torch==\([^ ]*\).*/\1/p' requirements-ml.txt)"
# The CPU wheel index first: torch from PyPI on Linux pulls several GB of CUDA.
"$VENV/bin/pip" install -q "torch==${TORCH}" --index-url https://download.pytorch.org/whl/cpu
"$VENV/bin/pip" install -q -r requirements-ml.txt
"$VENV/bin/python" -c "import torch, transformers; print('  torch', torch.__version__, '| transformers', transformers.__version__, '| threads', torch.get_num_threads())"

# ----------------------------------------------------------------------------
step "4/7 bge-m3 at the pinned revision"
REV="$(sed -n 's/^MODEL_REVISION = "\([0-9a-f]*\)"/\1/p' src/mizan/semantic.py)"
if [ -f "$HFHOME/hub/models--BAAI--bge-m3/snapshots/$REV/config.json" ]; then
  echo "  already on disk ($REV)"
else
  as_mizan "$VENV/bin/python" scripts/build_embeddings.py --download
fi
if [ ! -f data/embeddings/index.json ]; then
  warn "data/embeddings/index.json is missing - the AI tier will stay OFF and the"
  warn "deterministic path will serve. Copy it from the laptop: deploy/push.sh"
fi

# ----------------------------------------------------------------------------
step "5/7 the approved-translation index (data/corpus.sqlite)"
if [ -f data/corpus.sqlite ] && as_mizan "$VENV/bin/python" -m mizan.eval --check-index >/dev/null 2>&1; then
  echo "  data/corpus.sqlite matches the reference fingerprint - kept"
else
  runuser -u mizan -- env PYTHON="$VENV/bin/python" bash scripts/setup.sh --no-changes
fi
as_mizan "$VENV/bin/python" -c "
import json; from mizan import semantic as S
s = S.status(); print('  AI tier available:', s['semantic_available'], '| model on disk:', s['model_on_disk'], '| index languages:', s['index_languages'])"

# ----------------------------------------------------------------------------
step "6/7 systemd: mizan.service + watchdog timer"
[ -f /etc/mizan/mizan.env ] || { sed -e "s/__DOMAIN__/${DOMAIN}/g" deploy/mizan.env > /etc/mizan/mizan.env; chmod 0644 /etc/mizan/mizan.env; }
install -m 0644 deploy/mizan.service /etc/systemd/system/mizan.service
install -m 0644 deploy/mizan-watchdog.service /etc/systemd/system/mizan-watchdog.service
install -m 0644 deploy/mizan-watchdog.timer /etc/systemd/system/mizan-watchdog.timer
systemctl daemon-reload
systemctl enable -q mizan.service mizan-watchdog.timer
echo "  starting mizan (returns when the model is loaded; ~15-60 s)..."
systemctl restart mizan.service
systemctl start mizan-watchdog.timer
journalctl -u mizan -n 12 --no-pager | sed 's/^/  /'
cat /var/lib/mizan-status/ai.json 2>/dev/null | sed 's/^/  ai.json: /'; echo

# ----------------------------------------------------------------------------
step "7/7 Caddy (automatic HTTPS for $DOMAIN)"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi
sed -e "s/__DOMAIN__/${DOMAIN}/g" -e "s/__ACME_EMAIL__/${EMAIL}/g" deploy/Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl enable -q caddy
systemctl reload caddy 2>/dev/null || systemctl restart caddy

RESOLVED="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
echo "  $DOMAIN resolves to: ${RESOLVED:-NOTHING}   this server: $(hostname -I | awk '{print $1}')"
[ -n "$RESOLVED" ] || warn "DNS for $DOMAIN does not resolve yet - Caddy will keep retrying the certificate."

step "done - smoke test through HTTPS (the first certificate can take ~30 s)"
for _ in $(seq 1 12); do
  if "$VENV/bin/python" deploy/probe.py smoke --base "https://$DOMAIN" --skip-bench; then break; fi
  echo "  ...waiting for the certificate"; sleep 10
done
echo
echo "Next: set up the external monitors (docs/DEPLOY.md, 'Monitoring'), then run from"
echo "your laptop:  python3 deploy/probe.py smoke --base https://$DOMAIN"
