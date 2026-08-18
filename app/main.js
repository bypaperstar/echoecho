// echoecho Orb — menu-bar shell.
//
// Owns: the tray, the transparent frameless scene window, the reveal/dismiss
// lifecycle, the wake-word daemon's lifecycle (started on launch, tethered to
// this process so even a force quit takes it down, stopped on quit), and all
// HTTP to the Python viewer server (see lib/backend.js for why HTTP lives
// here and not in the renderer). The VNC bridge is lazy-loaded from
// vnc-proxy.js only when the renderer asks for echoecho's Mac.
'use strict';

const { app, BrowserWindow, Tray, Menu, ipcMain, screen, globalShortcut, nativeImage } = require('electron');
const { spawn, execFile, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const { trayIcon } = require('./lib/trayicon');
const { ViewerClient } = require('./lib/backend');
const { iconPng } = require('./lib/icon');

const SMOKE = process.env.ECHOECHO_ORB_SMOKE === '1';
const SMOKE_CONTROL = process.env.ECHOECHO_ORB_SMOKE_CONTROL === '1';
const DEMO = process.env.ECHOECHO_ORB_DEMO === '1';
// The app owns the wake word: launching the app starts the daemon (tethered
// to our pid, so even a force quit takes it down), quitting stops it. Off in
// smoke/demo runs and off macOS, where there is no daemon to own.
const MANAGED = process.platform === 'darwin' && !SMOKE && !SMOKE_CONTROL && !DEMO;
const VIEWER_PORT = process.env.ECHOECHO_VIEWER_PORT || '8765';
const VIEWER_BASE = `http://127.0.0.1:${VIEWER_PORT}`;

// Packaged builds carry runtime-config.json (repo location + version, baked
// by echoechoctl build-app); a dev checkout derives the repo from its own
// path and asks git live, so "Update & relaunch" is reflected on next start.
function loadRuntimeConfig() {
  try {
    return JSON.parse(fs.readFileSync(path.join(__dirname, 'runtime-config.json'), 'utf8'));
  } catch {
    const repoRoot = path.resolve(__dirname, '..');
    const cfg = { repoRoot, version: null, sha: 'dev', updatedAt: null, builtAt: null };
    try { cfg.version = fs.readFileSync(path.join(repoRoot, 'VERSION'), 'utf8').trim(); } catch { /* shown as v? */ }
    try {
      const line = execSync('git log -1 "--format=%h %cI"', { cwd: repoRoot }).toString().trim().split(' ');
      cfg.sha = line[0];
      cfg.updatedAt = line[1];
      // VERSION holds MAJOR.MINOR; the patch is the commit count, so every
      // deploy of new code shows a new number. A full semver in VERSION wins.
      if (cfg.version && cfg.version.split('.').length < 3) {
        cfg.version += '.' + execSync('git rev-list --count HEAD', { cwd: repoRoot }).toString().trim();
      }
    } catch { /* not a git checkout: version alone still shows */ }
    return cfg;
  }
}
const RUNTIME = loadRuntimeConfig();
const ECHOECHOCTL = path.join(RUNTIME.repoRoot, 'scripts', 'echoechoctl.sh');

let tray = null;
let win = null;
let controlWin = null;
let viewer = null;
let vncProxy = null; // lazy require, holds { start, stop }
let visible = false;

function sceneBounds() {
  // The whole work area: the scene is click-through everywhere except the
  // blob and its items, so covering the screen costs nothing — and it lets
  // the orb (and its items) be dragged anywhere instead of hitting the edge
  // of an invisible top-right box.
  const area = screen.getPrimaryDisplay().workArea;
  return { x: area.x, y: area.y, width: area.width, height: area.height };
}

// Where the blob pours from, in window-relative coordinates.
function anchorPoint(bounds) {
  if (tray && process.platform === 'darwin') {
    const tb = tray.getBounds();
    if (tb && tb.width) {
      return {
        x: Math.max(0, Math.min(bounds.width, tb.x + tb.width / 2 - bounds.x)),
        y: 0,
      };
    }
  }
  return { x: bounds.width - 40, y: 0 };
}

function createWindow() {
  const bounds = sceneBounds();
  win = new BrowserWindow({
    ...bounds,
    show: false,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: true,
    skipTaskbar: true,
    alwaysOnTop: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.setAlwaysOnTop(true, 'floating');
  // The window spans most of the screen but only the blob and its items may
  // eat clicks. Start click-through; the renderer toggles it from hover
  // (forward:true keeps mousemove flowing while ignored, so hover works).
  win.setIgnoreMouseEvents(true, { forward: true });
  // demo mode reaches the renderer as a query param — the clean channel
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'),
               DEMO ? { query: { demo: '1' } } : undefined);
  win.webContents.on('did-finish-load', () => {
    const queued = pendingSends;
    pendingSends = [];
    for (const [channel, payload] of queued) win.webContents.send(channel, payload);
  });
  // No blur-dismiss: the scene is click-through outside the blob, so losing
  // focus is routine (the whole point is working next to echoecho). Dismissal is
  // deliberate: Escape, the tray toggle, or Cmd-Shift-E.
  win.on('closed', () => {
    // Mirrors orb:hidden so the tray/shortcut toggle summons after a WM close.
    visible = false;
    win = null;
  });
}

// webContents.send before the renderer finishes loading is silently lost —
// and the first summon (or a wake arriving during startup) races the load.
// Sends queue until did-finish-load and flush in order; lifecycle channels
// are last-intent-wins, so only the newest reveal/dismiss survives.
const LIFECYCLE = new Set(['orb:reveal', 'orb:dismiss']);
let pendingSends = [];

function sendToScene(channel, payload) {
  if (!win) return;
  const wc = win.webContents;
  if (wc.isLoadingMainFrame()) {
    if (LIFECYCLE.has(channel)) {
      pendingSends = pendingSends.filter(([ch]) => !LIFECYCLE.has(ch));
    }
    pendingSends.push([channel, payload]);
  } else {
    wc.send(channel, payload);
  }
}

// Only an orb:hidden that answers our own orb:dismiss may hide the window;
// stale ones (from a dismissal superseded by a summon) must not.
let dismissing = false;

function summon(reason) {
  if (!win) createWindow();
  // re-cover the work area every summon: displays change, docks move
  const bounds = win.getBounds();
  const fresh = sceneBounds();
  if (bounds.x !== fresh.x || bounds.y !== fresh.y ||
      bounds.width !== fresh.width || bounds.height !== fresh.height) {
    win.setBounds(fresh);
  }
  visible = true;
  dismissing = false;
  win.show();
  sendToScene('orb:reveal', { anchor: anchorPoint(win.getBounds()), reason });
}

function dismiss() {
  if (!win || !visible) return;
  dismissing = true;
  sendToScene('orb:dismiss');
}

function toggle() {
  visible ? dismiss() : summon('tray');
}

// ---- control panel --------------------------------------------------------
// The window you get when you open echoecho.app: status of the daemon / VM / orb
// and the open/close/reset/update buttons. A normal framed window, so the
// dock shows the app is running and Cmd-W behaves as expected.
function openControl() {
  if (controlWin) {
    controlWin.show();
    controlWin.focus();
    return;
  }
  controlWin = new BrowserWindow({
    width: 460,
    height: 610,
    resizable: false,
    fullscreenable: false,
    title: 'echoecho',
    backgroundColor: '#101116',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  controlWin.loadFile(path.join(__dirname, 'renderer', 'control.html'));
  controlWin.on('closed', () => {
    controlWin = null;
  });
}

// The wake word runs iff the app runs. start-daemon no-ops when the daemon is
// already up, so this is safe to call on every launch and second-instance.
function ensureDaemon() {
  if (!MANAGED || !ownsLifecycle) return;
  runEchoechoctl('start-daemon').then((r) => {
    // The Dock icon promises "echoecho is listening" — a silent start failure
    // (missing .venv, stale repoRoot, no API key) would make it lie.
    if (!r.ok) console.error('[daemon] start-daemon failed:', r.output);
  });
}

// Only the instance holding the single-instance lock may manage the daemon:
// a losing duplicate still fires will-quit on its way out, and must not kill
// the daemon the primary instance owns.
let ownsLifecycle = false;

app.whenReady().then(() => {
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  ownsLifecycle = true;
  // echoechoctl start-daemon relaunches the app when it isn't running; that
  // arrives here as a second instance, so make sure the daemon comes up too.
  app.on('second-instance', () => { openControl(); ensureDaemon(); });
  if (process.platform === 'darwin' && app.dock) {
    // Visible in the Dock on purpose: app running ⇔ wake word listening
    // (ensureDaemon on launch, tether + stop-daemon on exit), so the Dock
    // icon answers "is echoecho listening?" at a glance. The packaged bundle
    // carries its icns; a dev checkout gets the same icon rendered at runtime.
    if (!app.isPackaged) {
      app.dock.setIcon(nativeImage.createFromBuffer(iconPng(512)));
    }
    app.dock.setMenu(Menu.buildFromTemplate([
      { label: 'Summon echoecho', click: () => summon('dock') },
      { label: 'Control Panel', click: () => openControl() },
    ]));
  }
  // Dock icon click (macOS 'activate') reopens the control panel.
  app.on('activate', () => openControl());

  try {
    tray = new Tray(trayIcon());
    tray.setToolTip('echoecho');
    tray.on('click', toggle);
    const menu = Menu.buildFromTemplate([
      { label: 'Summon echoecho', click: () => summon('menu') },
      { label: 'Control Panel…', click: () => openControl() },
      { type: 'separator' },
      { label: 'Quit echoecho Orb', click: () => app.quit() },
    ]);
    if (process.platform === 'darwin') {
      // setContextMenu on macOS opens the menu on LEFT click too, killing toggle.
      tray.on('right-click', () => tray.popUpContextMenu(menu));
    } else {
      tray.setContextMenu(menu);
    }
  } catch (err) {
    // Headless CI has no tray; the window + shortcuts still work.
    console.error('[orb] tray unavailable:', err.message);
  }

  createWindow();

  viewer = new ViewerClient(VIEWER_BASE);
  viewer.on('events', (evts) => sendToScene('viewer:events', evts));
  viewer.on('wake', () => summon('wake'));
  viewer.on('connected', () => sendToScene('viewer:status', { connected: true }));
  viewer.on('disconnected', () => sendToScene('viewer:status', { connected: false }));
  viewer.start();

  ensureDaemon();

  globalShortcut.register('CommandOrControl+Shift+E', toggle);

  if (SMOKE && DEMO) {
    // series capture of the scripted demo, aligned to the scene's own clock
    // (the renderer signals orb:demo-started when its timeline begins)
    summon('smoke');
    ipcMain.once('orb:demo-started', () => {
      const fs = require('fs');
      fs.mkdirSync('/tmp/orbscene', { recursive: true });
      const shots = [[500, '05'], [3000, '3'], [6000, '6'], [9000, '9'], [12000, '12'], [15000, '15']];
      let pending = shots.length;
      for (const [ms, name] of shots) {
        setTimeout(async () => {
          try {
            const img = await win.webContents.capturePage();
            fs.writeFileSync(`/tmp/orbscene/shot-${name}s.png`, img.toPNG());
            console.log(`[smoke] wrote /tmp/orbscene/shot-${name}s.png`);
          } catch (err) {
            console.error('[smoke] capture failed:', err);
            process.exitCode = 1;
          }
          if (--pending === 0) app.quit();
        }, ms);
      }
    });
  } else if (SMOKE) {
    summon('smoke');
    setTimeout(async () => {
      try {
        const img = await win.webContents.capturePage();
        require('fs').writeFileSync('/tmp/orb-smoke.png', img.toPNG());
        console.log('[smoke] wrote /tmp/orb-smoke.png');
      } catch (err) {
        console.error('[smoke] capture failed:', err);
        process.exitCode = 1;
      }
      app.quit();
    }, 6000);
  } else if (DEMO) {
    summon('demo');
  } else if (SMOKE_CONTROL) {
    openControl();
    setTimeout(async () => {
      try {
        const img = await controlWin.webContents.capturePage();
        require('fs').writeFileSync('/tmp/orb-control.png', img.toPNG());
        console.log('[smoke] wrote /tmp/orb-control.png');
      } catch (err) {
        console.error('[smoke] capture failed:', err);
        process.exitCode = 1;
      }
      app.quit();
    }, 4000);
  } else {
    // Opened like a normal app (double-click echoecho.app): show the panel.
    openControl();
  }
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (viewer) viewer.stop();
  if (vncProxy) vncProxy.stop();
  // Quitting the app quits the wake word: detached so it outlives us. This
  // also covers daemons we didn't start; force quit (no will-quit) is handled
  // by the daemon's own tether to our pid.
  if (MANAGED && ownsLifecycle) runEchoechoctl('stop-daemon', true);
});

// Keep running with no windows: we live in the menu bar.
app.on('window-all-closed', () => {});

// ---- IPC ----------------------------------------------------------------

ipcMain.handle('viewer:transcript', () => viewer.transcript());
ipcMain.handle('viewer:doc', (_e, relpath) => viewer.doc(String(relpath)));

ipcMain.on('orb:hidden', () => {
  if (!dismissing) return;
  dismissing = false;
  visible = false;
  if (win) win.hide();
});
ipcMain.on('orb:dismiss-request', () => dismiss());

// Renderer hover tracking: ignore=true (click-through) everywhere except over
// the blob's silhouette or an item; forward keeps mousemove alive while ignored.
ipcMain.on('orb:passthrough', (_e, on) => {
  if (win) win.setIgnoreMouseEvents(!!on, { forward: true });
});

// Renderer asks for echoecho's Mac: resolve the VNC endpoint (env override first,
// then the viewer's /vnc-info), start the WS<->TCP bridge, hand back a local
// WebSocket URL + password for noVNC. Failure ("asleep") is marshalled as a
// value — an IPC rejection would console.error in main, and an unreachable
// Mac is expected degradation, not an error. preload rethrows it, so the
// renderer still sees a rejected promise.
ipcMain.handle('vnc:connect', async () => {
  try {
    let target = process.env.ECHOECHO_VNC_URL || null;
    if (!target) {
      const info = await viewer.vncInfo(); // throws -> renderer shows "asleep"
      target = info.url;
    }
    if (!vncProxy) vncProxy = require('./vnc-proxy');
    return await vncProxy.start(target);
  } catch (err) {
    return { error: String((err && err.message) || err) };
  }
});

ipcMain.handle('vnc:disconnect', () => {
  if (vncProxy) vncProxy.stop();
});

// ---- control panel IPC ----------------------------------------------------

function runEchoechoctl(cmd, detached) {
  return new Promise((resolve) => {
    const child = spawn('bash', [ECHOECHOCTL, cmd], {
      cwd: RUNTIME.repoRoot,
      // start-daemon tethers the daemon to this pid: it exits when we do,
      // force quit included. Other commands ignore the variable.
      env: { ...process.env, ECHOECHO_TETHER_PID: String(process.pid) },
      detached: !!detached,
      stdio: detached ? 'ignore' : ['ignore', 'pipe', 'pipe'],
    });
    if (detached) {
      child.unref();
      resolve({ ok: true, detached: true });
      return;
    }
    let out = '';
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (out += d));
    child.on('close', (code) => resolve({ ok: code === 0, output: out.trim() }));
    child.on('error', (err) => resolve({ ok: false, output: err.message }));
  });
}

ipcMain.handle('ctl:status', async () => {
  const status = {
    version: RUNTIME.version,
    sha: RUNTIME.sha,
    updatedAt: RUNTIME.updatedAt,
    builtAt: RUNTIME.builtAt,
    orbVisible: visible,
    viewer: false,
    lastEventTs: null,
    vm: false,
    loginItem: app.getLoginItemSettings().openAtLogin,
  };
  try {
    const events = await viewer.transcript();
    status.viewer = true; // the viewer lives inside the daemon: up == daemon up
    const last = events[events.length - 1];
    if (last && typeof last.ts === 'number') status.lastEventTs = last.ts;
  } catch { /* daemon down */ }
  try {
    await viewer.vncInfo();
    status.vm = true;
  } catch { /* VM asleep or no token */ }
  return status;
});

const CTL_ACTIONS = {
  // in-process
  'summon': () => { summon('control'); return { ok: true }; },
  'dismiss': () => { dismiss(); return { ok: true }; },
  'quit-app': () => { setTimeout(() => app.quit(), 150); return { ok: true }; },
  // echoechoctl-backed (long ones run detached; the panel re-polls status)
  'daemon-start': () => runEchoechoctl('start-daemon'),
  'daemon-stop': () => runEchoechoctl('stop-daemon'),
  'daemon-restart': () => runEchoechoctl('restart-daemon'),
  'vm-boot': () => runEchoechoctl('boot-vm', true),
  'vm-reset': () => runEchoechoctl('reset-vm', true),
  // starts the standalone Live Writer server if needed and opens the page in
  // the default browser (the script does the `open`; it blocks until healthy)
  'live-writer': () => runEchoechoctl('live-writer'),
  'update': () => {
    // the script waits ~2s for us to exit, then pulls, rebuilds, reinstalls
    // the bundle and reopens it
    runEchoechoctl('update', true);
    setTimeout(() => app.quit(), 500);
    return { ok: true, detached: true };
  },
};

ipcMain.handle('ctl:action', (_e, name) => {
  const fn = CTL_ACTIONS[String(name)];
  if (!fn) return { ok: false, output: `unknown action ${name}` };
  return fn();
});

ipcMain.handle('ctl:login-item', (_e, enable) => {
  app.setLoginItemSettings({ openAtLogin: !!enable });
  return { ok: true };
});
