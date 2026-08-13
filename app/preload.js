'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('orb', {
  // lifecycle
  onReveal: (cb) => ipcRenderer.on('orb:reveal', (_e, p) => cb(p)),
  onDismiss: (cb) => ipcRenderer.on('orb:dismiss', () => cb()),
  onBlur: (cb) => ipcRenderer.on('orb:blur', () => cb()),
  hidden: () => ipcRenderer.send('orb:hidden'),
  dismissRequest: () => ipcRenderer.send('orb:dismiss-request'),

  // viewer data plane (proxied through main; see lib/backend.js)
  transcript: () => ipcRenderer.invoke('viewer:transcript'),
  doc: (relpath) => ipcRenderer.invoke('viewer:doc', relpath),
  onEvents: (cb) => ipcRenderer.on('viewer:events', (_e, evts) => cb(evts)),
  onViewerStatus: (cb) => ipcRenderer.on('viewer:status', (_e, s) => cb(s)),

  // Echo's Mac
  vncConnect: () => ipcRenderer.invoke('vnc:connect'),
  vncDisconnect: () => ipcRenderer.invoke('vnc:disconnect'),
});
