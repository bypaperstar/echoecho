'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const DIAGNOSTIC_EVENTS = new Set([
  'client.error', 'client.unhandled_rejection',
  'control.refresh_failed', 'control.refresh_recovered', 'control.status_transition',
  'control.action_start', 'control.action_done', 'control.action_failed',
  'control.action_cancelled', 'control.login_item_failed',
  'scene.storage_failed', 'scene.vnc_open_failed', 'scene.doc_fetch_failed',
  'scene.lifecycle', 'scene.viewer_status', 'scene.task_state',
  'vnc.stage', 'vnc.disconnect', 'vnc.credentials_required',
  'vnc.security_failure', 'vnc.view_only', 'vnc.cleanup_failed',
]);
const DIAGNOSTIC_FIELDS = new Set([
  'line', 'column', 'error_name', 'error_code', 'message', 'error_message',
  'stack', 'consecutive', 'duration_ms', 'failed_attempts', 'downtime_ms',
  'daemon', 'vm', 'orb_visible', 'action', 'ok', 'detached', 'enabled',
  'operation', 'transition', 'reason', 'connected', 'status', 'running',
  'stage', 'outcome', 'attempt', 'clean', 'was_connected', 'connected_ms',
  'has_password', 'has_reason',
]);
const MAX_DIAGNOSTIC_INPUT_FIELDS = 24;
const MAX_DIAGNOSTIC_EVENT_CHARS = 80;
const MAX_DIAGNOSTIC_KEY_CHARS = 64;
const MAX_DIAGNOSTIC_FIELD_CHARS = 256;
const MAX_DIAGNOSTIC_HASH_CHARS = 4096;

// Sandboxed preloads may only require Electron's allowlisted modules. A paired
// FNV-1a fingerprint keeps arbitrary renderer messages off IPC without crypto.
const safeString = (value, fallback = '[unprintable]') => {
  try { return String(value === undefined || value === null ? '' : value); }
  catch { return fallback; }
};

const hash = (value) => {
  const input = safeString(value);
  let a = 0x811c9dc5, b = 0x9e3779b9;
  for (let i = 0; i < input.length; i++) {
    const c = input.charCodeAt(i);
    a = Math.imul(a ^ c, 0x01000193);
    b = Math.imul(b ^ c, 0x85ebca6b);
  }
  return (a >>> 0).toString(16).padStart(8, '0') +
    (b >>> 0).toString(16).padStart(8, '0');
};

const boundedHash = (value) => {
  const input = safeString(value);
  const length = input.length;
  return {
    fingerprint: hash(`${length}:${input.slice(0, MAX_DIAGNOSTIC_HASH_CHARS)}`),
    length,
  };
};

function boundedPrimitive(value) {
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined;
  if (typeof value !== 'string') return undefined;
  if (value.length <= MAX_DIAGNOSTIC_FIELD_CHARS) return value;
  const meta = boundedHash(value);
  return `oversized:${meta.fingerprint}:${meta.length}`;
}

function reportDiagnostic(event, input = {}) {
  try {
    if (typeof event !== 'string' || event.length > MAX_DIAGNOSTIC_EVENT_CHARS ||
        !DIAGNOSTIC_EVENTS.has(event)) return false;
    const fields = {};
    let emitted = 0;
    const put = (key, value) => {
      if (emitted >= MAX_DIAGNOSTIC_INPUT_FIELDS ||
          Object.prototype.hasOwnProperty.call(fields, key)) return;
      fields[key] = value;
      emitted++;
    };
    if (input && typeof input === 'object') {
      let examined = 0;
      // Do not use Object.entries: it materializes and clones every property
      // before a limit can be applied. Renderer reports cross a privileged IPC
      // boundary, so examine a small primitive allowlist only.
      for (const key in input) {
        if (!Object.prototype.hasOwnProperty.call(input, key)) continue;
        if (++examined > MAX_DIAGNOSTIC_INPUT_FIELDS) break;
        if (key.length > MAX_DIAGNOSTIC_KEY_CHARS || !DIAGNOSTIC_FIELDS.has(key)) continue;
        let value;
        try { value = input[key]; } catch { continue; }
        if (key === 'message' || key === 'error_message') {
          if (!['string', 'number', 'boolean'].includes(typeof value)) continue;
          const message = boundedHash(value);
          put('message_fingerprint', message.fingerprint);
          put('message_length', message.length);
        } else if (key === 'stack') {
          if (!['string', 'number', 'boolean'].includes(typeof value)) continue;
          const stack = boundedHash(value);
          put('stack_fingerprint', stack.fingerprint);
          put('stack_length', stack.length);
        } else {
          const bounded = boundedPrimitive(value);
          if (bounded !== undefined) put(key, bounded);
        }
      }
    }
    ipcRenderer.send('diagnostics:event', { event, fields });
    return true;
  } catch {
    return false;
  }
}

function errorDiagnostic(event, err, extra = {}) {
  try {
    const obj = err && typeof err === 'object' ? err : {};
    const fields = {};
    try { Object.assign(fields, extra); } catch { /* hostile extra ignored */ }
    try { fields.error_name = obj.name || typeof err; } catch { fields.error_name = typeof err; }
    try { fields.error_code = obj.code || ''; } catch { fields.error_code = ''; }
    try { fields.message = obj.message || safeString(err); } catch { fields.message = safeString(err); }
    try { fields.stack = obj.stack || ''; } catch { fields.stack = ''; }
    return reportDiagnostic(event, fields);
  } catch {
    return false;
  }
}

window.addEventListener('error', (event) => {
  try {
    errorDiagnostic('client.error', event.error || event.message, {
      line: Number(event.lineno) || null,
      column: Number(event.colno) || null,
    });
  } catch { /* never recurse from global error instrumentation */ }
});
window.addEventListener('unhandledrejection', (event) => {
  try { errorDiagnostic('client.unhandled_rejection', event.reason); }
  catch { /* never recurse from global rejection instrumentation */ }
});

contextBridge.exposeInMainWorld('echoDiagnostics', {
  report: reportDiagnostic,
});

contextBridge.exposeInMainWorld('orb', {
  // lifecycle
  onReveal: (cb) => ipcRenderer.on('orb:reveal', (_e, p) => cb(p)),
  onDismiss: (cb) => ipcRenderer.on('orb:dismiss', () => cb()),
  hidden: () => ipcRenderer.send('orb:hidden'),
  dismissRequest: () => ipcRenderer.send('orb:dismiss-request'),
  // true = clicks fall through to whatever is behind the window
  setPassthrough: (on) => ipcRenderer.send('orb:passthrough', on),

  // viewer data plane (proxied through main; see lib/backend.js)
  transcript: () => ipcRenderer.invoke('viewer:transcript'),
  doc: (relpath) => ipcRenderer.invoke('viewer:doc', relpath),
  onEvents: (cb) => ipcRenderer.on('viewer:events', (_e, evts) => cb(evts)),
  onViewerStatus: (cb) => ipcRenderer.on('viewer:status', (_e, s) => cb(s)),

  // echoecho's Mac (main marshals failure as { error } to keep its console
  // clean; the renderer contract stays a rejected promise)
  vncConnect: () => ipcRenderer.invoke('vnc:connect').then((info) => {
    if (info && info.error) throw new Error(info.error);
    return info;
  }),
  vncDisconnect: () => ipcRenderer.invoke('vnc:disconnect'),

  // smoke/demo wiring: lets main align the screenshot series with the
  // scene's demo timeline
  demoStarted: () => ipcRenderer.send('orb:demo-started'),
});

// control panel surface (same preload serves both windows)
contextBridge.exposeInMainWorld('ctl', {
  status: () => ipcRenderer.invoke('ctl:status'),
  action: (name) => ipcRenderer.invoke('ctl:action', name),
  setLoginItem: (enable) => ipcRenderer.invoke('ctl:login-item', enable),
});
