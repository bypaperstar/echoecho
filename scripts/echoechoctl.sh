#!/usr/bin/env bash
# echoechoctl — one entry point for echoecho's lifecycle on the Mac. Used by the
# echoecho.app control panel's buttons and runnable by hand:
#
#   bash scripts/echoechoctl.sh status|start-daemon|stop-daemon|restart-daemon
#                           boot-vm|stop-vm|reset-vm
#                           build-app|install-app|start-app|stop-app|update
#
# Daemon env pins (audio device etc.) live in ~/.echoecho/daemon.env — one
# KEY=VALUE per line, sourced at daemon start so the app's buttons launch the
# daemon exactly like you would.
#
# Lifecycle: the app and the wake-word daemon are tied. start-daemon tethers
# the daemon to the app process (starting the app first if needed); closing or
# force-quitting the app takes the daemon down with it. A bare
# `python echoecho.py --voice` in a terminal stays untethered for debugging.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO/app"
VM_NAME="${ECHOECHO_VM_NAME:-echoecho-vm}"
GOLDEN="${ECHOECHO_VM_GOLDEN:-echoecho-golden}"
DAEMON_ENV="$HOME/.echoecho/daemon.env"
DAEMON_LOG="/tmp/echoecho-daemon.log"
APP_LOG="/tmp/echoecho-orb.log"
START_LOCK="/tmp/echoecho-start-daemon.lock"

# tools live in per-user places (nvm node, ~/.local/bin lume); resolve here so
# double-clicked app buttons (no shell profile) still find them
PATH="$HOME/.local/bin:$PATH"
if ! command -v node >/dev/null 2>&1 && [ -d "$HOME/.nvm/versions/node" ]; then
  latest="$(ls -1 "$HOME/.nvm/versions/node" | sort -V | tail -1)"
  PATH="$HOME/.nvm/versions/node/$latest/bin:$PATH"
fi
export PATH

# The deployed version: VERSION holds MAJOR.MINOR (bumped deliberately); the
# patch is the git commit count, so every deploy of new commits gets a new
# number automatically. A full MAJOR.MINOR.PATCH written into VERSION wins.
repo_version() {
  ver="$(tr -d '[:space:]' < "$REPO/VERSION" 2>/dev/null || echo 0.0.0)"
  case "$ver" in
    *.*.*) ;;  # explicit full version pinned by hand
    *) ver="$ver.$(git -C "$REPO" rev-list --count HEAD 2>/dev/null || echo 0)" ;;
  esac
  echo "$ver"
}

daemon_pid() { pgrep -f "echoecho\.py --voice" | head -1 || true; }
# The packaged app's main process only — helpers live under Contents/Frameworks
# so this pattern can't match them. Empty for a dev `electron .` orb: that orb
# starts its own daemon and passes ECHOECHO_TETHER_PID itself.
app_pid() { pgrep -f "echoecho\.app/Contents/MacOS/echoecho" | head -1 || true; }
app_bundle() {
  if [ -d "/Applications/echoecho.app" ]; then echo "/Applications/echoecho.app";
  elif [ -d "$HOME/Applications/echoecho.app" ]; then echo "$HOME/Applications/echoecho.app";
  else echo ""; fi
}

cmd_status() {
  pid="$(daemon_pid)"
  [ -n "$pid" ] && echo "daemon: running (pid $pid)" || echo "daemon: stopped"
  curl -s -o /dev/null -m 2 "http://127.0.0.1:${ECHOECHO_VIEWER_PORT:-8765}/transcript" \
    && echo "viewer: up" || echo "viewer: down"
  if command -v lume >/dev/null 2>&1; then
    lume ls 2>/dev/null | awk -v vm="$VM_NAME" '$1 == vm {print "vm: " $7}' | head -1
  else
    echo "vm: lume not installed"
  fi
  pgrep -f "echoecho\.app/Contents/MacOS/echoecho" >/dev/null && echo "app: running" || echo "app: not running"
}

cmd_start_daemon() {
  [ -n "$(daemon_pid)" ] && { echo "daemon already running (pid $(daemon_pid))"; return 0; }
  # The app and the wake word live and die together: the daemon tethers to the
  # app process (ECHOECHO_TETHER_PID) and exits when it disappears — quit or
  # force quit alike — so a listening wake word always means a Dock icon. The
  # orb passes its own pid; from a terminal we tether to the running app, or
  # start the app and let its launch sequence bring up its own tethered daemon.
  if [ -z "${ECHOECHO_TETHER_PID:-}" ]; then
    ECHOECHO_TETHER_PID="$(app_pid)"
    if [ -z "$ECHOECHO_TETHER_PID" ]; then
      echo "app not running — starting it (the app launches the daemon tethered to itself)"
      cmd_start_app
      for _ in $(seq 1 30); do sleep 1; [ -n "$(daemon_pid)" ] && break; done
      [ -n "$(daemon_pid)" ] && { echo "daemon started (pid $(daemon_pid)) via the app"; return 0; }
      echo "daemon did not come up via the app — tails of $APP_LOG and $DAEMON_LOG:"
      tail -5 "$APP_LOG" "$DAEMON_LOG" 2>/dev/null || true
      exit 1
    fi
  fi
  export ECHOECHO_TETHER_PID
  # Serialize the check->spawn window: app launch, second-instance, and a
  # terminal start-daemon can race, and two daemons means two open mics.
  # mkdir is atomic; the EXIT trap releases it on every path out.
  if ! mkdir "$START_LOCK" 2>/dev/null; then
    echo "another start-daemon is in flight — waiting for its daemon"
    for _ in $(seq 1 15); do sleep 1; [ -n "$(daemon_pid)" ] && break; done
    [ -n "$(daemon_pid)" ] && { echo "daemon started (pid $(daemon_pid))"; return 0; }
    echo "no daemon appeared — stealing stale lock $START_LOCK"
    rmdir "$START_LOCK" 2>/dev/null || true
    mkdir "$START_LOCK" 2>/dev/null || { echo "could not take start lock"; exit 1; }
  fi
  trap 'rmdir "$START_LOCK" 2>/dev/null || true' EXIT
  [ -n "$(daemon_pid)" ] && { echo "daemon already running (pid $(daemon_pid))"; return 0; }
  cd "$REPO"
  # shellcheck disable=SC1090
  [ -f "$DAEMON_ENV" ] && set -a && . "$DAEMON_ENV" && set +a
  : "${ECHOECHO_SANDBOX:=vm}"; export ECHOECHO_SANDBOX
  ( nohup .venv/bin/python -u echoecho.py --voice </dev/null >"$DAEMON_LOG" 2>&1 & )
  sleep 3
  [ -n "$(daemon_pid)" ] && echo "daemon started (pid $(daemon_pid)), log $DAEMON_LOG" \
    || { echo "daemon FAILED to start — tail of $DAEMON_LOG:"; tail -5 "$DAEMON_LOG"; exit 1; }
}

cmd_stop_daemon() {
  pid="$(daemon_pid)"
  [ -z "$pid" ] && { echo "daemon not running"; return 0; }
  kill "$pid" 2>/dev/null || true
  sleep 2
  # escalate on the SAME pid only: a detached stop (app quit) must never 9-kill
  # a fresh daemon that a relaunched app started inside our 2s grace window
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  echo "daemon stopped"
}

cmd_boot_vm() {
  command -v lume >/dev/null || { echo "lume not installed"; exit 1; }
  if ! lume get "$VM_NAME" >/dev/null 2>&1; then
    echo "cloning $GOLDEN -> $VM_NAME"
    lume clone "$GOLDEN" "$VM_NAME"
  fi
  if lume ls 2>/dev/null | awk -v vm="$VM_NAME" '$1 == vm && $7 == "running" {found=1} END {exit !found}'; then
    echo "vm already running"; return 0
  fi
  ( nohup lume run "$VM_NAME" --no-display --shared-dir "$REPO/workspace:rw" </dev/null >/tmp/lume-echoecho-vm.log 2>&1 & )
  echo "vm booting (log /tmp/lume-echoecho-vm.log)"
}

cmd_stop_vm() { lume stop "$VM_NAME" 2>/dev/null || true; echo "vm stopped"; }

cmd_reset_vm() {
  # the ladder's "undo": throw the scratch Mac away; next boot re-clones golden
  lume stop "$VM_NAME" 2>/dev/null || true
  lume delete "$VM_NAME" --force 2>/dev/null || true
  echo "vm deleted — re-cloning fresh from $GOLDEN"
  cmd_boot_vm
}

cmd_build_app() {
  cd "$APP_DIR"
  node tools/make-iconset.js
  iconutil -c icns build/echoecho.iconset -o build/icon.icns
  # bake the repo location + version into the bundle so the packaged app can
  # find its scripts and show what it's running
  node -e "require('fs').writeFileSync('runtime-config.json', JSON.stringify({
    repoRoot: '$REPO',
    version: '$(repo_version)',
    sha: require('child_process').execSync('git rev-parse --short HEAD', {cwd: '$REPO'}).toString().trim(),
    updatedAt: require('child_process').execSync('git log -1 --format=%cI', {cwd: '$REPO'}).toString().trim(),
    builtAt: new Date().toISOString() }, null, 2))"
  npx --yes @electron/packager . echoecho --platform=darwin --arch=arm64 \
    --app-bundle-id app.echoecho.desktop \
    --icon build/icon.icns --out dist --overwrite \
    --ignore '^/prototypes' --ignore '^/test' --ignore '^/dist' --ignore '^/build/echoecho.iconset' \
    >/dev/null
  rm -f runtime-config.json
  echo "built $APP_DIR/dist/echoecho-darwin-arm64/echoecho.app"
}

cmd_install_app() {
  cmd_build_app
  target="/Applications/echoecho.app"
  [ -w "/Applications" ] || target="$HOME/Applications/echoecho.app"
  mkdir -p "$(dirname "$target")"
  pkill -f "echoecho\.app/Contents/MacOS/echoecho" 2>/dev/null || true
  # dev orb: its cmdline is relative, so match the stable node_modules paths
  pkill -f "node_modules/\.bin/electron" 2>/dev/null || true
  pkill -f "node_modules/electron/dist/Electron\.app" 2>/dev/null || true
  # wait for the old instance to actually exit: `open` on a bundle id whose
  # process is still dying gets coalesced into it and no new app launches
  for _ in $(seq 1 10); do [ -z "$(app_pid)" ] && break; sleep 1; done
  rm -rf "$target"
  ditto "$APP_DIR/dist/echoecho-darwin-arm64/echoecho.app" "$target"
  echo "installed $target"
  open "$target"
}

cmd_start_app() {
  b="$(app_bundle)"
  [ -n "$b" ] && open "$b" || ( cd "$APP_DIR" && ( nohup ./node_modules/.bin/electron . </dev/null >"$APP_LOG" 2>&1 & ) )
}

cmd_stop_app() {
  pkill -f "echoecho\.app/Contents/MacOS/echoecho" 2>/dev/null || true
  pkill -f "node_modules/\.bin/electron" 2>/dev/null || true
  pkill -f "node_modules/electron/dist/Electron\.app" 2>/dev/null || true
  echo "app stopped"
}

LIVEWRITER_LOG="/tmp/echoecho-livewriter.log"

cmd_live_writer() {
  # Standalone by design: the Live Writer server shares nothing with the
  # daemon/orchestrator. Idempotent — if it's already listening we just open
  # the page.
  port="${LIVEWRITER_PORT:-8799}"
  url="http://127.0.0.1:${port}/"
  if ! curl -s -o /dev/null -m 2 "${url}healthz"; then
    cd "$REPO"
    ( nohup .venv/bin/python -u -m livewriter --port "$port" </dev/null >"$LIVEWRITER_LOG" 2>&1 & )
    for _ in $(seq 1 20); do
      curl -s -o /dev/null -m 2 "${url}healthz" && break
      sleep 0.5
    done
    if ! curl -s -o /dev/null -m 2 "${url}healthz"; then
      echo "live writer FAILED to start — tail of $LIVEWRITER_LOG:"
      tail -5 "$LIVEWRITER_LOG" 2>/dev/null || true
      exit 1
    fi
  fi
  command -v open >/dev/null 2>&1 && open "$url"
  echo "live writer: $url"
}

cmd_stop_live_writer() {
  pid="$(pgrep -f 'python[0-9.]* -u -m livewriter' | head -1 || true)"
  [ -z "$pid" ] && { echo "live writer not running"; return 0; }
  kill "$pid" 2>/dev/null || true
  echo "live writer stopped (pid $pid)"
}

cmd_update() {
  # called detached from the app's Update button (the app quits right after),
  # so wait for it to exit before swapping its bundle
  sleep 2
  cd "$REPO"
  was_daemon=""
  [ -n "$(daemon_pid)" ] && was_daemon=1
  git pull --ff-only origin main
  ( cd app && npm install --no-audit --no-fund >/dev/null )
  .venv/bin/pip install -q -r requirements-mac.txt || true
  # stop only: install-app reopens the new bundle, and the app launch starts a
  # fresh daemon tethered to the new app process (never to the dying old one)
  if [ -n "$was_daemon" ]; then cmd_stop_daemon; fi
  cmd_install_app
  echo "update complete: v$(repo_version) ($(git rev-parse --short HEAD))"
}

case "${1:-}" in
  status)          cmd_status ;;
  start-daemon)    cmd_start_daemon ;;
  stop-daemon)     cmd_stop_daemon ;;
  restart-daemon)  cmd_stop_daemon; cmd_start_daemon ;;
  boot-vm)         cmd_boot_vm ;;
  stop-vm)         cmd_stop_vm ;;
  reset-vm)        cmd_reset_vm ;;
  build-app)       cmd_build_app ;;
  install-app)     cmd_install_app ;;
  start-app)       cmd_start_app ;;
  stop-app)        cmd_stop_app ;;
  update)          cmd_update ;;
  live-writer)     cmd_live_writer ;;
  stop-live-writer) cmd_stop_live_writer ;;
  *) echo "usage: echoechoctl.sh {status|start-daemon|stop-daemon|restart-daemon|boot-vm|stop-vm|reset-vm|build-app|install-app|start-app|stop-app|update|live-writer|stop-live-writer}"; exit 2 ;;
esac
