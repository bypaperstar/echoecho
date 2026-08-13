// Echo Orb — menu-bar shell.
//
// Owns: the tray, the transparent frameless scene window, the reveal/dismiss
// lifecycle, and all HTTP to the Python viewer server (see lib/backend.js for
// why HTTP lives here and not in the renderer). The VNC bridge is lazy-loaded
// from vnc-proxy.js only when the renderer asks for Echo's Mac.
'use strict';

const { app, BrowserWindow, Tray, Menu, ipcMain, screen, globalShortcut, nativeImage } = require('electron');
const { spawn, execFile } = require('child_process');
const fs = require('fs');
const path = require('path');
const { trayIcon } = require('./lib/trayicon');
const { ViewerClient } = require('./lib/backend');
const { iconPng } = require('./lib/icon');

const SMOKE = process.env.ECHO_ORB_SMOKE === '1';
const SMOKE_CONTROL = process.env.ECHO_ORB_SMOKE_CONTROL === '1';
const DEMO = process.env.ECHO_ORB_DEMO === '1';
const VIEWER_PORT = process.env.ECHO_VIEWER_PORT || '8765';
const VIEWER_BASE = `http://127.0.0.1:${VIEWER_PORT}`;

// Packaged builds carry runtime-config.json (repo location + version, baked
// by echoctl build-app); a dev checkout derives the repo from its own path.
function loadRuntimeConfig() {
  try {
    return JSON.parse(fs.readFileSync(path.join(__dirname, 'runtime-config.json'), 'utf8'));
  } catch {
    return { repoRoot: path.resolve(__dirname, '..'), sha: 'dev', builtAt: null };
  }
}
const RUNTIME = loadRuntimeConfig();
const ECHOCTL = path.join(RUNTIME.repoRoot, 'scripts', 'echoctl.sh');

let tray = null;
let win = null;
let controlWin = null;
let viewer = null;
let vncProxy = null; // lazy require, holds { start, stop }
let visible = false;

function sceneBounds() {
  const area = screen.getPrimaryDisplay().workArea;
  const w = Math.min(Math.round(area.width * 0.74), 1320);
  const h = Math.min(Math.round(area.height * 0.86), 900);
  // Anchored toward the top-right, where the menu bar orb lives.
  const x = area.x + area.width - w - 24;
  const y = area.y + 8;
  return { x, y, width: w, height: h };
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
  // demo mode reaches the renderer as a query param — the clean channel
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'),
               DEMO ? { query: { demo: '1' } } : undefined);
  win.webContents.on('did-finish-load', () => {
    const queued = pendingSends;
    pendingSends = [];
    for (const [channel, payload] of queued) win.webContents.send(channel, payload);
  });
  win.on('blur', () => {
    // Clicking away dismisses, like a menu-bar popover — unless the user is
    // driving Echo's Mac, where focus loss is routine (drag, cmd-tab test).
    if (visible && !SMOKE) sendToScene('orb:blur');
  });
  win.on('closed', () => {
    // Mirrors orb:hidden so the tray/shortcut toggle summons after a WM close.
    visible = false;
    win = null;
  });
}

// webContents.send before the renderer finishes loading is silently lost —
// and the first summon (or a wake arriving during startup) races the load.
// Sends queue until did-finish-load and flush in order; lifecycle channels
// are last-intent-wins, so only the newest reveal/dismiss/blur survives.
const LIFECYCLE = new Set(['orb:reveal', 'orb:dismiss', 'orb:blur']);
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
  const bounds = win.getBounds();
  const fresh = sceneBounds();
  if (Math.abs(bounds.width - fresh.width) > 200) win.setBounds(fresh);
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
// The window you get when you open Echo.app: status of the daemon / VM / orb
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
    height: 560,
    resizable: false,
    fullscreenable: false,
    title: 'Echo',
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

app.whenReady().then(() => {
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  app.on('second-instance', () => openControl());
  if (process.platform === 'darwin' && app.dock) {
    // Visible in the Dock on purpose: "is Echo running?" should be answerable
    // at a glance. The packaged bundle carries its icns; a dev checkout gets
    // the same icon rendered at runtime.
    if (!app.isPackaged) {
      app.dock.setIcon(nativeImage.createFromBuffer(iconPng(512)));
    }
    app.dock.setMenu(Menu.buildFromTemplate([
      { label: 'Summon Echo', click: () => summon('dock') },
      { label: 'Control Panel', click: () => openControl() },
    ]));
  }
  // Dock icon click (macOS 'activate') reopens the control panel.
  app.on('activate', () => openControl());

  try {
    tray = new Tray(trayIcon());
    tray.setToolTip('Echo');
    tray.on('click', toggle);
    const menu = Menu.buildFromTemplate([
      { label: 'Summon Echo', click: () => summon('menu') },
      { label: 'Control Panel…', click: () => openControl() },
      { type: 'separator' },
      { label: 'Quit Echo Orb', click: () => app.quit() },
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
    // Opened like a normal app (double-click Echo.app): show the panel.
    openControl();
  }
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (viewer) viewer.stop();
  if (vncProxy) vncProxy.stop();
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

// Renderer asks for Echo's Mac: resolve the VNC endpoint (env override first,
// then the viewer's /vnc-info), start the WS<->TCP bridge, hand back a local
// WebSocket URL + password for noVNC. Failure ("asleep") is marshalled as a
// value — an IPC rejection would console.error in main, and an unreachable
// Mac is expected degradation, not an error. preload rethrows it, so the
// renderer still sees a rejected promise.
ipcMain.handle('vnc:connect', async () => {
  try {
    let target = process.env.ECHO_VNC_URL || null;
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

function runEchoctl(cmd, detached) {
  return new Promise((resolve) => {
    const child = spawn('bash', [ECHOCTL, cmd], {
      cwd: RUNTIME.repoRoot,
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
    version: RUNTIME.sha,
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
  // echoctl-backed (long ones run detached; the panel re-polls status)
  'daemon-start': () => runEchoctl('start-daemon'),
  'daemon-stop': () => runEchoctl('stop-daemon'),
  'daemon-restart': () => runEchoctl('restart-daemon'),
  'vm-boot': () => runEchoctl('boot-vm', true),
  'vm-reset': () => runEchoctl('reset-vm', true),
  'update': () => {
    // the script waits ~2s for us to exit, then pulls, rebuilds, reinstalls
    // the bundle and reopens it
    runEchoctl('update', true);
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
