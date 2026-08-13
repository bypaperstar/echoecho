// Separate Electron entry for the VNC e2e (run by test/vnc-e2e.sh):
//   electron --no-sandbox app/test/vnc-smoke-main.js
// Loads renderer/vnc.js UNMODIFIED in a harness window, backs its
// window.orb.vncConnect with the real vnc-proxy, and captures the page
// before/after the renderer types a marker into the remote xterm.
'use strict';

const fs = require('fs');
const path = require('path');
const { app, BrowserWindow, ipcMain } = require('electron');
const proxy = require(path.join(__dirname, '..', 'vnc-proxy.js'));

// No extra Chromium switches here on purpose: the harness must run the same
// configuration as the real shell (app/main.js), or a PASS proves nothing
// about the app. Verified (process.sandboxed === true): Electron 38's
// sandboxed renderer dynamic-import()s the pure-ESM noVNC over file:// fine
// without allow-file-access-from-files.

// Fresh profile per run: the shared default ~/.config/Electron code cache
// has produced flaky "Failed to fetch dynamically imported module" errors
// when back-to-back runs reuse it.
app.setPath('userData', fs.mkdtempSync(path.join(require('os').tmpdir(), 'vnc-smoke-')));

const TARGET = process.env.ECHO_VNC_URL;
const BEFORE = process.env.VNC_E2E_BEFORE || '/tmp/vnc-e2e-before.png';
const AFTER = process.env.VNC_E2E_AFTER || '/tmp/vnc-e2e-after.png';
// RFB framebuffer updates and canvas paints are async: settle before capture
const SETTLE_MS = 2500;
const TIMEOUT_MS = 90000;

if (!TARGET) {
  console.error('[vnc-smoke] ECHO_VNC_URL is required');
  process.exit(2);
}

let win = null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fail(msg) {
  console.error('[vnc-smoke] FAIL:', msg);
  try { proxy.stop(); } catch {}
  app.exit(1);
}

// capturePage can transiently fail under Xvfb software raster (empty mojo
// CopyOutput results, GPU-process restarts yielding a uniform blank frame):
// accept only a real PNG of a NON-uniform frame — the VNC canvas always has
// the white xterm over the dark desktop — and retry otherwise.
function isUniform(img) {
  const bmp = img.toBitmap();
  if (bmp.length < 8) return true;
  for (let o = 4; o < bmp.length; o += 4 * 997) { // sample every 997th px
    if (bmp[o] !== bmp[0] || bmp[o + 1] !== bmp[1] || bmp[o + 2] !== bmp[2]) {
      return false;
    }
  }
  return true;
}

async function capture(file) {
  for (let attempt = 1; ; attempt++) {
    const img = await win.webContents.capturePage();
    const png = img.toPNG();
    const ok = png.length > 8 && png.readUInt32BE(0) === 0x89504e47 && !isUniform(img);
    if (ok) {
      fs.writeFileSync(file, png);
      console.log('[vnc-smoke] wrote', file, `(${png.length} bytes)`);
      return;
    }
    if (attempt >= 8) throw new Error(`capturePage kept returning blank/invalid images for ${file}`);
    await sleep(500);
  }
}

ipcMain.handle('vnc:connect', () => proxy.start(TARGET));
ipcMain.handle('vnc:disconnect', () => proxy.stop());
ipcMain.on('smoke:fail', (_e, msg) => fail(msg));

ipcMain.handle('smoke:phase', async (_e, name) => {
  try {
    if (name === 'connected') {
      await sleep(SETTLE_MS);
      await capture(BEFORE);
    } else if (name === 'typed') {
      await sleep(SETTLE_MS);
      await capture(AFTER);
      console.log('[vnc-smoke] OK');
      await proxy.stop();
      app.exit(0);
    }
  } catch (err) {
    fail(String((err && err.stack) || err));
  }
});

app.whenReady().then(() => {
  setTimeout(() => fail(`timed out after ${TIMEOUT_MS}ms`), TIMEOUT_MS);
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    show: true,
    webPreferences: {
      preload: path.join(__dirname, 'vnc-smoke-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.webContents.on('console-message', (_e, _level, message) => {
    console.log('[renderer]', message);
  });
  win.loadFile(path.join(__dirname, 'vnc-smoke.html'));
});
