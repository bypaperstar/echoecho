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
const {
  createDiagnostics, errorFields, fingerprint, boundedFingerprint,
} = require('./lib/diagnostics');

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
// Claim primary ownership before creating a latest pointer. A losing
// duplicate must not overwrite the live Orb's diagnostics pointer or prune
// its history while it exits.
const primaryInstance = app.requestSingleInstanceLock();
let ownsLifecycle = false;
const diagnostics = createDiagnostics({
  enabled: primaryInstance ? undefined : false,
  build: {
    version: RUNTIME.version,
    sha: RUNTIME.sha,
    packaged: app.isPackaged,
    electron: process.versions.electron,
    node: process.versions.node,
  },
});
const mainDiag = diagnostics.child('electron-main');
const viewerDiag = diagnostics.child('viewer-client');
const noisyDiagnosticCounts = new Map();
let noisyDiagnosticReceived = 0;
let noisyDiagnosticEmitted = 0;
const NOISY_WINDOW_MS = 10000;
const NOISY_WINDOW_BURST = 20;
const NOISY_FINGERPRINTS = 32;

function sampleNoisyDiagnostic(key, valueFingerprint = '') {
  const now = Date.now();
  let state = noisyDiagnosticCounts.get(key);
  if (!state || now - state.startedAt >= NOISY_WINDOW_MS) {
    state = { startedAt: now, count: 0, fingerprints: new Map() };
    noisyDiagnosticCounts.set(key, state);
  }
  state.count++;
  let firstSeenFingerprint = false;
  if (valueFingerprint) {
    const prior = state.fingerprints.get(valueFingerprint);
    if (prior !== undefined) {
      state.fingerprints.set(valueFingerprint, prior + 1);
    } else if (state.fingerprints.size < NOISY_FINGERPRINTS) {
      // Preserve a bounded allowance for a novel failure/state even after an
      // unrelated renderer flood has exhausted the ordinary category burst.
      // Do not evict within the window: otherwise attacker-controlled unique
      // values could continually regain this first-seen allowance.
      state.fingerprints.set(valueFingerprint, 1);
      firstSeenFingerprint = true;
    }
  }
  noisyDiagnosticReceived++;
  const overflow = state.count - NOISY_WINDOW_BURST;
  const emit = overflow <= 0 || firstSeenFingerprint || overflow <= 3 ||
    (overflow & (overflow - 1)) === 0;
  if (emit) noisyDiagnosticEmitted++;
  return { emit, count: state.count };
}

function flushNoisyDiagnosticSummary() {
  if (!noisyDiagnosticReceived) return;
  mainDiag.info('renderer.diagnostic_summary', {
    source_count: noisyDiagnosticCounts.size,
    received: noisyDiagnosticReceived,
    emitted: noisyDiagnosticEmitted,
    suppressed: noisyDiagnosticReceived - noisyDiagnosticEmitted,
  });
  noisyDiagnosticCounts.clear();
  noisyDiagnosticReceived = 0;
  noisyDiagnosticEmitted = 0;
}

// Observe fatal errors without suppressing Node's normal uncaught-exception
// behavior. Under Node's default rejection policy, an unhandled rejection is
// promoted to an uncaught exception and arrives here too. Do not install an
// ``unhandledRejection`` listener: its mere presence changes the default from
// fatal to handled and could keep a corrupted Electron process alive.
process.on('uncaughtExceptionMonitor', (err, origin) => {
  mainDiag.error('process.uncaught_exception', { origin, error: errorFields(err) });
});
mainDiag.info('app.process_start', {
  platform: process.platform, arch: process.arch,
  smoke: SMOKE, smoke_control: SMOKE_CONTROL, demo: DEMO, managed: MANAGED,
});

let tray = null;
let win = null;
let controlWin = null;
let viewer = null;
let viewerConnectionState = null;
let vncProxy = null; // lazy require, holds { start, stop }
let visible = false;

function attachWindowDiagnostics(browserWindow, surface) {
  const log = diagnostics.child(surface);
  const wc = browserWindow.webContents;
  let loadStartedAt = 0;
  let unresponsiveAt = 0;
  log.info('window.created', {});
  wc.on('did-start-loading', () => {
    loadStartedAt = Date.now();
    log.info('window.load_started', {});
  });
  wc.on('did-finish-load', () => {
    log.info('window.load_finished', {
      duration_ms: loadStartedAt ? Date.now() - loadStartedAt : null,
    });
  });
  wc.on('did-fail-load', (_event, code, description, _validatedUrl, isMainFrame) => {
    log.error('window.load_failed', {
      code, is_main_frame: !!isMainFrame,
      description_fingerprint: fingerprint(description),
    });
  });
  wc.on('preload-error', (_event, _preloadPath, err) => {
    log.error('window.preload_failed', { error: errorFields(err) });
  });
  wc.on('render-process-gone', (_event, details) => {
    log.error('window.renderer_gone', {
      reason: details && details.reason,
      exit_code: details && details.exitCode,
    });
  });
  wc.on('console-message', (_event, ...args) => {
    const details = args.length === 1 && args[0] && typeof args[0] === 'object' ? args[0] : {
      level: args[0], message: args[1], lineNumber: args[2],
    };
    const levelMap = { verbose: 0, info: 1, warning: 2, error: 3 };
    const numeric = typeof details.level === 'string' ? levelMap[details.level] : Number(details.level);
    if (!(numeric >= 2)) return; // warnings/errors only; never retain console text
    // Renderer console text is untrusted and can contain an entire response or
    // document. Hash a bounded prefix plus its original length so one warning
    // cannot synchronously pin the Electron main thread.
    const message = boundedFingerprint(details.message);
    const messageFingerprint = message.fingerprint;
    const sample = sampleNoisyDiagnostic(
      `console:${surface}:${numeric}`, messageFingerprint);
    if (!sample.emit) return;
    log[numeric >= 3 ? 'error' : 'warn']('window.console_message', {
      console_level: numeric,
      message_fingerprint: messageFingerprint,
      message_length: message.length,
      line: Number(details.lineNumber) || null,
      occurrences: sample.count,
    });
  });
  browserWindow.on('unresponsive', () => {
    unresponsiveAt = Date.now();
    log.error('window.unresponsive', {});
  });
  browserWindow.on('responsive', () => {
    log.info('window.responsive', {
      unresponsive_ms: unresponsiveAt ? Date.now() - unresponsiveAt : null,
    });
    unresponsiveAt = 0;
  });
  browserWindow.on('closed', () => log.info('window.closed', {}));
}

function loadWindowFile(browserWindow, file, options, surface) {
  const started = Date.now();
  browserWindow.loadFile(file, options).catch((err) => {
    diagnostics.error('window.load_promise_rejected', {
      duration_ms: Date.now() - started, error: errorFields(err),
    }, surface);
  });
}

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
  attachWindowDiagnostics(win, 'orb-renderer');
  win.setAlwaysOnTop(true, 'floating');
  // The window spans most of the screen but only the blob and its items may
  // eat clicks. Start click-through; the renderer toggles it from hover
  // (forward:true keeps mousemove flowing while ignored, so hover works).
  win.setIgnoreMouseEvents(true, { forward: true });
  // demo mode reaches the renderer as a query param — the clean channel
  loadWindowFile(win, path.join(__dirname, 'renderer', 'index.html'),
                 DEMO ? { query: { demo: '1' } } : undefined, 'orb-renderer');
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
  if (!win) {
    mainDiag.warn('orb.send_dropped', { channel });
    return;
  }
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
let dismissStartedAt = 0;

function summon(reason) {
  const recreated = !win;
  if (recreated) createWindow();
  // re-cover the work area every summon: displays change, docks move
  const bounds = win.getBounds();
  const fresh = sceneBounds();
  if (bounds.x !== fresh.x || bounds.y !== fresh.y ||
      bounds.width !== fresh.width || bounds.height !== fresh.height) {
    win.setBounds(fresh);
  }
  visible = true;
  dismissing = false;
  dismissStartedAt = 0;
  win.show();
  mainDiag.info('orb.summon', { reason, recreated });
  sendToScene('orb:reveal', { anchor: anchorPoint(win.getBounds()), reason });
}

function dismiss() {
  if (!win || !visible) {
    mainDiag.debug('orb.dismiss_skipped', { has_window: !!win, visible });
    return;
  }
  dismissing = true;
  dismissStartedAt = Date.now();
  mainDiag.info('orb.dismiss_requested', {});
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
    mainDiag.info('control.shown', { reused: true });
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
  mainDiag.info('control.shown', { reused: false });
  attachWindowDiagnostics(controlWin, 'control-renderer');
  loadWindowFile(controlWin, path.join(__dirname, 'renderer', 'control.html'),
                 undefined, 'control-renderer');
  controlWin.on('closed', () => {
    controlWin = null;
  });
}

// The wake word runs iff the app runs. start-daemon no-ops when the daemon is
// already up, so this is safe to call on every launch and second-instance.
function ensureDaemon() {
  if (!MANAGED || !ownsLifecycle) {
    mainDiag.debug('daemon.start_skipped', { managed: MANAGED, owns_lifecycle: ownsLifecycle });
    return;
  }
  runEchoechoctl('start-daemon').then((r) => {
    // The Dock icon promises "echoecho is listening" — a silent start failure
    // (missing .venv, stale repoRoot, no API key) would make it lie.
    if (!r.ok) console.error('[daemon] start-daemon failed; see diagnostics/control panel');
  });
}

app.whenReady().then(() => {
  mainDiag.info('app.ready', {});
  if (!primaryInstance) {
    app.quit();
    return;
  }
  ownsLifecycle = true;
  // echoechoctl start-daemon relaunches the app when it isn't running; that
  // arrives here as a second instance, so make sure the daemon comes up too.
  app.on('second-instance', () => {
    mainDiag.info('app.second_instance', {});
    openControl(); ensureDaemon();
  });
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
    mainDiag.info('tray.ready', {});
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
    mainDiag.warn('tray.unavailable', { error: errorFields(err) });
  }

  createWindow();

  viewer = new ViewerClient(VIEWER_BASE, { diagnostics: viewerDiag });
  viewer.on('events', (evts) => sendToScene('viewer:events', evts));
  viewer.on('wake', () => summon('wake'));
  viewer.on('connected', () => {
    if (viewerConnectionState !== true) mainDiag.info('viewer.connected', {});
    viewerConnectionState = true;
    sendToScene('viewer:status', { connected: true });
  });
  viewer.on('disconnected', () => {
    if (viewerConnectionState !== false) mainDiag.warn('viewer.disconnected', {});
    viewerConnectionState = false;
    sendToScene('viewer:status', { connected: false });
  });
  viewer.start();

  ensureDaemon();

  const shortcutRegistered = globalShortcut.register('CommandOrControl+Shift+E', toggle);
  mainDiag[shortcutRegistered ? 'info' : 'warn']('shortcut.registration', {
    registered: shortcutRegistered,
  });

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
}).catch((err) => {
  mainDiag.error('app.startup_failed', { error: errorFields(err) });
  throw err;
});

app.on('child-process-gone', (_event, details) => {
  mainDiag.error('app.child_process_gone', {
    type: details && details.type,
    reason: details && details.reason,
    exit_code: details && details.exitCode,
    service_name_fingerprint: fingerprint(details && details.serviceName),
  });
});

app.on('before-quit', () => mainDiag.info('app.before_quit', {}));
app.on('will-quit', () => {
  flushNoisyDiagnosticSummary();
  mainDiag.info('app.will_quit', {
    owns_lifecycle: ownsLifecycle, managed: MANAGED,
  });
  globalShortcut.unregisterAll();
  if (viewer) viewer.stop();
  if (vncProxy) vncProxy.stop();
  // Quitting the app quits the wake word: detached so it outlives us. This
  // also covers daemons we didn't start; force quit (no will-quit) is handled
  // by the daemon's own tether to our pid.
  if (MANAGED && ownsLifecycle) runEchoechoctl('stop-daemon', true);
  diagnostics.close('app-quit');
});

process.on('exit', () => diagnostics.close('process-exit'));

// Keep running with no windows: we live in the menu bar.
app.on('window-all-closed', () => {});

// ---- IPC ----------------------------------------------------------------

const RENDERER_DIAGNOSTIC_SCHEMA = {
  'client.error': { level: 'error', fields: { line: 'number', column: 'number', error_name: 'token', error_code: 'token' }, error: true },
  'client.unhandled_rejection': { level: 'error', fields: { error_name: 'token', error_code: 'token' }, error: true },
  'control.refresh_failed': { level: 'warn', fields: { consecutive: 'number', duration_ms: 'number', error_name: 'token', error_code: 'token' }, error: true },
  'control.refresh_recovered': { level: 'info', fields: { failed_attempts: 'number', downtime_ms: 'number' } },
  'control.status_transition': { level: 'info', fields: { daemon: 'boolean', vm: 'boolean', orb_visible: 'boolean' } },
  'control.action_start': { level: 'info', fields: { action: 'token' } },
  'control.action_done': { level: 'info', fields: { action: 'token', ok: 'boolean', detached: 'boolean', duration_ms: 'number' } },
  'control.action_failed': { level: 'warn', fields: { action: 'token', duration_ms: 'number', error_name: 'token', error_code: 'token' }, error: true },
  'control.action_cancelled': { level: 'info', fields: { action: 'token' } },
  'control.login_item_failed': { level: 'warn', fields: { enabled: 'boolean', error_name: 'token', error_code: 'token' }, error: true },
  'scene.storage_failed': { level: 'warn', fields: { operation: 'token', error_name: 'token', error_code: 'token' }, error: true },
  'scene.vnc_open_failed': { level: 'warn', fields: { error_name: 'token', error_code: 'token' }, error: true },
  'scene.doc_fetch_failed': { level: 'warn', fields: { error_name: 'token', error_code: 'token' }, error: true },
  'scene.lifecycle': { level: 'info', fields: { transition: 'token', reason: 'token', duration_ms: 'number' } },
  'scene.viewer_status': { level: 'info', fields: { connected: 'boolean' } },
  'scene.task_state': { level: 'info', fields: { status: 'token', running: 'number' } },
  'vnc.stage': { level: 'info', fields: { stage: 'token', outcome: 'token', attempt: 'number', duration_ms: 'number', error_name: 'token', error_code: 'token' }, error: true },
  'vnc.disconnect': { level: 'info', fields: { clean: 'boolean', was_connected: 'boolean', connected_ms: 'number' } },
  'vnc.credentials_required': { level: 'warn', fields: { has_password: 'boolean' } },
  'vnc.security_failure': { level: 'warn', fields: { has_reason: 'boolean' } },
  'vnc.view_only': { level: 'info', fields: { enabled: 'boolean' } },
  'vnc.cleanup_failed': { level: 'warn', fields: { stage: 'token', error_name: 'token', error_code: 'token' }, error: true },
};

function rendererSurface(sender) {
  if (win && sender === win.webContents) return 'orb-renderer';
  if (controlWin && sender === controlWin.webContents) return 'control-renderer';
  return 'unknown-renderer';
}

const RENDERER_TOKEN_VALUES = {
  action: new Set(['summon', 'live-writer', 'daemon-start', 'daemon-stop', 'daemon-restart',
                   'vm-boot', 'vm-reset', 'update', 'quit-app']),
  operation: new Set(['pose-read', 'pose-write']),
  transition: new Set(['reveal-received', 'dismiss-received', 'hidden-ack', 'revealed']),
  reason: new Set(['wake', 'tray', 'dock', 'menu', 'control', 'smoke', 'demo', 'unknown']),
  status: new Set(['queued', 'running', 'progress', 'done', 'error', 'unknown']),
  stage: new Set(['module-import', 'module-ready', 'proxy-connect', 'rfb-create', 'rfb-connect',
                  'scene-remove', 'close', 'open-replace', 'proxy-disconnect']),
  outcome: new Set(['ready', 'failed', 'superseded']),
};
const SAFE_ERROR_NAMES = new Set([
  'Error', 'TypeError', 'RangeError', 'ReferenceError', 'SyntaxError', 'DOMException',
  'AbortError', 'NotAllowedError', 'NotFoundError', 'SecurityError', 'NetworkError',
  'InvalidStateError', 'OperationError',
]);
const MAX_RENDERER_DIAGNOSTIC_EVENT_CHARS = 80;
const MAX_RENDERER_DIAGNOSTIC_TOKEN_CHARS = 256;
const MAX_RENDERER_DIAGNOSTIC_SCHEMA_FIELDS = 16;

function rendererPrimitive(value) {
  if (typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  return null;
}

function rendererLength(value) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value : null;
}

function safeToken(key, value) {
  value = rendererPrimitive(value);
  if (value === null) return null;
  const raw = typeof value === 'string' ? value : String(value);
  if (raw.length > MAX_RENDERER_DIAGNOSTIC_TOKEN_CHARS) {
    return `fingerprint:${boundedFingerprint(raw).fingerprint}`;
  }
  if (RENDERER_TOKEN_VALUES[key]) {
    return RENDERER_TOKEN_VALUES[key].has(raw) ? raw : `fingerprint:${fingerprint(raw)}`;
  }
  if (key === 'error_name') {
    return SAFE_ERROR_NAMES.has(raw) ? raw : `fingerprint:${fingerprint(raw)}`;
  }
  if (key === 'error_code') {
    return /^[A-Z0-9_.-]{0,40}$/.test(raw) ? raw : `fingerprint:${fingerprint(raw)}`;
  }
  return /^[a-z0-9_.:-]{0,80}$/i.test(raw) ? raw : `fingerprint:${fingerprint(raw)}`;
}

function normalizeRendererDiagnostic(schema, raw) {
  const fields = {};
  raw = raw && typeof raw === 'object' ? raw : {};
  let processed = 0;
  for (const [key, type] of Object.entries(schema.fields)) {
    if (++processed > MAX_RENDERER_DIAGNOSTIC_SCHEMA_FIELDS) break;
    const value = raw[key];
    if (value === undefined || value === null) continue;
    if (type === 'number' && typeof value === 'number' && Number.isFinite(value)) {
      fields[key] = value;
    } else if (type === 'boolean' && typeof value === 'boolean') {
      fields[key] = value;
    } else if (type === 'token') {
      const token = safeToken(key, value);
      if (token !== null) fields[key] = token;
    }
  }
  if (schema.error) {
    const message = raw.message || raw.error_message || '';
    const stack = raw.stack || '';
    if (typeof raw.message_fingerprint === 'string' &&
        /^[a-f0-9]{16}$/.test(raw.message_fingerprint)) {
      fields.message_fingerprint = raw.message_fingerprint;
      const length = rendererLength(raw.message_length);
      if (length !== null) fields.message_length = length;
    } else if (message !== '' && rendererPrimitive(message) !== null) {
      const meta = boundedFingerprint(message);
      fields.message_fingerprint = meta.fingerprint;
      fields.message_length = meta.length;
    }
    if (typeof raw.stack_fingerprint === 'string' &&
        /^[a-f0-9]{16}$/.test(raw.stack_fingerprint)) {
      fields.stack_fingerprint = raw.stack_fingerprint;
      const length = rendererLength(raw.stack_length);
      if (length !== null) fields.stack_length = length;
    } else if (stack !== '' && rendererPrimitive(stack) !== null) {
      const meta = boundedFingerprint(stack);
      fields.stack_fingerprint = meta.fingerprint;
      fields.stack_length = meta.length;
    }
  }
  return fields;
}

ipcMain.on('diagnostics:event', (ipcEvent, payload) => {
  if (!payload || typeof payload !== 'object') return;
  const surface = rendererSurface(ipcEvent.sender);
  if (surface === 'unknown-renderer') return;
  const rawEvent = typeof payload.event === 'string' ? payload.event : '';
  const event = rawEvent.length <= MAX_RENDERER_DIAGNOSTIC_EVENT_CHARS ? rawEvent : '';
  const schema = RENDERER_DIAGNOSTIC_SCHEMA[event];
  if (!schema) {
    const eventFingerprint = boundedFingerprint(rawEvent).fingerprint;
    const sample = sampleNoisyDiagnostic(
      `rejected:${surface}`, eventFingerprint);
    if (sample.emit) {
      mainDiag.warn('renderer.report_rejected', {
        event_fingerprint: eventFingerprint, source: surface,
        occurrences: sample.count,
      });
    }
    return;
  }
  const fields = normalizeRendererDiagnostic(schema, payload.fields);
  const level = event === 'vnc.stage' && fields.outcome === 'failed' ? 'warn' : schema.level;
  const fieldsFingerprint = fingerprint(JSON.stringify(fields));
  const sample = sampleNoisyDiagnostic(
    `report:${surface}:${event}`, fieldsFingerprint);
  if (sample.emit) {
    diagnostics.log(level, event, { ...fields, occurrences: sample.count }, surface);
  }
});

ipcMain.handle('viewer:transcript', () => viewer.transcript());
ipcMain.handle('viewer:doc', (_e, relpath) => viewer.doc(String(relpath)));

ipcMain.on('orb:hidden', () => {
  if (!dismissing) {
    mainDiag.warn('orb.hidden_stale', {});
    return;
  }
  dismissing = false;
  visible = false;
  mainDiag.info('orb.hidden', {
    dismiss_ms: dismissStartedAt ? Date.now() - dismissStartedAt : null,
  });
  dismissStartedAt = 0;
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
  const started = Date.now();
  const source = process.env.ECHOECHO_VNC_URL ? 'environment' : 'viewer';
  mainDiag.info('vnc.connect_started', { source });
  try {
    let target = process.env.ECHOECHO_VNC_URL || null;
    if (!target) {
      const info = await viewer.vncInfo(); // throws -> renderer shows "asleep"
      target = info.url;
    }
    if (!vncProxy) {
      vncProxy = require('./vnc-proxy');
      if (vncProxy.setDiagnostics) vncProxy.setDiagnostics(diagnostics.child('vnc-proxy'));
    }
    const info = await vncProxy.start(target);
    mainDiag.info('vnc.connect_ready', {
      source, duration_ms: Date.now() - started,
    });
    return info;
  } catch (err) {
    mainDiag.warn('vnc.connect_failed', {
      source, duration_ms: Date.now() - started, error: errorFields(err),
    });
    return { error: String((err && err.message) || err) };
  }
});

ipcMain.handle('vnc:disconnect', () => {
  const started = Date.now();
  if (!vncProxy) {
    mainDiag.debug('vnc.disconnect_skipped', {});
    return;
  }
  return vncProxy.stop().then(() => {
    mainDiag.info('vnc.disconnected', { duration_ms: Date.now() - started });
  }).catch((err) => {
    mainDiag.warn('vnc.disconnect_failed', {
      duration_ms: Date.now() - started, error: errorFields(err),
    });
    throw err;
  });
});

// ---- control panel IPC ----------------------------------------------------

function runEchoechoctl(cmd, detached) {
  return new Promise((resolve) => {
    const started = Date.now();
    const MAX_CAPTURE = 64 * 1024;
    mainDiag.info('ctl.command_started', {
      command: cmd, detached: !!detached, parent_run_id: diagnostics.runId,
    });
    let child;
    try {
      child = spawn('bash', [ECHOECHOCTL, cmd], {
        cwd: RUNTIME.repoRoot,
        // start-daemon tethers the daemon to this pid: it exits when we do,
        // force quit included. Other commands ignore the variable.
        env: {
          ...process.env,
          ECHOECHO_TETHER_PID: String(process.pid),
          ECHOECHO_PARENT_RUN_ID: diagnostics.runId,
        },
        detached: !!detached,
        stdio: detached ? 'ignore' : ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      mainDiag.error('ctl.command_failed', {
        command: cmd, duration_ms: Date.now() - started,
        parent_run_id: diagnostics.runId, error: errorFields(err),
      });
      resolve({ ok: false, output: String((err && err.message) || err) });
      return;
    }
    if (detached) {
      child.once('error', (err) => {
        mainDiag.error('ctl.detached_command_failed', {
          command: cmd, duration_ms: Date.now() - started,
          parent_run_id: diagnostics.runId, error: errorFields(err),
        });
      });
      child.unref();
      mainDiag.info('ctl.command_detached', {
        command: cmd, duration_ms: Date.now() - started,
        parent_run_id: diagnostics.runId,
      });
      resolve({ ok: true, detached: true });
      return;
    }
    let out = '';
    let capturedBytes = 0;
    let totalBytes = 0;
    let truncated = false;
    let settled = false;
    const capture = (d) => {
      const buf = Buffer.from(d);
      totalBytes += buf.length;
      if (capturedBytes < MAX_CAPTURE) {
        const take = buf.subarray(0, MAX_CAPTURE - capturedBytes);
        out += take.toString('utf8');
        capturedBytes += take.length;
      }
      if (totalBytes > MAX_CAPTURE) truncated = true;
    };
    const finish = (result, err) => {
      if (settled) return;
      settled = true;
      const fields = {
        command: cmd, ok: !!result.ok, duration_ms: Date.now() - started,
        parent_run_id: diagnostics.runId,
        exit_code: result.code === undefined ? null : result.code,
        stdout_stderr_bytes: totalBytes, capture_truncated: truncated,
      };
      if (err) fields.error = errorFields(err);
      mainDiag[result.ok ? 'info' : 'warn']('ctl.command_finished', fields);
      resolve({ ok: result.ok, output: result.output });
    };
    child.stdout.on('data', capture);
    child.stderr.on('data', capture);
    child.on('close', (code) => finish({
      ok: code === 0, code, output: out.trim() + (truncated ? '\n[output truncated]' : ''),
    }));
    child.on('error', (err) => finish({ ok: false, output: String(err.message || err) }, err));
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

ipcMain.handle('ctl:action', async (_e, name) => {
  const action = String(name);
  const fn = CTL_ACTIONS[action];
  const started = Date.now();
  if (!fn) {
    mainDiag.warn('ctl.action_rejected', {
      action_fingerprint: fingerprint(action),
    });
    return { ok: false, output: `unknown action ${name}` };
  }
  mainDiag.info('ctl.action_started', { action });
  try {
    const result = await fn();
    mainDiag[result && result.ok === false ? 'warn' : 'info']('ctl.action_finished', {
      action, ok: !result || result.ok !== false, detached: !!(result && result.detached),
      duration_ms: Date.now() - started,
    });
    return result;
  } catch (err) {
    mainDiag.error('ctl.action_failed', {
      action, duration_ms: Date.now() - started, error: errorFields(err),
    });
    throw err;
  }
});

ipcMain.handle('ctl:login-item', (_e, enable) => {
  const requested = !!enable;
  try {
    app.setLoginItemSettings({ openAtLogin: requested });
    mainDiag.info('ctl.login_item_changed', { enabled: requested });
    return { ok: true };
  } catch (err) {
    mainDiag.error('ctl.login_item_failed', {
      enabled: requested, error: errorFields(err),
    });
    throw err;
  }
});
