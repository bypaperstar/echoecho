// Harness preload: expose the same two window.orb members renderer/vnc.js
// uses (backed by the REAL vnc-proxy in vnc-smoke-main.js), plus a smoke
// channel for phase sync and failure reporting.
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('orb', {
  vncConnect: () => ipcRenderer.invoke('vnc:connect'),
  vncDisconnect: () => ipcRenderer.invoke('vnc:disconnect'),
});

contextBridge.exposeInMainWorld('smoke', {
  phase: (name) => ipcRenderer.invoke('smoke:phase', name),
  fail: (msg) => ipcRenderer.send('smoke:fail', msg),
});
