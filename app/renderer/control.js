// Control panel logic: poll ctl:status, render the three status rows, and
// map the buttons onto ctl:action names. Deliberately dumb — all real work
// happens in main / echoechoctl.sh.
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  let busy = false;
  let daemonUp = false;
  let vmUp = false;

  // tiny animated orb in the header — same organism, 52px
  const cv = $('icon');
  const cx2 = cv.getContext('2d');
  let t0 = performance.now();
  (function tick(now) {
    const t = (now - t0) / 1000;
    const S = cv.width;
    cx2.clearRect(0, 0, S, S);
    const cx = S / 2, cy = S / 2;
    cx2.beginPath();
    for (let a = 0; a <= Math.PI * 2 + 0.05; a += 0.12) {
      const r = S * 0.36 * (1 + Math.sin(a * 3 + t * 0.9) * 0.07 + Math.sin(a * 5 - t * 1.4) * 0.045);
      const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
      a === 0 ? cx2.moveTo(x, y) : cx2.lineTo(x, y);
    }
    cx2.closePath();
    const g = cx2.createLinearGradient(0, 0, 0, S);
    g.addColorStop(0, '#1c1d26');
    g.addColorStop(1, '#07070a');
    cx2.fillStyle = g;
    cx2.fill();
    cx2.strokeStyle = 'rgba(126,168,255,0.35)';
    cx2.lineWidth = 1.2;
    cx2.stroke();
    const sx = cx - S * 0.11, sy = cy - S * 0.15;
    const rg = cx2.createRadialGradient(sx, sy, 0, sx, sy, S * 0.15);
    rg.addColorStop(0, 'rgba(235,240,255,0.5)');
    rg.addColorStop(1, 'rgba(235,240,255,0)');
    cx2.fillStyle = rg;
    cx2.fill();
    requestAnimationFrame(tick);
  })(t0);

  function fmtAgo(ts) {
    if (!ts) return 'no events yet';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return `${Math.round(s)}s ago`;
    if (s < 5400) return `${Math.round(s / 60)}m ago`;
    return `${Math.round(s / 3600)}h ago`;
  }

  function setDot(id, on) {
    $(id).className = 'dot ' + (on ? 'on' : 'off');
  }

  async function refresh() {
    let st;
    try {
      st = await window.ctl.status();
    } catch {
      return;
    }
    daemonUp = st.viewer;
    vmUp = st.vm;
    $('version').textContent = st.builtAt
      ? `${st.version} · built ${new Date(st.builtAt).toLocaleString()}`
      : `${st.version} checkout`;
    setDot('d-daemon', daemonUp);
    $('v-daemon').textContent = daemonUp ? `listening · last event ${fmtAgo(st.lastEventTs)}` : 'stopped';
    setDot('d-vm', vmUp);
    $('v-vm').textContent = vmUp ? 'running — portal live' : 'asleep';
    setDot('d-orb', true);
    $('v-orb').textContent = st.orbVisible ? 'revealed' : 'in the menu bar';
    $('b-daemon').textContent = daemonUp ? 'Stop daemon' : 'Start daemon';
    $('b-vm').textContent = vmUp ? "echoecho's Mac is awake" : "Wake echoecho's Mac";
    $('b-vm').disabled = vmUp || busy;  // the 3s poll must not undo the busy grey-out
    $('login').checked = !!st.loginItem;
  }

  // grey the whole action grid while one runs: double-clicks were already
  // ignored via `busy`, but nothing showed the user that
  function setActionsDisabled(on) {
    document.querySelectorAll('.actions button').forEach((b) => { b.disabled = on; });
  }

  async function act(name, noteText) {
    if (busy) return;
    busy = true;
    setActionsDisabled(true);
    $('note').textContent = noteText || '';
    try {
      const res = await window.ctl.action(name);
      if (res && res.output) $('note').textContent = res.output.split('\n').pop();
      else if (res && res.ok && !noteText) $('note').textContent = '';
    } finally {
      busy = false;
      setActionsDisabled(false);
      refresh();
    }
  }

  $('b-summon').addEventListener('click', () => act('summon'));
  $('b-daemon').addEventListener('click', () =>
    act(daemonUp ? 'daemon-stop' : 'daemon-start', daemonUp ? 'stopping daemon…' : 'starting daemon…'));
  $('b-vm').addEventListener('click', () => act('vm-boot', "waking echoecho's Mac (clone + boot takes ~a minute)…"));
  $('b-reset').addEventListener('click', () => {
    if (confirm("Reset echoecho's Mac? The VM is deleted and re-cloned fresh from the golden image. Workspace files on your Mac are untouched.")) {
      act('vm-reset', 'resetting: delete + fresh clone + boot…');
    }
  });
  $('b-update').addEventListener('click', () => {
    if (confirm('Update echoecho? Pulls the latest main, reinstalls, rebuilds the app, restarts the daemon, and relaunches. echoecho will quit now and reopen when done.')) {
      $('note').textContent = 'updating — echoecho will reopen itself…';
      window.ctl.action('update');
    }
  });
  $('b-quit').addEventListener('click', () => window.ctl.action('quit-app'));
  $('login').addEventListener('change', (e) => window.ctl.setLoginItem(e.target.checked));

  refresh();
  setInterval(refresh, 3000);
})();
