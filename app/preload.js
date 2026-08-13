'use strict';

const { contextBridge, ipcRenderer } = require('electron');

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

  // Echo's Mac (main marshals failure as { error } to keep its console
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
