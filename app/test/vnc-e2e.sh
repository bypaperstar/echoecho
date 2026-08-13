#!/usr/bin/env bash
# VNC chain e2e against a REAL RFB server (TigerVNC Xvnc):
#   Xvnc + xterm  <-TCP-  vnc-proxy.js  <-ws-  renderer/vnc.js (unmodified)
# Proves connect + render + INPUT: the harness types a marker through
# echoVnc.sendKey and png-diff.js asserts the pixels changed (no eyeballing).
# Artifacts kept for inspection: /tmp/vnc-e2e-before.png, /tmp/vnc-e2e-after.png
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS='echoecho'          # VncAuth only uses the first 8 chars: keep it 8
RFB_DISPLAY=':7'
RFB_PORT=5907
PASSFILE="$(mktemp /tmp/vnc-e2e-pass.XXXXXX)"
BEFORE=/tmp/vnc-e2e-before.png
AFTER=/tmp/vnc-e2e-after.png

pids=()
cleanup() {
  local pid
  for pid in "${pids[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  rm -f "$PASSFILE"
  rm -rf /tmp/vnc-smoke-*  # per-run Electron profiles (vnc-smoke-main.js)
}
trap cleanup EXIT

rm -f "$BEFORE" "$AFTER"

echo "$PASS" | vncpasswd -f > "$PASSFILE"

Xvnc "$RFB_DISPLAY" -geometry 1280x800 -depth 24 -rfbport "$RFB_PORT" \
     -SecurityTypes VncAuth -PasswordFile "$PASSFILE" &
pids+=($!)

# wait for the RFB port before pointing anything at it
up=0
for _ in $(seq 1 50); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$RFB_PORT") 2>/dev/null; then
    exec 3>&- 3<&- || true
    up=1
    break
  fi
  sleep 0.2
done
[ "$up" = 1 ] || { echo "vnc-e2e: Xvnc did not come up on :$RFB_PORT" >&2; exit 1; }

# focused xterm on the VNC desktop; tty echo will render the typed marker
DISPLAY="$RFB_DISPLAY" xterm -geometry 120x30+50+50 \
  -e bash -c 'echo ECHOECHO PORTAL E2E; exec sleep 3600' &
pids+=($!)
sleep 1

# electron headless (xvfb-run picks a free X display for the app window)
smoke_once() {
  ECHOECHO_VNC_URL="vnc://:${PASS}@127.0.0.1:${RFB_PORT}" \
    xvfb-run -a "$APP_DIR/node_modules/.bin/electron" --no-sandbox \
    "$APP_DIR/test/vnc-smoke-main.js"
}
if ! smoke_once; then
  # Right after Electron startup the file:// module import of noVNC can fail
  # transiently (code-cache flake) — one retry; the pixel assertions below
  # stay strict.
  echo "vnc-e2e: smoke run failed once, retrying" >&2
  rm -f "$BEFORE" "$AFTER"
  smoke_once
fi

node "$APP_DIR/test/png-diff.js" "$BEFORE" "$AFTER"
echo "vnc-e2e: PASS ($BEFORE / $AFTER)"
