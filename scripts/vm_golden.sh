#!/usr/bin/env bash
# Build Echo's golden macOS VM (sandbox tier 2, PLAN-GENERIC.md). Run ON THE
# MAC, once; idempotent, safe to re-run after a partial failure.
#
#   golden image = base macOS + Echo's SSH key + AcceptEnv for model keys
#                  (+ node & the claude CLI inside the guest with AGENT=1)
#   scratch VMs are APFS-cloned from it at task time (echo_app/services/vm.py)
#
# Knobs: ECHO_VM_GOLDEN, ECHO_VM_IMAGE, ECHO_VM_SSH_KEY, ECHO_VM_USER,
# ECHO_VM_PASSWORD (vanilla images ship lume/lume), AGENT=1.
set -euo pipefail

GOLDEN=${ECHO_VM_GOLDEN:-echo-golden}
IMAGE=${ECHO_VM_IMAGE:-macos-sequoia-vanilla:latest}
KEY=${ECHO_VM_SSH_KEY:-$HOME/.ssh/echo_vm_ed25519}
KEY="${KEY/#\~/$HOME}"
GUEST_USER=${ECHO_VM_USER:-lume}
GUEST_PASS=${ECHO_VM_PASSWORD:-lume}
AGENT=${AGENT:-0}
NODE_VERSION=${NODE_VERSION:-v22.11.0}

say() { printf '\n== %s\n' "$*"; }

# -- 0. lume ------------------------------------------------------------------
if ! command -v lume >/dev/null 2>&1; then
  say "installing lume"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/lume/scripts/install.sh)"
  export PATH="$HOME/.local/bin:$PATH"
fi

vm_json() { lume get "$GOLDEN" -f json 2>/dev/null || true; }
vm_field() {  # $1 = key. lume 0.5.x returns a JSON ARRAY (maybe log-prefixed):
  vm_json | python3 -c '   # try each [/{ offset until one parses, unwrap [0]
import json, sys
raw = sys.stdin.read()
for i in sorted(j for j, c in enumerate(raw) if c in "[{"):
    try:
        d = json.loads(raw[i:])
    except ValueError:
        continue
    if isinstance(d, list):
        d = d[0] if d else {}
    print(d.get(sys.argv[1], "") if isinstance(d, dict) else "")
    break
' "$1"
}

# -- 1. base image -> golden VM -------------------------------------------------
if [ -z "$(vm_field name)" ]; then
  say "pulling $IMAGE as $GOLDEN (tens of GB — one time)"
  lume pull "$IMAGE" "$GOLDEN"
else
  say "$GOLDEN already exists — skipping pull"
fi

# -- 2. Echo's SSH key -----------------------------------------------------------
if [ ! -f "$KEY" ]; then
  say "generating $KEY"
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C echo-vm >/dev/null
fi
PUBKEY=$(cat "$KEY.pub")

# -- 3. boot + wait for SSH ------------------------------------------------------
if [ "$(vm_field status)" != "running" ]; then
  say "booting $GOLDEN"
  nohup lume run "$GOLDEN" --no-display >/dev/null 2>&1 &
fi
say "waiting for the guest to come up"
IP=""
for _ in $(seq 1 120); do
  IP=$(vm_field ipAddress)
  if [ -n "$IP" ] && nc -z -w 2 "$IP" 22 2>/dev/null; then break; fi
  sleep 2
done
[ -n "$IP" ] || { echo "guest never became reachable"; exit 1; }
say "guest is up at $IP"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
# PreferredAuthentications=password forces plain password: without it ssh
# offers keyboard-interactive first, whose PAM prompt hangs a scripted expect
# (learned the hard way validating this live).
PW_OPTS="$SSH_OPTS -o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1"

ssh_pass() {  # password-auth ssh via expect (stock on macOS); $* = remote cmd
  expect - "$GUEST_USER" "$GUEST_PASS" "$IP" "$PW_OPTS" "$*" <<'EXP'
set user [lindex $argv 0]
set pass [lindex $argv 1]
set host [lindex $argv 2]
set opts [lindex $argv 3]
set cmd  [lindex $argv 4]
set timeout 120
spawn ssh {*}[split $opts] $user@$host $cmd
expect {
  -re "(?i)password:" { send "$pass\r"; exp_continue }
  eof
}
catch wait result
exit [lindex $result 3]
EXP
}

# -- 4. key auth + AcceptEnv (model API keys reach the in-guest agent) -----------
if ! ssh -i "$KEY" $SSH_OPTS -o BatchMode=yes -o ConnectTimeout=5 \
     "$GUEST_USER@$IP" true 2>/dev/null; then
  say "installing Echo's key in the guest (ssh-copy-id, default creds)"
  # ssh-copy-id appends the key in the correct format and fixes perms itself
  # (a hand-rolled echo/base64 append silently corrupted the key when tested
  # live); -f skips its own key-auth precheck.
  expect - "$GUEST_USER" "$GUEST_PASS" "$IP" "$KEY.pub" "$PW_OPTS" <<'EXP'
set user [lindex $argv 0]
set pass [lindex $argv 1]
set host [lindex $argv 2]
set pub  [lindex $argv 3]
set opts [lindex $argv 4]
set timeout 60
spawn ssh-copy-id -f -i $pub {*}[split $opts] $user@$host
expect {
  -re "(?i)password:" { send "$pass\r"; exp_continue }
  -re "Number of key.s. added|added: 1|WARNING: All keys" { }
  eof
}
EXP
fi
gssh() { ssh -i "$KEY" $SSH_OPTS "$GUEST_USER@$IP" "$@"; }
say "key auth OK: $(gssh 'echo ok from $(hostname)')"

# least privilege: allow ONLY the key the in-guest runtime needs. Default is
# claude -> ANTHROPIC_API_KEY; override ACCEPT_ENV for a codex/other guest.
ACCEPT_ENV=${ACCEPT_ENV:-ANTHROPIC_API_KEY}
if ! gssh "grep -q '^AcceptEnv $ACCEPT_ENV' /etc/ssh/sshd_config" 2>/dev/null; then
  say "allowing $ACCEPT_ENV through the guest sshd (AcceptEnv)"
  gssh "echo '$GUEST_PASS' | sudo -S sh -c \
    'echo \"AcceptEnv $ACCEPT_ENV\" >> /etc/ssh/sshd_config \
     && launchctl kickstart -k system/com.openssh.sshd 2>/dev/null || true'"
fi

# -- 5. optional: the agent runtime inside the guest -----------------------------
if [ "$AGENT" = "1" ]; then
  if ! gssh "test -x /usr/local/bin/claude || command -v claude" >/dev/null 2>&1; then
    say "installing node $NODE_VERSION + claude CLI inside the guest"
    # npm's shebang is '#!/usr/bin/env node', so node MUST be on PATH for the
    # npm run and for claude itself — pass it explicitly through sudo (which
    # scrubs PATH) via `sudo env PATH=...`.
    gssh "curl -fsSL -o /tmp/node.tar.gz \
      https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-darwin-arm64.tar.gz \
      && echo '$GUEST_PASS' | sudo -S sh -c \
      'mkdir -p /usr/local && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1' \
      && echo '$GUEST_PASS' | sudo -S env PATH=/usr/local/bin:/usr/bin:/bin \
      /usr/local/bin/node /usr/local/bin/npm install -g @anthropic-ai/claude-code \
      && /usr/local/bin/node /usr/local/bin/claude --version"
  else
    say "claude CLI already in the guest"
  fi
fi

# -- 6. freeze the golden image ---------------------------------------------------
say "stopping $GOLDEN (scratch VMs clone from it at task time)"
lume stop "$GOLDEN" || true

cat <<DONE

golden VM '$GOLDEN' is ready.
  try the tier:   ECHO_SANDBOX=vm python3 echo.py --text
  scratch VM:     managed automatically (clone '$GOLDEN' -> '\${ECHO_VM_NAME:-echo-vm}')
  undo/rollback:  lume delete \${ECHO_VM_NAME:-echo-vm} --force   (re-clones next task)
DONE
