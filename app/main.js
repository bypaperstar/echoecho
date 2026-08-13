// Echo Orb — menu-bar shell.
//
// Owns: the tray, the transparent frameless scene window, the reveal/dismiss
// lifecycle, and all HTTP to the Python viewer server (see lib/backend.js for
// why HTTP lives here and not in the renderer). The VNC bridge is lazy-loaded
// from vnc-proxy.js only when the renderer asks for Echo's Mac.
'use strict';

const { app, BrowserWindow, Tray, Menu, ipcMain, screen, globalShortcut } = require('electron');
const path = require('path');
const { trayIcon } = require('./lib/trayicon');
const { ViewerClient } = require('./lib/backend');

const SMOKE = process.env.ECHO_ORB_SMOKE === '1';
const DEMO = process.env.ECHO_ORB_DEMO === '1';
const VIEWER_PORT = process.env.ECHO_VIEWER_PORT || '8765';
const VIEWER_BASE = `http://127.0.0.1:${VIEWER_PORT}`;

let tray = null;
let win = null;
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

app.whenReady().then(() => {
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  app.on('second-instance', () => summon('second-instance'));
  if (process.platform === 'darwin' && app.dock) app.dock.hide();

  try {
    tray = new Tray(trayIcon());
    tray.setToolTip('Echo');
    tray.on('click', toggle);
    const menu = Menu.buildFromTemplate([
      { label: 'Summon Echo', click: () => summon('menu') },
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
