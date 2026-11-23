#!/usr/bin/env bash
# PrintWarden native installer: Debian/Ubuntu/Raspberry Pi OS, Fedora/RHEL,
# Arch, openSUSE. Installs into /opt/printwarden (Python venv), keeps data in
# /var/lib/printwarden and runs as the systemd service "printwarden".
#
#   curl -fsSL https://raw.githubusercontent.com/DIEHARDave/PrintWarden/main/install.sh | sudo bash
#   sudo ./install.sh                 # from a clone: installs that checkout
#
# Options:
#   --version vX.Y.Z   install that release instead of the latest
#   --output DIR       where rendered timelapses go (default /var/lib/printwarden/timelapses)
#   --port N           web page port (default 8095)
#   --no-service       install only, no systemd unit (containers, testing)
#   --uninstall        remove the program and service; keeps data and config
#   --purge            with --uninstall: also delete data and config
#
# Running it again updates the program and keeps settings, data and config.
set -euo pipefail

REPO="DIEHARDave/PrintWarden"
PREFIX=/opt/printwarden
DATA=/var/lib/printwarden
CONF_DIR=/etc/printwarden
SVC_USER=printwarden
OUTPUT=""
PORT=""
VERSION=""
SERVICE=1
UNINSTALL=0
PURGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,18p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"
have_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null; }

# --- uninstall -------------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
  if have_systemd && systemctl list-unit-files printwarden.service >/dev/null 2>&1; then
    systemctl disable --now printwarden.service 2>/dev/null || true
  fi
  rm -f /etc/systemd/system/printwarden.service
  have_systemd && systemctl daemon-reload || true
  rm -rf "$PREFIX"
  if [ "$PURGE" -eq 1 ]; then
    rm -rf "$DATA" "$CONF_DIR"
    userdel "$SVC_USER" 2>/dev/null || true
    say "PrintWarden removed, with its data and config"
  else
    say "PrintWarden removed. Data kept in $DATA, config in $CONF_DIR (--purge deletes them)"
  fi
  exit 0
fi

# --- system packages ---------------------------------------------------------
case "$(uname -m)" in
  x86_64|amd64|aarch64|arm64) ;;
  *) warn "$(uname -m): onnxruntime has no wheels for this CPU, so failure detection will not work (the page and timelapses will)";;
esac

say "Installing system packages (Python 3, venv, ffmpeg)"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip ffmpeg curl ca-certificates tar >/dev/null
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 python3-pip curl tar ca-certificates >/dev/null
  dnf install -y -q ffmpeg >/dev/null 2>&1 || dnf install -y -q ffmpeg-free >/dev/null 2>&1 \
    || warn "ffmpeg not installed: enable RPM Fusion (or EPEL) and install ffmpeg, or timelapses can't render"
elif command -v pacman >/dev/null; then
  pacman -Sy --noconfirm --needed python ffmpeg curl tar ca-certificates >/dev/null
elif command -v zypper >/dev/null; then
  zypper -n -q install python3 curl tar ca-certificates >/dev/null
  zypper -n -q install ffmpeg >/dev/null 2>&1 || zypper -n -q install ffmpeg-7 >/dev/null 2>&1 \
    || warn "ffmpeg not installed: add the Packman repository and install ffmpeg, or timelapses can't render"
else
  warn "no apt, dnf, pacman or zypper: install python3 (3.10+, with venv) and ffmpeg yourself"
fi

PY=$(command -v python3 || true)
[ -n "$PY" ] || die "python3 not found"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer is needed (found $("$PY" -V 2>&1))"

# --- program files -----------------------------------------------------------
SRC=""
HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
if [ -z "$VERSION" ] && [ -n "$HERE" ] && [ -f "$HERE/printwarden/__main__.py" ] && [ -f "$HERE/requirements.txt" ]; then
  SRC="$HERE"
  say "Installing from $SRC"
else
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  if [ -z "$VERSION" ]; then
    VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
      | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
    [ -n "$VERSION" ] || die "could not find the latest release on GitHub"
  fi
  say "Downloading PrintWarden $VERSION"
  curl -fsSL "https://github.com/$REPO/archive/refs/tags/$VERSION.tar.gz" | tar xz -C "$TMP"
  SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)
fi

id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null \
  || useradd -r -d "$DATA" -s /sbin/nologin "$SVC_USER"

mkdir -p "$PREFIX" "$DATA" "$DATA/work" "$CONF_DIR"
rm -rf "$PREFIX/app.new"
mkdir -p "$PREFIX/app.new"
cp -r "$SRC/printwarden" "$SRC/requirements.txt" "$PREFIX/app.new/"
[ -f "$SRC/config.example.yaml" ] && cp "$SRC/config.example.yaml" "$CONF_DIR/config.example.yaml"
find "$PREFIX/app.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$PREFIX/app"
mv "$PREFIX/app.new" "$PREFIX/app"

say "Installing Python packages into $PREFIX/venv"
[ -x "$PREFIX/venv/bin/python" ] || "$PY" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
if ! "$PREFIX/venv/bin/pip" install -q -r "$PREFIX/app/requirements.txt"; then
  # Pinned versions have no wheels for a newer Python: take current ones
  warn "pinned versions failed on $("$PY" -V 2>&1); installing current versions instead"
  "$PREFIX/venv/bin/pip" install -q aiohttp PyYAML onnxruntime numpy Pillow \
    || "$PREFIX/venv/bin/pip" install -q aiohttp PyYAML numpy Pillow
fi
"$PREFIX/venv/bin/python" -c 'import onnxruntime' 2>/dev/null \
  || warn "onnxruntime is not available here: failure detection is off, the page and timelapses work"

# --- settings that survive updates -------------------------------------------
ENV_FILE="$CONF_DIR/printwarden.env"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
# PrintWarden service settings (kept across updates)
PRINTWARDEN_CONFIG=$CONF_DIR/config.yaml
PRINTWARDEN_DATA=$DATA
PRINTWARDEN_WORK=$DATA/work
PRINTWARDEN_OUTPUT=${OUTPUT:-$DATA/timelapses}
EOF
fi
[ -n "$OUTPUT" ] && sed -i "s#^PRINTWARDEN_OUTPUT=.*#PRINTWARDEN_OUTPUT=$OUTPUT#" "$ENV_FILE"
OUT_DIR=$(sed -n 's/^PRINTWARDEN_OUTPUT=//p' "$ENV_FILE")
mkdir -p "$OUT_DIR"
if [ -n "$PORT" ]; then
  if [ -f "$CONF_DIR/config.yaml" ]; then
    warn "--port: set web.port: $PORT in $CONF_DIR/config.yaml yourself"
  else
    printf 'web:\n  port: %s\n' "$PORT" > "$CONF_DIR/config.yaml"
  fi
fi
chown -R "$SVC_USER": "$DATA"
chown "$SVC_USER": "$OUT_DIR" 2>/dev/null || warn "could not chown $OUT_DIR: make it writable for user $SVC_USER"
chmod 640 "$ENV_FILE"; chgrp "$SVC_USER" "$ENV_FILE"

# --- service ------------------------------------------------------------------
if [ "$SERVICE" -eq 1 ] && have_systemd; then
  cat > /etc/systemd/system/printwarden.service <<EOF
[Unit]
Description=PrintWarden (Klipper/Moonraker printer monitor, failure detection, timelapses)
Wants=network-online.target
After=network-online.target

[Service]
User=$SVC_USER
EnvironmentFile=$ENV_FILE
WorkingDirectory=$PREFIX/app
ExecStart=$PREFIX/venv/bin/python -m printwarden serve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable -q printwarden.service
  systemctl restart printwarden.service
  say "Service printwarden started (logs: journalctl -u printwarden -f)"
elif [ "$SERVICE" -eq 1 ]; then
  warn "no systemd: start it with: sudo -u $SVC_USER env \$(cat $ENV_FILE | grep -v '^#' | xargs) $PREFIX/venv/bin/python -m printwarden serve"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
say "Done. Open http://${IP:-<this host>}:${PORT:-8095}/ and click Printers… to add your printers."
echo "    Config (optional): $CONF_DIR/config.yaml (example next to it)"
echo "    Data: $DATA   Timelapses: $OUT_DIR"
echo "    Update: run the installer again.  Remove: sudo $0 --uninstall"
