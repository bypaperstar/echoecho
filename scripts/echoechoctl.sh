#!/usr/bin/env bash
# echoechoctl — one entry point for echoecho's lifecycle on the Mac. Used by the
# echoecho.app control panel's buttons and runnable by hand:
#
#   bash scripts/echoechoctl.sh status|start-daemon|stop-daemon|restart-daemon
#                           boot-vm|stop-vm|reset-vm
#                           build-app|install-app|start-app|stop-app|update
#                           diagnostics|doctor|logs
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
STATE_DIR="${ECHOECHO_STATE_DIR:-$HOME/.echoecho}"
DIAGNOSTICS_DIR="${ECHOECHO_DIAGNOSTICS_DIR:-$STATE_DIR/diagnostics}"
CONSOLE_DIR="$DIAGNOSTICS_DIR/console"
DAEMON_ENV="$STATE_DIR/daemon.env"
DAEMON_LOG="$CONSOLE_DIR/daemon-current.log"
APP_LOG="$CONSOLE_DIR/orb-current.log"
VM_LOG="$CONSOLE_DIR/vm-current.log"
LIVEWRITER_LOG="$CONSOLE_DIR/livewriter-current.log"
UPDATE_LOG="$CONSOLE_DIR/update-current.log"
START_LOCK="/tmp/echoecho-start-daemon.lock"
export ECHOECHO_DIAGNOSTICS_DIR="$DIAGNOSTICS_DIR"

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

# Console output complements the structured JSONL stream. Each detached launch
# gets a private, size-bounded log ring plus a convenient <component>-current
# symlink. Structured diagnostics remain the preferred, redacted source.
ensure_log_dirs() {
  umask 077
  diagnostics_existed=0; [ -d "$DIAGNOSTICS_DIR" ] && diagnostics_existed=1
  mkdir -p "$DIAGNOSTICS_DIR"
  if [ -L "$CONSOLE_DIR" ]; then
    echo "refusing symlink console directory: $CONSOLE_DIR" >&2
    return 1
  fi
  if [ -e "$CONSOLE_DIR" ] && [ ! -d "$CONSOLE_DIR" ]; then
    echo "console log path is not a directory: $CONSOLE_DIR" >&2
    return 1
  fi
  console_existed=0; [ -d "$CONSOLE_DIR" ] && console_existed=1
  mkdir -p "$CONSOLE_DIR"
  if [ -L "$CONSOLE_DIR" ] || [ ! -d "$CONSOLE_DIR" ]; then
    echo "console directory changed while opening: $CONSOLE_DIR" >&2
    return 1
  fi
  # Preserve intentional sharing policy on operator-supplied existing dirs;
  # newly created dirs and every log file are private.
  [ "$diagnostics_existed" -eq 1 ] || chmod 700 "$DIAGNOSTICS_DIR" 2>/dev/null || true
  [ "$console_existed" -eq 1 ] || chmod 700 "$CONSOLE_DIR" 2>/dev/null || true
}

new_console_log() {
  component="$1"
  if ! ensure_log_dirs; then
    echo "console logging disabled: log directory unavailable" >&2
    echo /dev/null
    return 0
  fi
  create_python="$(diagnostics_python 2>/dev/null || true)"
  if [ -n "$create_python" ] && [ -r "$REPO/scripts/console_capture.py" ]; then
    if log="$("$create_python" "$REPO/scripts/console_capture.py" \
        --create-dir "$CONSOLE_DIR" --component "$component")"; then
      echo "$log"
      return 0
    fi
  fi
  # Console capture is observability, never a launch dependency. If the
  # bounded helper is unavailable or retention cannot be enforced, discard
  # raw output instead of adding an unbounded fallback file.
  echo "console logging disabled: bounded capture unavailable" >&2
  echo /dev/null
}

latest_console_log() {
  component="$1"
  [ -d "$CONSOLE_DIR" ] && [ ! -L "$CONSOLE_DIR" ] || return 1
  python="$(diagnostics_python 2>/dev/null || true)"
  [ -n "$python" ] && [ -r "$REPO/scripts/console_capture.py" ] || return 1
  "$python" "$REPO/scripts/console_capture.py" \
    --latest-dir "$CONSOLE_DIR" --component "$component"
}

diagnostics_python() {
  if [ -x "$REPO/.venv/bin/python" ]; then
    echo "$REPO/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    echo "python3 is required for diagnostics" >&2
    return 1
  fi
}

# Put the real command on the producer side of a pipe rather than underneath a
# Python launcher. That preserves the exact command line used by daemon_pid and
# the existing pgrep/pkill controls. If Python is unavailable, preserve the
# requested component and discard raw output rather than let a file grow.
start_console_command() {
  local log capture_python
  log="$1"
  shift
  capture_python="$(diagnostics_python 2>/dev/null || true)"
  if [ -n "$capture_python" ] && [ -r "$REPO/scripts/console_capture.py" ]; then
    ( nohup "$@" </dev/null 2>&1 \
        | nohup "$capture_python" -u "$REPO/scripts/console_capture.py" --log "$log" \
          >/dev/null 2>&1 & )
  else
    echo "console rotation unavailable; raw output discarded" >>"$log"
    ( nohup "$@" </dev/null >/dev/null 2>&1 & )
  fi
}

run_console_command_sync() {
  local log capture_python producer_status
  log="$1"
  shift
  capture_python="$(diagnostics_python 2>/dev/null || true)"
  set +e
  if [ -n "$capture_python" ] && [ -r "$REPO/scripts/console_capture.py" ]; then
    "$@" 2>&1 \
      | "$capture_python" -u "$REPO/scripts/console_capture.py" --log "$log"
    producer_status="${PIPESTATUS[0]}"
  else
    echo "console rotation unavailable; raw output discarded" >>"$log"
    "$@" >/dev/null 2>&1
    producer_status="$?"
  fi
  set -e
  return "$producer_status"
}

safe_console_tail() {
  local log lines raw python
  log="$1"
  lines="$2"
  raw="${3:-}"
  python="$(diagnostics_python 2>/dev/null || true)"
  if [ -n "$python" ] && [ -r "$REPO/scripts/console_capture.py" ]; then
    if [ "$raw" = "--raw" ]; then
      "$python" "$REPO/scripts/console_capture.py" \
        --log "$log" --tail "$lines" --raw-tail
    else
      "$python" "$REPO/scripts/console_capture.py" --log "$log" --tail "$lines"
    fi
  else
    echo "python3 is required to read console logs safely" >&2
    return 1
  fi
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
  echo "diagnostics: $DIAGNOSTICS_DIR"
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
      for component in orb daemon; do
        failed_log="$(latest_console_log "$component" || true)"
        if [ -n "$failed_log" ]; then
          echo "==> $failed_log <=="
          safe_console_tail "$failed_log" 5 2>/dev/null || true
        fi
      done
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
  # A daemon-specific diagnostics root in daemon.env should hold its console
  # output too, rather than splitting one reproduction across two locations.
  if [ -n "$ECHOECHO_DIAGNOSTICS_DIR" ] \
      && [ "$ECHOECHO_DIAGNOSTICS_DIR" != "$DIAGNOSTICS_DIR" ]; then
    DIAGNOSTICS_DIR="$ECHOECHO_DIAGNOSTICS_DIR"
    CONSOLE_DIR="$DIAGNOSTICS_DIR/console"
  elif [ -z "$ECHOECHO_DIAGNOSTICS_DIR" ]; then
    ECHOECHO_DIAGNOSTICS_DIR="$DIAGNOSTICS_DIR"
    export ECHOECHO_DIAGNOSTICS_DIR
  fi
  : "${ECHOECHO_SANDBOX:=vm}"; export ECHOECHO_SANDBOX
  DAEMON_LOG="$(new_console_log daemon)"
  start_console_command "$DAEMON_LOG" .venv/bin/python -u echoecho.py --voice
  sleep 3
  [ -n "$(daemon_pid)" ] && echo "daemon started (pid $(daemon_pid)), log $DAEMON_LOG" \
    || { echo "daemon FAILED to start — tail of $DAEMON_LOG:"; safe_console_tail "$DAEMON_LOG" 5 || true; exit 1; }
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
  VM_LOG="$(new_console_log vm)"
  start_console_command "$VM_LOG" \
    lume run "$VM_NAME" --no-display --shared-dir "$REPO/workspace:rw"
  echo "vm booting (log $VM_LOG)"
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
  if [ -n "$b" ]; then
    open "$b"
  else
    APP_LOG="$(new_console_log orb)"
    ( cd "$APP_DIR" && start_console_command "$APP_LOG" ./node_modules/.bin/electron . )
    echo "orb starting (log $APP_LOG)"
  fi
}

cmd_stop_app() {
  pkill -f "echoecho\.app/Contents/MacOS/echoecho" 2>/dev/null || true
  pkill -f "node_modules/\.bin/electron" 2>/dev/null || true
  pkill -f "node_modules/electron/dist/Electron\.app" 2>/dev/null || true
  echo "app stopped"
}

cmd_live_writer() {
  # Standalone by design: the Live Writer server shares nothing with the
  # daemon/orchestrator. Idempotent — if it's already listening we just open
  # the page.
  port="${LIVEWRITER_PORT:-8799}"
  url="http://127.0.0.1:${port}/"
  if ! curl -s -o /dev/null -m 2 "${url}healthz"; then
    cd "$REPO"
    LIVEWRITER_LOG="$(new_console_log livewriter)"
    start_console_command "$LIVEWRITER_LOG" \
      .venv/bin/python -u -m livewriter --port "$port"
    for _ in $(seq 1 20); do
      curl -s -o /dev/null -m 2 "${url}healthz" && break
      sleep 0.5
    done
    if ! curl -s -o /dev/null -m 2 "${url}healthz"; then
      echo "live writer FAILED to start — tail of $LIVEWRITER_LOG:"
      safe_console_tail "$LIVEWRITER_LOG" 5 2>/dev/null || true
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
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  # Escalate only the pid we signalled. A newly launched Live Writer must not
  # be caught by a second pgrep while this stop command is winding down.
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
  fi
  if kill -0 "$pid" 2>/dev/null; then
    echo "live writer FAILED to stop (pid $pid)" >&2
    return 1
  fi
  echo "live writer stopped (pid $pid)"
}

cmd_update() {
  # called detached from the app's Update button (the app quits right after),
  # so wait for it to exit before swapping its bundle
  UPDATE_LOG="$(new_console_log update)"
  echo "update running; log $UPDATE_LOG"
  run_console_command_sync "$UPDATE_LOG" \
    bash "$REPO/scripts/echoechoctl.sh" _update-body
}

install_python_requirements() {
  python="$REPO/.venv/bin/python"
  if [ ! -x "$python" ]; then
    echo "project virtualenv is missing: $python" >&2
    return 1
  fi
  if ! "$python" -m pip --version >/dev/null 2>&1; then
    echo "virtualenv pip is missing; bootstrapping with ensurepip"
    "$python" -m ensurepip --upgrade >/dev/null
  fi
  "$python" -m pip install -q -r "$REPO/requirements-mac.txt"
}

cmd_update_body() {
  echo "update started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  trap 'status=$?; echo "update finished at $(date -u +%Y-%m-%dT%H:%M:%SZ), status=$status"' EXIT
  sleep 2
  cd "$REPO"
  was_daemon=""
  [ -n "$(daemon_pid)" ] && was_daemon=1
  git pull --ff-only origin main
  ( cd app && npm install --no-audit --no-fund >/dev/null )
  install_python_requirements
  # stop only: install-app reopens the new bundle, and the app launch starts a
  # fresh daemon tethered to the new app process (never to the dying old one)
  if [ -n "$was_daemon" ]; then cmd_stop_daemon; fi
  # The pull may have rewritten this very script, but bash keeps executing
  # the PRE-pull function bodies (observed: a version-bake change didn't
  # apply to the build its own update produced). Run the rebuild — and the
  # version banner — from the fresh copy on disk.
  bash "$REPO/scripts/echoechoctl.sh" install-app
  echo "update complete: v$(bash "$REPO/scripts/echoechoctl.sh" version) ($(git rev-parse --short HEAD))"
}

cmd_diagnostics() {
  python="$(diagnostics_python)"
  "$python" "$REPO/scripts/diagnostics.py" --dir "$DIAGNOSTICS_DIR" "$@"
}

cmd_doctor() {
  python="$(diagnostics_python)"
  "$python" "$REPO/scripts/diagnostics.py" --dir "$DIAGNOSTICS_DIR" --doctor "$@"
}

cmd_logs() {
  component="${1:-all}"
  lines="${2:-100}"
  raw="${3:-}"
  case "$lines" in ''|*[!0-9]*) echo "lines must be a positive integer" >&2; return 2 ;; esac
  [ "$lines" -gt 0 ] || { echo "lines must be a positive integer" >&2; return 2; }
  case "$raw" in ''|--raw) ;; *) echo "usage: echoechoctl.sh logs [component] [lines] [--raw]" >&2; return 2 ;; esac
  case "$component" in
    all) components="daemon orb vm livewriter update" ;;
    daemon|orb|vm|livewriter|update) components="$component" ;;
    *) echo "usage: echoechoctl.sh logs [daemon|orb|vm|livewriter|update|all] [lines]" >&2; return 2 ;;
  esac
  found=0
  for name in $components; do
    log="$(latest_console_log "$name" || true)"
    [ -n "$log" ] || continue
    found=1
    echo "== $name: $log =="
    safe_console_tail "$log" "$lines" "$raw" 2>/dev/null \
      || echo "console log could not be read safely" >&2
  done
  [ "$found" -eq 1 ] || echo "no console logs found under $CONSOLE_DIR"
  echo "structured diagnostics: $DIAGNOSTICS_DIR"
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
  _update-body)    cmd_update_body ;;
  version)         repo_version ;;
  live-writer)     cmd_live_writer ;;
  stop-live-writer) cmd_stop_live_writer ;;
  diagnostics)     shift; cmd_diagnostics "$@" ;;
  doctor)          shift; cmd_doctor "$@" ;;
  logs)            shift; cmd_logs "$@" ;;
  *) echo "usage: echoechoctl.sh {status|start-daemon|stop-daemon|restart-daemon|boot-vm|stop-vm|reset-vm|build-app|install-app|start-app|stop-app|update|version|live-writer|stop-live-writer|diagnostics|doctor|logs}"; exit 2 ;;
esac
